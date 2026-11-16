"""每天 0 点把前一天的 token 消耗汇总，私聊发给主人。

用法：装上即用。默认 0 点整发「上一个完整自然日」的报表。
数据源是 MaiBot 自己的 llm_usage 明细表，直接只读打开 sqlite，
不走聚合表（statistics_model_hourly 有 15 分钟延迟，零点会漏掉数据）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import asyncio
import json
import os
import sqlite3

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

# 轮询间隔：框架没有 cron，只能自己醒来看表。60 秒足够准，也不会白烧 CPU。
_TICK_SECONDS = 60.0
# 状态文件里最多记多少天的发送记录（防重发用）。
_KEEP_DAYS = 40
_OWNER_QQ = "100000000"

_TASK_LABELS = {
    "planner": "决策", "replyer": "回复生成", "utils": "辅助处理",
    "memory": "记忆整理", "memory_episode": "记忆分段",
    "learner": "表达学习", "vlm": "识图", "emoji": "表情包选择",
}


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ReportConfig(PluginConfigBase):
    """报表配置。"""

    __ui_label__ = "报表"
    __ui_icon__ = "bar-chart"
    __ui_order__ = 1

    target_qq: list[str] = Field(default_factory=lambda: ["100000000"], description="报表发给哪些 QQ 号")
    send_hour: int = Field(default=0, description="每天几点发（0-23）")
    send_minute: int = Field(default=0, description="每天几分发（0-59）")
    top_sessions: int = Field(default=6, description="会话榜显示前几名")
    top_models: int = Field(default=5, description="模型榜显示前几名")
    catch_up_on_start: bool = Field(default=True, description="启动时补发漏掉的昨日报表")


class DataConfig(PluginConfigBase):
    """数据源配置。"""

    __ui_label__ = "数据源"
    __ui_icon__ = "database"
    __ui_order__ = 2

    db_path: str = Field(default="", description="MaiBot.db 路径；留空自动探测")
    image_price: float = Field(default=0.03, description="每张生成图片的价格（元）")
    image_quota_db: str = Field(default="", description="oaimage 插件的 image_quota.sqlite3 路径；留空自动探测")


class DailyReportConfig(PluginConfigBase):
    """每日用量日报配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    data: DataConfig = Field(default_factory=DataConfig)


@dataclass
class DayTotals:
    """一天的总量。"""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cost: float = 0.0


@dataclass
class Bucket:
    """按会话 / 模型 / 任务聚合的一行。"""

    key: str
    label: str
    total_tokens: int = 0
    cost: float = 0.0
    requests: int = 0


def _fmt_tokens(value: int) -> str:
    """把 token 数写成好读的紧凑形式。"""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return f"{value:,}"


def _fmt_money(value: float) -> str:
    """金额，很小的时候保留更多位数，别显示成 0。"""
    if value <= 0:
        return "¥0"
    if value < 0.01:
        return f"¥{value:.4f}"
    return f"¥{value:.2f}"


# 模型通道的人话翻译：MaiBot 里的名字是「渠道-用途」的内部编号。
_MODEL_LABELS = {
    "free-chat": "聊天",
    "workbuddy-chat": "聊天",
    "free-think": "深度思考",
    "free-vision": "识图",
    "geili-flex-chat": "聊天",
    "geili-flex-think": "深度思考",
    "geili-flex-vision": "识图",
    "luna-chat": "聊天",
    "luna-vision": "识图",
    "geili-chat": "聊天",
    "geili-think": "深度思考",
    "geili-vision": "识图",
    "geili2-chat": "聊天",
    "geili2-think": "深度思考",
    "geili2-vision": "识图",
    "local-bge-m3": "向量",
}
# 渠道正名：2026-11-11 渠道大换血（详见部署说明 5.102/5.103），改名前后的记录
# 要归并到同一渠道，不然报表会把一个渠道拆成两行。计费口径直接折进渠道名
# （用户定稿版式：免费组叫「ds免费组」，flex 标 0.25×），不再单独反推倍率。
# 注意历史同名不同义：'geili' 在 11-11 22:10 前指 codexpert(0.2×)，之后指免费组；
# 报表只渲染昨天，所以换血日起 'geili'/'geili2' 的旧名记录都出自换血后的渠道。
_CHANNEL_OF = {
    "free": "ds免费组",
    "geili": "ds免费组",
    "geili-flex": "给力·flex（0.25×）",
    "geili2": "给力·flex（0.25×）",
    "luna": "luna·GPT",
    "codexpert": "codexpert（0.2×）",
    "tokendance": "tokendance（1×）",
    "workbuddy": "聚合反代网关",
    "local-embed": "本地",
}


