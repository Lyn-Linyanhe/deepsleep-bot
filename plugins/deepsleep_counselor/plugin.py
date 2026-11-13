"""恋爱军师：把「狗头军师」知识库压缩成主题摘要，聊天中按需取用。

来源：github.com/powerycy/goutoujunshi（PolyForm Noncommercial 1.0.0）。
原库 43 篇 301KB，直接注入上下文不现实；这里预先压成 23 份主题摘要
（knowledge/*.md，首行「关键词: a|b|c」用于匹配），每次调用按话题现读
1-2 份 + 通用方法。摘要文件现读现用，改知识不用重启。

设计原则：
- Planner 只在对方明确求援时调用（触发纪律写在 tool description 与
  bot_config 的 behavior_style 里，后者可热重载调）。
- 返回的知识是参考不是发言稿，靠 instruction 约束回复口吻。
- 安全信号（家暴/自杀/跟踪等）强制置顶危机篇并附加求助指引。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
_GENERAL_FILE = "00-通用方法.md"

# 安全升级信号：命中就不当普通感情问题处理。
_CRISIS_PATTERN = re.compile(
    r"自杀|轻生|自残|想死|家暴|打我|暴力|威胁|跟踪|堵门|勒索|下药|强迫|侵犯|性骚扰|报警|求救|被骗|诈骗|私密影像"
)

_KEYWORD_LINE = re.compile(r"^关键词[:：]\s*(.+)$")
_KEYWORD_SPLIT = re.compile(r"[|｜,，;；、]")
_WHITESPACE = re.compile(r"\s+")

_HIT_SCORE = 3.0          # 关键词整词命中
_PARTIAL_SCORE = 1.5      # 词内 bigram 覆盖率 ≥ 1/3 时的部分得分
_TOPIC_THRESHOLD = 1.5    # 低于这个分不当作相关

_INSTRUCTION = (
    "以上是参考知识，不是发言稿。用小睡自己的口吻回复：像朋友聊天，不像顾问做报告；"
    "先把对方的情绪接住，再给判断；最多给一条能直接用的建议或话术，不要罗列框架术语，"
    "不要暴露「参考了知识库」。对方只是随口提到恋爱话题、没有求援时，不要主动上课。"
    "如果对方明显只想倾诉，就先听着，别急着给方案。"
)

_CRISIS_INSTRUCTION = (
    "检测到安全相关信号：这不是普通感情问题。先确认对方当下是否安全，"
    "再明确建议求助——暴力威胁打 110、医疗急症打 120、被骗尽快报警并保存记录；"
    "不要劝和、不要说「再忍忍」。"
)

_EMPTY_INSTRUCTION = (
    "知识库里没有找到高度匹配的参考，按你自己的判断回复，不要硬套框架；"
    "拿不准时就先问清情况或者只共情。"
)


def _normalize(text: Any) -> str:
    return _WHITESPACE.sub("", str(text or "")).lower()


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _load_topics() -> list[dict[str, Any]]:
    """现读 knowledge/：每个文件首行「关键词: a|b|c」。坏文件跳过不报错。"""
    topics: list[dict[str, Any]] = []
    try:
        paths = sorted(_KNOWLEDGE_DIR.glob("*.md"))
    except OSError:
        return topics
    for path in paths:
        if path.name == _GENERAL_FILE:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = raw.splitlines()
        match = _KEYWORD_LINE.match(lines[0].strip()) if lines else None
        if match is None:
            continue
        keywords = [_normalize(item) for item in _KEYWORD_SPLIT.split(match.group(1))]
        keywords = [item for item in keywords if item]
        if not keywords:
            continue
        topics.append({"name": path.stem, "path": path, "keywords": keywords, "text": raw})
    return topics


def _score_topic(query: str, query_bigrams: set[str], topic: dict[str, Any]) -> float:
    score = 0.0
    for keyword in topic["keywords"]:
        if keyword in query:
            score += _HIT_SCORE
            continue
        keyword_bigrams = _bigrams(keyword)
        if not keyword_bigrams:
            continue
        overlap = len(query_bigrams & keyword_bigrams) / len(keyword_bigrams)
        if overlap >= 1 / 3:
            score += _PARTIAL_SCORE * overlap
    return score


def _select_topics(query: str, topics: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    normalized = _normalize(query)
    if not normalized:
        return []
    query_bigrams = _bigrams(normalized)
    scored = sorted(
        ((_score_topic(normalized, query_bigrams, topic), topic) for topic in topics),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [topic for score, topic in scored if score >= _TOPIC_THRESHOLD][:limit]


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class CounselorSectionConfig(PluginConfigBase):
    """军师配置。"""

    __ui_label__ = "军师"
    __ui_icon__ = "heart"
    __ui_order__ = 1

    max_topics: int = Field(default=2, description="每次最多返回几个主题摘要（1-3）")
    max_inject_chars: int = Field(default=4200, description="注入正文总字数上限；放不下整份就跳过，不截断半份")


class CounselorConfig(PluginConfigBase):
    """恋爱军师配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    counselor: CounselorSectionConfig = Field(default_factory=CounselorSectionConfig)


