"""表达反馈闭环：把群友对她具体回复的反应回写到表达库的 count。

她每次 reply 成功发送后，这里记录「她发出的消息 id → 本次选用的表达 id」。
之后：
- 有人 reply 引用她的消息说出明显负面的话（闭嘴/好吵/烦…）→ 那批表达 count-1
  （下限 1，不删行）；
- QQ 表情回应通知命中她的消息 → count+1（上限 500）。
count 同时驱动快模式抽样的权重——这就是"被嫌弃的说法逐渐少说"的确定性实现。
全部规则无 LLM；表情回应通知由适配器折叠成文本进同一入站管线，这里直接解析。
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

from src.common.database.database import get_db_session
from src.common.database.database_model import Expression
from src.common.logger import get_logger

logger = get_logger("expression_feedback")

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "expression_feedback" / "feedback.sqlite3"
_MAPPING_TTL_DAYS = 3.0
_COUNT_FLOOR = 1
_COUNT_CEILING = 500
# 负面判定刻意保守：只认明确的嫌弃用语，且必须 reply 引用她的消息，避免误伤。
_NEGATIVE_PATTERN = re.compile(
    r"闭嘴|别说了|别说话|好吵|太吵|吵死|吵死了|烦不烦|烦死了|给我闭嘴|滚滚滚|禁言|别发了|消停点|聒噪|别吵"
)
_REACTION_PREFIX = "[事件-群消息表情回应]"
_REACTION_ID_PATTERN = re.compile(r"对消息\(ID:([-0-9A-Za-z_]+)\)")


def _text_bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return {cleaned[i: i + 2] for i in range(len(cleaned) - 1)}


class ExpressionFeedbackService:
    """表达使用映射记录 + 会话级反馈回写。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH

    # ---------- 记录（发出侧调用） ----------

    def record_reply(self, session_id: str, sent_message_ids: List[str], expression_ids: List[int]) -> None:
        """成功发送后记录映射；没有选用表达就没有可反馈对象，跳过。"""
        clean_ids = [str(item or "").strip() for item in (sent_message_ids or []) if str(item or "").strip()]
        clean_expressions = [int(item) for item in (expression_ids or []) if isinstance(item, int)]
        if not clean_ids or not clean_expressions:
            return
        now = time.time()
        con = self._connect()
        try:
            with con:
                con.execute(
                    """CREATE TABLE IF NOT EXISTS sent_map (
                           message_id TEXT PRIMARY KEY,
                           session_id TEXT NOT NULL,
                           expression_ids TEXT NOT NULL,
                           created_at REAL NOT NULL)"""
                )
                for message_id in clean_ids:
                    con.execute(
                        "INSERT OR IGNORE INTO sent_map(message_id, session_id, expression_ids, created_at) "
                        "VALUES(?,?,?,?)",
                        (message_id, session_id, json.dumps(clean_expressions), now),
                    )
                con.execute("DELETE FROM sent_map WHERE created_at < ?", (now - _MAPPING_TTL_DAYS * 86400,))
        finally:
            con.close()

    # ---------- 反馈（入站侧调用） ----------

    async def observe_incoming(self, message: Any) -> Dict[str, Any] | None:
        """对每条入站消息做确定性判定；命中映射才动 count。"""
        try:
            outcome = await asyncio.to_thread(self._observe_sync, message)
        except Exception as exc:
            logger.warning(f"表达反馈观察失败(不影响消息流): {exc}")
            return None
        if not outcome:
            return None
        # 向量索引是派生数据；在工作线程里没有事件循环，调度必须在异步侧完成。
        index_updates = outcome.pop("index_updates", [])
        if index_updates:
            self._schedule_index_sync(index_updates)
        return outcome

    def _observe_sync(self, message: Any) -> Dict[str, Any] | None:
        text = str(getattr(message, "processed_plain_text", "") or "").strip()
        reply_to = str(getattr(message, "reply_to", "") or "").strip()
        session_id = str(getattr(message, "session_id", "") or "").strip()
        message_id = str(getattr(message, "message_id", "") or "").strip()
        if not session_id or not message_id:
            return None
        info = getattr(message, "message_info", None)
        additional = getattr(info, "additional_config", None) or {}
        is_reaction = bool(getattr(message, "is_notify", False)) and additional.get("napcat_notice_type") == "group_msg_emoji_like"
        if is_reaction:
            payload = additional.get("napcat_notice_payload")
            if not isinstance(payload, dict) or not payload.get("message_id") or not payload.get("user_id"):
                return None
            target_ids = [str(payload["message_id"])]
            # 同一人对同一消息的回应只计一次，不以随机通知包装 ID 去重。
            event_key = "reaction:" + str(payload["user_id"]) + ":" + target_ids[0]
        elif reply_to and not getattr(message, "is_notify", False):
            if not _NEGATIVE_PATTERN.search(text):
                return None
            target_ids = [reply_to]
            event_key = "reply:" + message_id
        else:
            return None

        mappings = self._lookup(target_ids, session_id)
        if not mappings:
            return None
        expression_ids = sorted({int(item) for _, ids in mappings for item in ids})
        # 先持久认领：并发/重启不重复加权；中途崩溃宁可漏一次弱反馈，不重放。
        if not self._claim_event(session_id, event_key):
            return None

        if is_reaction:
            outcome, index_updates = self._apply_count_delta(expression_ids, +1)
            kind = "正反馈(表情回应)"
        else:
            if not _NEGATIVE_PATTERN.search(text):
                return None
            outcome, index_updates = self._apply_count_delta(expression_ids, -1)
            kind = "负反馈(被嫌弃)"
        if not outcome:
            return None
        logger.info(f"表达反馈生效: {kind} 表达数={len(outcome)} 明细={outcome}")
        return {"kind": kind, "updated": outcome, "index_updates": index_updates}

    # ---------- 内部 ----------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self._db_path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _claim_event(self, session_id: str, event_key: str) -> bool:
        con = self._connect()
        try:
            with con:
                con.execute("CREATE TABLE IF NOT EXISTS feedback_events (session_id TEXT, event_key TEXT, created_at REAL, PRIMARY KEY(session_id,event_key))")
                con.execute("DELETE FROM feedback_events WHERE created_at < ?", (time.time() - _MAPPING_TTL_DAYS * 86400,))
                return con.execute(
                    "INSERT OR IGNORE INTO feedback_events VALUES(?,?,?)",
                    (session_id, event_key, time.time()),
                ).rowcount == 1
        finally:
            con.close()

    def _lookup(self, target_ids: List[str], session_id: str) -> List[tuple[str, List[int]]]:
        cutoff = time.time() - _MAPPING_TTL_DAYS * 86400
        placeholders = ",".join("?" for _ in target_ids)
        con = self._connect()
        try:
            with con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS sent_map ("
                    "message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                    "expression_ids TEXT NOT NULL, created_at REAL NOT NULL)"
                )
                rows = con.execute(
                    f"SELECT message_id, expression_ids FROM sent_map "
                    f"WHERE message_id IN ({placeholders}) AND session_id = ? AND created_at >= ?",
                    (*target_ids, session_id, cutoff),
                ).fetchall()
        finally:
            con.close()
        parsed: List[tuple[str, List[int]]] = []
        for message_id, raw in rows:
            try:
                ids = [int(item) for item in json.loads(raw)]
            except (ValueError, TypeError):
                continue
            if ids:
                parsed.append((message_id, ids))
        return parsed

    def _apply_count_delta(self, expression_ids: List[int], delta: int) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """对表达库行做有界的 count 增减；返回 (明细, 待同步的向量索引条目)。"""
        outcome: List[Dict[str, Any]] = []
        index_updates: List[Dict[str, Any]] = []
        try:
            from sqlmodel import select

            from src.config.config import global_config
            use_vector = bool(global_config.expression.use_vector_expression)
        except Exception:
            logger.exception("表达反馈依赖导入失败，跳过本次回写")
            return outcome, index_updates

        with get_db_session() as session:
            for expression_id in expression_ids:
                row = session.exec(
                    select(Expression).where(Expression.id == expression_id)  # type: ignore[attr-defined]
                ).first()
                if row is None:
                    continue
                old_count = int(row.count or 0)
                new_count = max(_COUNT_FLOOR, min(_COUNT_CEILING, old_count + delta))
                if new_count == old_count:
                    continue
                row.count = new_count
                session.add(row)
                outcome.append({"id": expression_id, "old": old_count, "new": new_count})
                index_updates.append(
                    {
                        "id": expression_id,
                        "situation": row.situation,
                        "style": row.style,
                        "count": new_count,
                        "session_id": row.session_id,
                        "checked": row.checked,
                        "modified_by": row.modified_by,
                    }
                )

        if not use_vector:
            index_updates = []
        return outcome, index_updates

    def _schedule_index_sync(self, index_updates: List[Dict[str, Any]]) -> None:
        """把 count 变更同步进表达向量索引（派生数据，失败只记日志）。"""
        try:
            from src.chat.replyer.expression_vector_index import (
                ExpressionVectorIndexUpsertItem,
                expression_vector_index,
            )
            from src.config.config import global_config

            items = [
                ExpressionVectorIndexUpsertItem(
                    id=item["id"],
                    situation=item["situation"],
                    style=item["style"],
                    count=item["count"],
                    session_id=item["session_id"],
                    checked=item["checked"],
                    modified_by=item["modified_by"],
                )
                for item in index_updates
            ]
            asyncio.get_running_loop().create_task(
                expression_vector_index.upsert_expressions(
                    index_path=global_config.expression.expression_vector_index_path,
                    expressions=items,
                )
            )
        except Exception:
            logger.exception("表达反馈后的向量索引同步失败")


def select_expressions_by_ids(expression_ids: List[int]):
    """保留给外部调试的查询构造；主流程直接在 _apply_count_delta 内查询。"""
    from sqlmodel import select

    return select(Expression).where(Expression.id.in_(expression_ids))  # type: ignore[attr-defined]


expression_feedback_service = ExpressionFeedbackService()