class DailyReportPlugin(MaiBotPlugin):
    """每天 0 点汇报前一天 token 消耗。"""

    config_model = DailyReportConfig

    def __init__(self) -> None:
        super().__init__()
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._shutting_down = False
        self._state_lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._shutting_down = False
        self.ctx.logger.info("每日用量日报已加载，目标 QQ=%s", self.config.report.target_qq)
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._daily_loop())

    async def on_unload(self) -> None:
        self._shutting_down = True
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            self.ctx.logger.info("每日用量日报配置已更新")

    # ---------- 主循环 ----------

    async def _daily_loop(self) -> None:
        """每分钟醒一次，到点就发；靠状态文件保证一天只发一次。"""
        try:
            if self.config.report.catch_up_on_start:
                # 启动补发：如果昨天该发的还没发，先补上。
                await asyncio.sleep(20)
                await self._maybe_send(catch_up=True)
            while not self._shutting_down:
                await asyncio.sleep(_TICK_SECONDS)
                if self._shutting_down:
                    break
                try:
                    await self._maybe_send(catch_up=False)
                except Exception:
                    self.ctx.logger.warning("发送每日报表失败，下个周期重试", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _maybe_send(self, *, catch_up: bool) -> None:
        """判断该不该发，该发就发。"""
        now = datetime.now()
        report_day = (now - timedelta(days=1)).date()

        # 还没到今天设定的发送时刻，说明该发的是「前天」，不是昨天。
        send_at = now.replace(
            hour=max(0, min(23, int(self.config.report.send_hour))),
            minute=max(0, min(59, int(self.config.report.send_minute))),
            second=0,
            microsecond=0,
        )
        if now < send_at:
            report_day = report_day - timedelta(days=1)

        keys = await self._load_sent_days()
        if report_day.isoformat() in keys:
            return
        # 补发模式下只补昨天，别把很久以前的都翻出来发一遍。
        if catch_up and report_day != (now - timedelta(days=1)).date():
            return

        text = await asyncio.to_thread(self._build_report, report_day)
        sent = await self._send_to_targets(text)
        if not sent:
            self.ctx.logger.warning("每日报表发送失败，等待下次重试: day=%s", report_day)
            return

        keys.add(report_day.isoformat())
        await self._save_sent_days(keys)
        self.ctx.logger.info("每日报表已发送: day=%s", report_day)

    # ---------- 手动触发 ----------

    @Command(
        "daily_report",
        description="立刻发送昨天的 token 报表",
        pattern=r"^/daily_report$",
        permission="operator",
    )
    async def handle_daily_report(self, stream_id: str = "", **kwargs: Any):
        """手动补发昨天的报表（不发到当前会话，还是发给配置里的 QQ）。"""
        del stream_id, kwargs
        report_day = (datetime.now() - timedelta(days=1)).date()
        text = await asyncio.to_thread(self._build_report, report_day)
        await self._send_to_targets(text)
        return True, f"已生成 {report_day} 的报表", True

    @Tool(
        "query_today_usage",
        description="主人查询今天截至目前的费用、token、渠道或用途分类；群内请求仅私发给主人。",
        parameters=[
            ToolParameterInfo(name="detailed", param_type=ToolParamType.BOOLEAN,
                              description="需要用途、渠道和会话分类时设为 true", required=False),
            ToolParameterInfo(name="group_name", param_type=ToolParamType.STRING,
                              description="只查某群时填写群名或群号，留空查全部", required=False),
        ],
        platforms=["qq"],
    )
    async def handle_query_today_usage(
        self, detailed: bool = False, group_name: str = "", **kwargs: Any,
    ) -> dict[str, Any]:
        """只信任 Host 另行注入的身份，不使用模型填写的 user_id 鉴权。"""
        caller = kwargs.get("_usage_context")
        if not isinstance(caller, dict) or (
            caller.get("platform") != "qq" or caller.get("user_id") != _OWNER_QQ
            or not caller.get("session_id")
            or type(caller.get("is_group_chat")) is not bool
            or bool(caller.get("group_id")) != caller.get("is_group_chat")
        ):
            return {"success": False, "error": "费用查询仅限主人本人，无法确认身份时不提供数据。"}
        try:
            if type(detailed) is not bool:
                return {"success": False, "error": "详细分类参数必须是布尔值。"}
            usage = await asyncio.to_thread(self._today_usage, str(group_name or "").strip())
            text = self._usage_text(usage, detailed=detailed)
        except ValueError:
            return {"success": False, "error": "找不到唯一匹配的群，请提供更完整的群名或群号。"}
        except Exception:
            self.ctx.logger.warning("今日费用查询失败", exc_info=True)
            return {"success": False, "error": "这次没查到账，不能把查询失败当成零费用。"}
        if caller["is_group_chat"]:
            try:
                stream_id = await self._resolve_stream_id(_OWNER_QQ)
                sent = bool(stream_id) and bool(await self.ctx.send.text(text, stream_id))
            except Exception:
                self.ctx.logger.warning("今日费用私聊发送失败", exc_info=True)
                sent = False
            if not sent:
                return {"success": False, "error": "账已查询，但私聊发送失败；不要在群里公布数据。"}
            return {"success": True, "content": "已将费用查询结果私聊发给主人，群里只确认已私发，不公布金额或明细。"}
        return {
            "success": True, "content": text, "usage": usage,
            "instruction": "用口语简短回答，注明统计截止时间和本地估算；不要编造未入账费用。",
        }

    # ---------- 发送 ----------

    async def _resolve_stream_id(self, user_id: str) -> str:
        """拿到某个 QQ 号的私聊 session_id，没有就建一个。"""
        try:
            stream = await self.ctx.chat.get_stream_by_user_id(user_id, "qq")
        except Exception as exc:
            self.ctx.logger.warning("查找私聊会话失败: user_id=%s err=%s", user_id, exc)
            stream = None

        if isinstance(stream, dict) and stream.get("success") is not False:
            session_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if session_id:
                return session_id
            account_id = str(stream.get("account_id") or "").strip()
            scope = str(stream.get("scope") or "").strip()
        else:
            account_id = ""
            scope = ""

        try:
            result = await self.ctx.chat.open_session(
                platform="qq",
                chat_type="private",
                user_id=user_id,
                account_id=account_id,
                scope=scope,
            )
        except Exception as exc:
            self.ctx.logger.warning("打开私聊会话失败: user_id=%s err=%s", user_id, exc)
            return ""

        if isinstance(result, dict) and result.get("success") is not False:
            return str(result.get("session_id") or result.get("stream_id") or "").strip()
        return ""

    async def _send_to_targets(self, text: str) -> bool:
        """发给所有目标，任意一个成功就算成功。"""
        targets = [str(item).strip() for item in (self.config.report.target_qq or []) if str(item).strip()]
        if not targets:
            self.ctx.logger.warning("没有配置 target_qq，无法发送报表")
            return False

        any_sent = False
        for user_id in targets:
            stream_id = await self._resolve_stream_id(user_id)
            if not stream_id:
                self.ctx.logger.warning("拿不到私聊会话，跳过: user_id=%s", user_id)
                continue
            try:
                ok = await self.ctx.send.text(text, stream_id)
            except Exception as exc:
                self.ctx.logger.warning("发送报表异常: user_id=%s err=%s", user_id, exc)
                continue
            if ok:
                any_sent = True
            else:
                self.ctx.logger.warning("发送报表返回失败: user_id=%s", user_id)
        return any_sent

    # ---------- 数据 ----------

    def _db_path(self) -> Path:
        """找到 MaiBot.db。配置优先，否则按插件位置往上推。"""
        configured = str(self.config.data.db_path or "").strip()
        if configured:
            return Path(configured)
        # plugins/deepsleep_daily_report/plugin.py -> MaiBot/data/MaiBot.db
        return Path(__file__).resolve().parents[2] / "data" / "MaiBot.db"

    def _image_usage(self, day: Any) -> tuple[int, float]:
        """从 oaimage 插件的配额库读当天生图张数，折算费用。"""
        configured = str(self.config.data.image_quota_db or "").strip()
        path = Path(configured) if configured else Path(__file__).resolve().parents[2] / "plugins" / "a-dawn_oaimage" / "data" / "image_quota.sqlite3"
        if not path.exists():
            return 0, 0.0
        try:
            uri = f"file:{path.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=10.0)
            try:
                row = con.execute("SELECT used FROM daily_usage WHERE day = ?", (day.isoformat(),)).fetchone()
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            return 0, 0.0
        count = int(row[0]) if row else 0
        return count, round(count * float(self.config.data.image_price), 4)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """只读查库；WAL 模式下不阻塞正在写的主进程。"""
        db_path = self._db_path()
        if not db_path.exists():
            self.ctx.logger.warning("数据库不存在: %s", db_path)
            return []
        uri = f"file:{db_path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10.0)
        try:
            con.row_factory = sqlite3.Row
            return [tuple(row) for row in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def _today_usage(self, group_name: str = "") -> dict[str, Any]:
        """在同一只读快照中统计今天，不将查库失败降级成零费用。"""
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        uri = self._db_path().resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10.0)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            where = "u.timestamp >= ? AND u.timestamp < ?"
            params: tuple[Any, ...] = (str(start), str(now))
            scope = "全部"
            if group_name:
                rows = con.execute(
                    "SELECT session_id,group_id,group_name FROM chat_sessions "
                    "WHERE group_id IS NOT NULL AND group_id != ''",
                ).fetchall()
                exact = [r for r in rows if group_name in (r["group_id"], r["group_name"])]
                matches = exact or [r for r in rows if group_name.casefold() in str(r["group_name"] or "").casefold()]
                group_ids = {r["group_id"] for r in matches}
                if len(group_ids) != 1:
                    raise ValueError("ambiguous or unknown group")
                sessions = [r["session_id"] for r in matches]
                where += " AND u.session_id IN (" + ",".join("?" for _ in sessions) + ")"
                params += tuple(sessions)
                scope = str(matches[0]["group_name"] or "指定群聊")
            totals = dict(con.execute(
                "SELECT COUNT(*) requests,COALESCE(SUM(u.cost),0) llm_cost,"
                "COALESCE(SUM(u.prompt_tokens),0) prompt_tokens,"
                "COALESCE(SUM(u.completion_tokens),0) completion_tokens,"
                "COALESCE(SUM(u.total_tokens),0) total_tokens,"
                "COALESCE(SUM(u.prompt_cache_hit_tokens),0) cache_hit_tokens,"
                "MAX(u.timestamp) last_record FROM llm_usage u WHERE " + where, params,
            ).fetchone())
            tasks = [dict(r) for r in con.execute(
                "SELECT u.task_name name,COALESCE(SUM(u.cost),0) cost,"
                "COALESCE(SUM(u.total_tokens),0) tokens FROM llm_usage u WHERE " + where
                + " GROUP BY u.task_name ORDER BY cost DESC", params,
            )]
            channels = [dict(r) for r in con.execute(
                "SELECT u.model_api_provider_name name,COALESCE(SUM(u.cost),0) cost,"
                "COALESCE(SUM(u.total_tokens),0) tokens FROM llm_usage u WHERE " + where
                + " GROUP BY u.model_api_provider_name ORDER BY cost DESC", params,
            )]
            sessions = [dict(r) for r in con.execute(
                "SELECT CASE WHEN COALESCE(u.session_id,'')='' THEN '系统开销' "
                "WHEN COALESCE(s.group_id,'')!='' THEN '群 · '||COALESCE(NULLIF(s.group_name,''),'未命名群') "
                "WHEN COALESCE(s.user_id,'')!='' THEN '私聊 · '||COALESCE(NULLIF(s.user_nickname,''),'未命名联系人') "
                "ELSE '未匹配会话' END name,COALESCE(SUM(u.cost),0) cost,"
                "COALESCE(SUM(u.total_tokens),0) tokens FROM llm_usage u "
                "LEFT JOIN chat_sessions s ON s.session_id=u.session_id WHERE " + where
                + " GROUP BY u.session_id ORDER BY cost DESC LIMIT 6", params,
            )]
        finally:
            con.close()
        image_count = 0
        image_cost = 0.0
        image_available = False
        image_status = "单群无法分摊生图费用，未包含生图"
        if not group_name:
            configured = str(self.config.data.image_quota_db or "").strip()
            path = Path(configured) if configured else Path(__file__).resolve().parents[2] / "plugins/a-dawn_oaimage/data/image_quota.sqlite3"
            image_status = "生图配额库不可用，未计入生图"
            if path.exists():
                image_con = None
                try:
                    image_con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
                    row = image_con.execute("SELECT used FROM daily_usage WHERE day=?", (now.date().isoformat(),)).fetchone()
                    image_available = True
                    image_count = int(row[0]) if row else 0
                    image_cost = round(image_count * float(self.config.data.image_price), 6)
                    image_status = "生图配额库另行读取，按当日累计张数估算，非逐张时间快照" if row else "生图配额库今日暂无记录"
                except (sqlite3.Error, OSError, ValueError, TypeError):
                    image_status = "生图配额库读取失败，未计入生图"
                finally:
                    if image_con is not None:
                        image_con.close()
        return {
            **totals, "start": start.isoformat(sep=" "), "as_of": now.isoformat(sep=" "),
            "scope": scope, "tasks": tasks, "channels": channels, "sessions": sessions,
            "image_count": image_count, "image_cost": image_cost, "image_status": image_status,
            "image_available": image_available, "image_as_of": datetime.now().isoformat(sep=" "),
            "estimated_total_cost": round(totals["llm_cost"] + image_cost, 6) if image_available else None,
        }

    @staticmethod
    def _usage_text(usage: dict[str, Any], *, detailed: bool = False) -> str:
        """给聊天模型与私发消息共用的口语摘要，不输出代码或表格。"""
        stamp = datetime.fromisoformat(usage["as_of"])
        lines = [f"今天截至{stamp:%H:%M}，{usage['scope']}的LLM本地账本费用估算为"
                 f"{_fmt_money(usage['llm_cost'])}，用了{_fmt_tokens(usage['total_tokens'])} token。"]
        if detailed and usage["prompt_tokens"]:
            rate = 100 * usage["cache_hit_tokens"] / usage["prompt_tokens"]
            lines.append(f"输入{_fmt_tokens(usage['prompt_tokens'])}、输出{_fmt_tokens(usage['completion_tokens'])}，缓存读取比例{rate:.1f}%。")
        paid_tasks = [r for r in usage["tasks"] if r["cost"] > 0]
        if paid_tasks:
            lines.append("主要花在" + "、".join(
                f"{_TASK_LABELS.get(r['name'], '其他任务')}{_fmt_money(r['cost'])}"
                for r in paid_tasks[:3]) + "。")
        if detailed:
            lines.append("渠道：" + "；".join(
                f"{_CHANNEL_OF.get(r['name'], '其他渠道')} {_fmt_tokens(r['tokens'])} token / {_fmt_money(r['cost'])}"
                for r in usage["channels"]) + "。")
            lines.append("会话：" + "；".join(
                f"{r['name']} {_fmt_money(r['cost'])}" for r in usage["sessions"]) + "。")
        if usage["image_count"]:
            lines.append(f"其中已记录生图{usage['image_count']}张，估算{_fmt_money(usage['image_cost'])}。")
        last = usage["last_record"]
        lines.append(f"最新入账到{datetime.fromisoformat(last):%H:%M:%S}。" if last else "今天暂无已入账模型调用。")
        lines.append(usage["image_status"] + "；这不是供应商实付账单，未入账调用和绕过本地记录的测试不包含。")
        return "\n".join(lines)

    def _build_report(self, day: Any) -> str:
        """把某一天的用量拼成一条消息。"""
        start = f"{day.isoformat()} 00:00:00"
        end = f"{(day + timedelta(days=1)).isoformat()} 00:00:00"
        window = ("timestamp >= ? AND timestamp < ?", (start, end))

        rows = self._query(
            f"""SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),
                       COALESCE(SUM(total_tokens),0), COALESCE(SUM(prompt_cache_hit_tokens),0),
                       COALESCE(SUM(prompt_cache_miss_tokens),0), COALESCE(SUM(cost),0)
                FROM llm_usage WHERE {window[0]}""",
            window[1],
        )
        if not rows:
            return f"📊 {day.strftime('%m月%d日')} 用量日报\n\n读不到数据（数据库打不开）。"

        totals = DayTotals(*rows[0])
        if totals.requests == 0:
            return f"📊 {day.strftime('%m月%d日')} 用量日报\n\n昨天一条请求都没有，睡了整整一天…"

        img_count, img_cost = self._image_usage(day)
        lines = [f"📊 {day.strftime('%m月%d日')} 用量日报", ""]
        if img_count > 0:
            lines.append(f"总花费 {_fmt_money(totals.cost + img_cost)}（LLM {_fmt_money(totals.cost)} + 生图 {_fmt_money(img_cost)}）")
        else:
            lines.append(f"总花费 {_fmt_money(totals.cost)}")
        lines.append(
            f"token {_fmt_tokens(totals.total_tokens)}"
            f"（输入 {_fmt_tokens(totals.prompt_tokens)} / 输出 {_fmt_tokens(totals.completion_tokens)}）"
        )
        if img_count > 0:
            lines.append(f"生图 {img_count} 张 / {_fmt_money(img_cost)}（¥{self.config.data.image_price:g}/张）")

        # 分母用「全部输入 token」，不能只用 hit+miss：有些供应商不返回缓存用量，
        # 那部分 token 是按原价计费的，漏掉会把命中率算虚高。
        if totals.prompt_tokens > 0:
            rate = totals.cache_hit_tokens / totals.prompt_tokens * 100
            task_rates = self._task_hit_rates(window)
            detail = " / ".join(f"{name} {rate_:.0f}%" for name, rate_ in task_rates)
            lines.append(f"缓存读取 {_fmt_tokens(totals.cache_hit_tokens)} 缓存命中比例{rate:.1f}%（{detail}）")

        session_lines = self._session_lines(window)
        if session_lines:
            lines.append("")
            lines.append("—— 花得最多的地方 ——")
            lines.extend(session_lines)

        model_lines = self._model_lines(window)
        if model_lines:
            lines.append("")
            lines.append("—— 模型分渠道 ——")
            lines.extend(model_lines)

        return "\n".join(lines)

    def _task_hit_rates(self, window: tuple[str, tuple[Any, ...]]) -> list[tuple[str, float]]:
        """planner / replyer 两个大头的缓存命中率，其余任务量小不看。"""
        rows = self._query(
            f"""SELECT task_name,
                       COALESCE(SUM(prompt_tokens),0) p,
                       COALESCE(SUM(prompt_cache_hit_tokens),0) h
                FROM llm_usage
                WHERE {window[0]} AND task_name IN ('planner','replyer')
                GROUP BY task_name""",
            window[1],
        )
        return [(name, 100.0 * hit / max(prompt, 1)) for name, prompt, hit in rows]

    def _session_lines(self, window: tuple[str, tuple[Any, ...]]) -> list[str]:
        """按会话排行，群显示群名，私聊显示对方昵称。"""
        limit = max(1, min(20, int(self.config.report.top_sessions)))
        rows = self._query(
            f"""SELECT u.session_id,
                       COALESCE(s.group_name, s.user_nickname, ''),
                       s.group_id, s.user_id,
                       COALESCE(SUM(u.total_tokens),0) t,
                       COALESCE(SUM(u.cost),0) c
                FROM llm_usage u
                LEFT JOIN chat_sessions s ON s.session_id = u.session_id
                WHERE {window[0]}
                GROUP BY u.session_id
                ORDER BY t DESC
                LIMIT {limit}""",
            window[1],
        )

        out: list[str] = []
        for session_id, name, group_id, user_id, tokens, cost in rows:
            if not session_id:
                label = "系统开销"
            elif group_id:
                label = f"群 · {name or group_id}"
            elif user_id:
                label = f"私聊 · {name or user_id}"
            else:
                label = f"未知 · {str(session_id)[:6]}"
            out.append(f"· {label} — {_fmt_tokens(tokens)} / {_fmt_money(cost)}")
        return out

    def _model_lines(self, window: tuple[str, tuple[Any, ...]]) -> list[str]:
        """按渠道归并排行：改名前后的旧记录归并进同一渠道，一行一个渠道。"""
        limit = max(1, min(20, int(self.config.report.top_models)))
        rows = self._query(
            f"""SELECT model_api_provider_name,
                       COALESCE(NULLIF(model_assign_name,''), model_name),
                       COALESCE(SUM(total_tokens),0) t,
                       COALESCE(SUM(cost),0) c,
                       COUNT(*)
                FROM llm_usage
                WHERE {window[0]}
                GROUP BY 1, 2
                ORDER BY t DESC""",
            window[1],
        )

        grouped: dict[str, dict[str, Any]] = {}
        for provider, channel, tokens, cost, count in rows:
            channel_name = _CHANNEL_OF.get(provider, provider)
            slot = grouped.setdefault(channel_name, {"tokens": 0, "cost": 0.0, "uses": {}})
            slot["tokens"] += tokens
            slot["cost"] += cost
            use = _MODEL_LABELS.get(channel, channel)
            slot["uses"][use] = slot["uses"].get(use, 0) + tokens

        ordered = sorted(grouped.items(), key=lambda kv: kv[1]["tokens"], reverse=True)[:limit]
        lines = []
        for channel_name, slot in ordered:
            uses = " / ".join(
                f"{use} {_fmt_tokens(tok)}"
                for use, tok in sorted(slot["uses"].items(), key=lambda kv: kv[1], reverse=True)
            )
            lines.append(f"· {channel_name} — {_fmt_tokens(slot['tokens'])} / {_fmt_money(slot['cost'])}")
            lines.append(f"    {uses}")
        return lines

    # ---------- 状态 ----------

    def _state_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "sent_days.json"

    async def _load_sent_days(self) -> set[str]:
        async with self._state_lock:
            try:
                raw = await asyncio.to_thread(self._state_path().read_text, "utf-8")
                data = json.loads(raw)
            except (OSError, ValueError):
                return set()
            days = data.get("sent_days") if isinstance(data, dict) else None
            return {str(item) for item in days} if isinstance(days, list) else set()

    async def _save_sent_days(self, keys: set[str]) -> None:
        async with self._state_lock:
            trimmed = sorted(keys)[-_KEEP_DAYS:]
            payload = json.dumps({"sent_days": trimmed}, ensure_ascii=False)
            await asyncio.to_thread(self._write_state, payload)

    def _write_state(self, payload: str) -> None:
        """先写临时文件再替换，断电也不会留下半个 json。"""
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(payload, "utf-8")
            tmp.replace(path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def create_plugin() -> DailyReportPlugin:
    """创建插件实例。"""
    return DailyReportPlugin()