class CounselorPlugin(MaiBotPlugin):
    """被求援恋爱/社交问题时提供压缩后的参考知识。"""

    config_model = CounselorConfig

    async def on_load(self) -> None:
        count = await asyncio.to_thread(lambda: len(_load_topics()))
        self.ctx.logger.info("恋爱军师知识库已加载: 主题=%d", count)

    async def on_unload(self) -> None:
        self.ctx.logger.info("恋爱军师已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            self.ctx.logger.info("恋爱军师配置已更新")

    @Tool(
        "relationship_counsel",
        description=(
            "私聊里对方明确请你出主意时调用：怎么回消息、要不要主动、怎么约见面、"
            "吵架了想怎么说开、被冷落、想分手、失恋、被 PUA、怎么拒绝别人、"
            "让你分析聊天记录。返回该话题的实战参考。只是闲聊提到恋爱话题、"
            "没有明确求助时不要调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="topic",
                param_type=ToolParamType.STRING,
                description="对方的问题概要：几句话写清处境和 TA 想要什么（怎么回/该不该/怎么办）",
                required=True,
            ),
        ],
        platforms=["qq"],
        chat_scope="private",
    )
    async def handle_relationship_counsel(self, topic: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs  # 知识库不依赖会话上下文，上下文在对话里本来就有
        query = str(topic or "").strip()
        try:
            picks = await asyncio.to_thread(self._pick_topics, query)
            general = await asyncio.to_thread(self._read_general)
            body = self._compose(picks, general)
        except Exception:
            self.ctx.logger.warning("恋爱军师知识加载失败", exc_info=True)
            return {"success": False, "error": "参考知识读取失败，这次没加载到内容。"}

        crisis = bool(_CRISIS_PATTERN.search(query))
        if crisis:
            instruction = _CRISIS_INSTRUCTION + _INSTRUCTION
        elif not picks:
            instruction = _EMPTY_INSTRUCTION + _INSTRUCTION
        else:
            instruction = _INSTRUCTION
        names = [item["name"] for item in picks]
        self.ctx.logger.info(
            "恋爱军师参考: topics=%s crisis=%s chars=%d", names, crisis, len(body)
        )
        result: dict[str, Any] = {
            "success": True,
            "content": body,
            "instruction": instruction,
            "topics": names,
        }
        if crisis:
            result["crisis"] = True
        return result

    def _pick_topics(self, query: str) -> list[dict[str, Any]]:
        limit = int(self.config.counselor.max_topics or 1)
        limit = max(1, min(limit, 3))
        topics = _load_topics()
        picks = _select_topics(query, topics, limit)
        if _CRISIS_PATTERN.search(query):
            crisis_topic = next((item for item in topics if "危机" in item["name"]), None)
            if crisis_topic is not None and crisis_topic not in picks:
                picks = [crisis_topic] + picks[: max(0, limit - 1)]
        return picks

    def _read_general(self) -> str:
        try:
            return (_KNOWLEDGE_DIR / _GENERAL_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def _compose(self, picks: list[dict[str, Any]], general: str) -> str:
        cap = int(self.config.counselor.max_inject_chars or 0)
        if cap <= 0:
            cap = 100_000
        chosen: list[str] = []
        used = 0
        for item in [*picks, general]:
            text = (item["text"] if isinstance(item, dict) else item).strip()
            if not text:
                continue
            extra = len(text) + (2 if chosen else 0)
            if used + extra > cap:
                continue  # 放不下就整份跳过——边界警告都在文件末尾，不允许截断半份
            chosen.append(text)
            used += extra
        return "\n\n---\n\n".join(chosen)


def create_plugin() -> CounselorPlugin:
    """插件加载器要求的工厂函数。"""
    return CounselorPlugin()
