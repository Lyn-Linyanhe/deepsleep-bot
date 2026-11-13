"""说话方式注入器：让她「自己说得更好」，不是当军师。

用户意图（2026-11-13 二次拍板）：不要工具化的「求援→给建议」，而是要她在
日常聊天里自然地更会说话——接话接得住、夸人夸得具体、会救场、会接情绪。

机制：挂在 maisaka.replyer.before_model_request 钩子上，每次她即将开口时
用规则（零 LLM）判断当前处境（报喜/倾诉/吐槽/分享/冷场/逗嘴/求帮忙/道歉…），
命中就把一张「说话小提示」卡追加进本次请求的 system item：
- 卡片是提醒不是台词，明确要求她保持自己的口吻和长度、别照念；
- 冷却机制（同卡 15 分钟、全局 90 秒、每小时上限）防止每次都说成模板；
- 卡片知识来源：狗头军师 knowledge 摘要 → 提炼成她自己的视角（cards/*.md，现读现用）。

代价：命中时才加 100~200 token，无额外模型调用。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

_CARDS_DIR = Path(__file__).resolve().parent / "cards"
_MARKER = "【说话小提示"
_KEYWORD_LINE = re.compile(r"^关键词[:：]\s*(.+)$")
_SITUATION_LINE = re.compile(r"^情况[:：]\s*(.+)$")
_TIP_LINE = re.compile(r"^提示[:：]\s*(.+)$")
_KEYWORD_SPLIT = re.compile(r"[|｜]")
_WHITESPACE = re.compile(r"\s+")

# 一个字回复的判定集合（冷场信号）
_SHORT_REPLY_MAX_CHARS = 4
_SHORT_REPLY_SET = {
    "嗯", "哦", "啊", "哈", "行", "好", "哦哦", "嗯嗯", "还行", "随便",
    "不知道", "笑死", "确实", "是吧", "好的", "恩恩", "噢", "喔",
}

_INSTRUCTION_SUFFIX = (
    "（这是提醒不是台词：用不用、怎么说你自己判断，保持你自己的口吻和长度，别照念。）"
)

# 会话类型判定：入站消息里 group_info 非空=群聊，否则私聊（与主程序判定一致）；
# session_id 形态（qq_group_xxx / qq_private_xxx）作兜底。
_GROUP_SESSION_HINTS = ("_group_", "group_")
_PRIVATE_SESSION_HINTS = ("_private_", "private_")


def _classify_session_id(session_id: str) -> str:
    sid = str(session_id or "").strip().lower()
    if not sid:
        return ""
    if any(hint in sid for hint in _GROUP_SESSION_HINTS):
        return "group"
    if any(hint in sid for hint in _PRIVATE_SESSION_HINTS):
        return "private"
    return ""



def _normalize(text: Any) -> str:
    return _WHITESPACE.sub("", str(text or ""))


def _load_cards() -> list[dict[str, Any]]:
    """现读 cards/*.md：关键词行支持 `!` 前缀表示强信号（权重 2）。"""
    cards: list[dict[str, Any]] = []
    try:
        paths = sorted(_CARDS_DIR.glob("*.md"))
    except OSError:
        return cards
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        keywords: list[tuple[str, float, bool]] = []  # (pattern, weight, anchored)
        situation = ""
        tip = ""
        for line in raw.splitlines():
            stripped = line.strip()
            match = _KEYWORD_LINE.match(stripped)
            if match:
                for token in _KEYWORD_SPLIT.split(match.group(1)):
                    token = _normalize(token)
                    if not token:
                        continue
                    weight = 1.0
                    if token.startswith("!"):
                        weight = 2.0
                        token = token[1:]
                    anchored = token.startswith("^") and token.endswith("$")
                    if anchored:
                        token = token.strip("^$")
                    if token:
                        keywords.append((token, weight, anchored))
                continue
            match = _SITUATION_LINE.match(stripped)
            if match:
                situation = match.group(1).strip()
                continue
            match = _TIP_LINE.match(stripped)
            if match:
                tip = match.group(1).strip()
        if keywords and tip:
            cards.append(
                {"name": path.stem, "keywords": keywords, "situation": situation, "tip": tip}
            )
    return cards


def _collect_user_texts(items: Any) -> list[str]:
    """从 ContextItem 快照里按顺序取 user 消息的文本片段。"""
    texts: list[str] = []
    if not isinstance(items, list):
        return texts
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        pieces = [
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and str(part.get("type", "")).lower() == "text"
        ]
        joined = "\n".join(pieces).strip()
        if joined:
            texts.append(joined)
    return texts


def _score_card(joined_text: str, last_text: str, card: dict[str, Any]) -> float:
    score = 0.0
    for pattern, weight, anchored in card["keywords"]:
        target = last_text if anchored else joined_text
        if pattern and pattern in target:
            score += weight
    return score


def _pick_card(
    items: Any, cards: list[dict[str, Any]], min_score: float
) -> tuple[dict[str, Any], float] | None:
    texts = _collect_user_texts(items)
    if not texts:
        return None
    last_text = _normalize(texts[-1])
    joined = _normalize("\n".join(texts[-3:]))
    best: tuple[dict[str, Any], float] | None = None
    for card in cards:
        score = _score_card(joined, last_text, card)
        # 冷场特判：最后一条是单字/短回复，命中冷场卡加权
        if "冷场" in card["name"] and len(last_text) <= _SHORT_REPLY_MAX_CHARS:
            if last_text in _SHORT_REPLY_SET:
                score += 2.0
        if score < min_score:
            continue
        if best is None or score > best[1]:
            best = (card, score)
    return best


def _build_prompt(card: dict[str, Any]) -> str:
    subject = card["situation"] or card["name"]
    return f"{_MARKER}·{subject}】{card['tip']}{_INSTRUCTION_SUFFIX}"


def _inject_into_system(items: Any, prompt: str) -> list[dict[str, Any]] | None:
    """把提示追加到第一个 SystemMessageItem 的 parts；没有就在头部插入一条。"""
    if not isinstance(items, list):
        return None
    updated: list[dict[str, Any]] = []
    injected = False
    for item in items:
        if not injected and isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
            parts = item.get("parts")
            if isinstance(parts, list):
                item = dict(item)
                item["parts"] = [*parts, {"type": "text", "text": prompt}]
                injected = True
        updated.append(item)
    if not injected:
        updated.insert(0, {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": prompt}]})
    return updated


def _already_injected(items: Any) -> bool:
    markers = (_MARKER, "【对这个人放开撩", "【对男朋友放开撩")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict):
                text = str(part.get("text", ""))
                if any(marker in text for marker in markers):
                    return True
    return False


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class SpeechSectionConfig(PluginConfigBase):
    """说话提示配置。"""

    __ui_label__ = "说话提示"
    __ui_icon__ = "message-circle"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否注入说话提示")
    min_score: float = Field(default=2.0, description="命中阈值：关键词权重累计达到才注入（强信号 ! 记 2 分）")
    per_card_cooldown_seconds: int = Field(default=900, description="同一张卡两次注入的最小间隔（秒）")
    min_interval_seconds: int = Field(default=90, description="任意两张卡之间的全局最小间隔（秒）")
    max_per_hour: int = Field(default=10, description="每小时最多注入次数")
    # 私聊更积极：一对一聊天里说话质量体感最强，阈值和冷却都放宽（2026-11-13 用户拍板）
    private_min_score: float = Field(default=1.0, description="私聊命中阈值（单个关键词即可触发）")
    private_per_card_cooldown_seconds: int = Field(default=300, description="私聊同卡冷却（秒）")
    private_min_interval_seconds: int = Field(default=30, description="私聊全局最小间隔（秒）")
    private_max_per_hour: int = Field(default=30, description="私聊每小时最多注入次数")
    # 指定用户通道：命中这些 QQ 的会话注入固定指令（2026-11-13 用户拍板"小号放开撩"）
    special_user_ids: list[str] = Field(default_factory=list, description="对这些 QQ 号注入固定指令")
    special_directive: str = Field(default="", description="注入给指定用户的指令文本")
    # 主人通道：对主人（男朋友）另一套指令，措辞不能提"小号/测试"
    owner_user_ids: list[str] = Field(default_factory=list, description="主人 QQ 号")
    owner_directive: str = Field(default="", description="注入给主人的指令文本")


class SpeechConfig(PluginConfigBase):
    """说话方式注入器配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    speech: SpeechSectionConfig = Field(default_factory=SpeechSectionConfig)


class SpeechPlugin(MaiBotPlugin):
    """replyer 请求前按处境注入一张说话技巧卡。"""

    config_model = SpeechConfig

    def __init__(self) -> None:
        super().__init__()
        self._last_card_at: dict[str, float] = {}
        self._recent_injections: deque[float] = deque()
        self._session_types: dict[str, str] = {}
        self._session_users: dict[str, str] = {}

    async def on_load(self) -> None:
        count = await asyncio.to_thread(lambda: len(_load_cards()))
        self.ctx.logger.info("说话提示卡已加载: 张数=%d", count)

    async def on_unload(self) -> None:
        self.ctx.logger.info("说话提示已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            self.ctx.logger.info("说话提示配置已更新")

    # ---------- Hook：记录会话类型 ----------

    @HookHandler(
        "chat.receive.after_process",
        name="deepsleep_speech_capture_session",
        description="记录会话 ID 对应的聊天类型（群聊/私聊），供注入频率区分。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def capture_session_type(self, **kwargs: Any) -> dict[str, Any]:
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return {"action": "continue"}
        sid = str(message.get("session_id", "") or "").strip()
        chat_type = ""
        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            if message_info.get("group_info"):
                chat_type = "group"
            else:
                chat_type = "private"
            if not sid:
                extra = message_info.get("additional_config")
                if isinstance(extra, dict):
                    sid = str(extra.get("session_id") or extra.get("stream_id") or "").strip()
        if not chat_type:
            chat_type = _classify_session_id(sid)
        if sid and chat_type:
            self._remember_session(sid, chat_type)
        user_id = ""
        if isinstance(message_info, dict):
            user_info = message_info.get("user_info")
            if isinstance(user_info, dict):
                user_id = str(user_info.get("user_id") or "").strip()
        if not user_id:
            user_id = str(message.get("user_id") or "").strip()
        if sid and user_id:
            self._remember_session_user(sid, user_id)
        return {"action": "continue"}

    def _remember_session_user(self, sid: str, user_id: str) -> None:
        self._session_users[sid] = user_id
        if len(self._session_users) > 200:
            oldest = next(iter(self._session_users))
            self._session_users.pop(oldest, None)

    def _owner_directive_for(self, session_id: str) -> str:
        """命中主人通道时返回固定指令；否则空串。"""
        cfg = self.config.speech
        ids = [str(item).strip() for item in (getattr(cfg, "owner_user_ids", None) or []) if str(item).strip()]
        directive = str(getattr(cfg, "owner_directive", "") or "").strip()
        if not ids or not directive:
            return ""
        user_id = self._session_users.get(str(session_id or "").strip(), "")
        if user_id and user_id in ids:
            return directive
        return ""

    def _special_directive_for(self, session_id: str) -> str:
        """命中指定用户通道时返回固定指令；否则空串。"""
        cfg = self.config.speech
        ids = [str(item).strip() for item in (getattr(cfg, "special_user_ids", None) or []) if str(item).strip()]
        directive = str(getattr(cfg, "special_directive", "") or "").strip()
        if not ids or not directive:
            return ""
        user_id = self._session_users.get(str(session_id or "").strip(), "")
        if user_id and user_id in ids:
            return directive
        return ""

    def _remember_session(self, sid: str, chat_type: str) -> None:
        self._session_types[sid] = chat_type
        if len(self._session_types) > 200:  # 只留最近的，防无限增长
            oldest = next(iter(self._session_types))
            self._session_types.pop(oldest, None)

    def _policy_for(self, session_id: str) -> Any:
        """按会话类型给出 (min_score, 冷却参数)。未知类型按默认（群标椎）处理。"""
        import types as _types

        cfg = self.config.speech
        sid = str(session_id or "").strip()
        chat_type = self._session_types.get(sid) or _classify_session_id(sid)
        if chat_type == "private":
            return _types.SimpleNamespace(
                scope="private",
                min_score=float(cfg.private_min_score),
                per_card_cooldown_seconds=float(cfg.private_per_card_cooldown_seconds),
                min_interval_seconds=float(cfg.private_min_interval_seconds),
                max_per_hour=int(cfg.private_max_per_hour),
            )
        return _types.SimpleNamespace(
            scope=chat_type or "unknown",
            min_score=float(cfg.min_score),
            per_card_cooldown_seconds=float(cfg.per_card_cooldown_seconds),
            min_interval_seconds=float(cfg.min_interval_seconds),
            max_per_hour=int(cfg.max_per_hour),
        )

    # ---------- Hook：replyer 请求前注入 ----------

    @HookHandler(
        "maisaka.planner.before_request",
        name="deepsleep_speech_planner_directive",
        description="Planner 定调子，同样注入主人/指定用户通道指令，避免计划出'拒绝'路线。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_planner_directive(self, **kwargs: Any) -> dict[str, Any]:
        items = kwargs.get("items")
        if not isinstance(items, list) or _already_injected(items):
            return {"action": "continue"}
        sid = str(kwargs.get("session_id") or "")
        directive = self._owner_directive_for(sid) or self._special_directive_for(sid)
        if not directive:
            return {"action": "continue"}
        new_items = _inject_into_system(items, directive)
        if new_items is None:
            return {"action": "continue"}
        merged = dict(kwargs)
        merged["items"] = new_items
        return {"action": "continue", "modified_kwargs": merged}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="deepsleep_speech_inject",
        description="按当前处境给本次回复追加一条说话技巧提示。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_speech_card(self, **kwargs: Any) -> dict[str, Any]:
        items = kwargs.get("items")
        if not isinstance(items, list) or _already_injected(items):
            return {"action": "continue"}
        sid = str(kwargs.get("session_id") or "")
        # 指定用户通道优先：命中就注入固定指令，不参与卡片逻辑（主人通道优先于小号通道）
        directive = self._owner_directive_for(sid)
        kind = "owner"
        if not directive:
            directive = self._special_directive_for(sid)
            kind = "special"
        if directive:
            new_items = _inject_into_system(items, directive)
            if new_items is None:
                return {"action": "continue"}
            self.ctx.logger.info(
                "特殊用户指令注入: kind=%s user=%s chars=%d",
                kind, self._session_users.get(sid, "?"), len(directive),
            )
            merged = dict(kwargs)
            merged["items"] = new_items
            return {"action": "continue", "modified_kwargs": merged}
        if not self.config.speech.enabled:
            return {"action": "continue"}
        policy = self._policy_for(sid)
        try:
            cards = await asyncio.to_thread(_load_cards)
            pick = _pick_card(items, cards, policy.min_score)
        except Exception:
            self.ctx.logger.warning("说话提示匹配失败", exc_info=True)
            return {"action": "continue"}
        if pick is None:
            return {"action": "continue"}
        card, score = pick
        now = time.monotonic()
        if not self._cooldown_ok(card["name"], now, policy):
            self.ctx.logger.info("说话提示冷却跳过: card=%s scope=%s", card["name"], policy.scope)
            return {"action": "continue"}
        new_items = _inject_into_system(items, _build_prompt(card))
        if new_items is None:
            return {"action": "continue"}
        self._record(card["name"], now)
        self.ctx.logger.info(
            "说话提示注入: card=%s score=%.1f scope=%s", card["name"], score, policy.scope
        )
        merged = dict(kwargs)
        merged["items"] = new_items
        return {"action": "continue", "modified_kwargs": merged}

    # ---------- 冷却 ----------

    def _cooldown_ok(self, card_name: str, now: float, policy: Any = None) -> bool:
        policy = policy or self.config.speech
        last = self._last_card_at.get(card_name, 0.0)
        if now - last < float(policy.per_card_cooldown_seconds):
            return False
        if self._recent_injections and now - self._recent_injections[-1] < float(policy.min_interval_seconds):
            return False
        while self._recent_injections and now - self._recent_injections[0] > 3600.0:
            self._recent_injections.popleft()
        if len(self._recent_injections) >= int(policy.max_per_hour):
            return False
        return True

    def _record(self, card_name: str, now: float) -> None:
        self._last_card_at[card_name] = now
        self._recent_injections.append(now)
        while self._recent_injections and now - self._recent_injections[0] > 3600.0:
            self._recent_injections.popleft()


def create_plugin() -> SpeechPlugin:
    """插件加载器要求的工厂函数。"""
    return SpeechPlugin()
