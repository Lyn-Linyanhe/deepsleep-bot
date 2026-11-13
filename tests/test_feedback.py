"""Offline tests for expression feedback loop, contextual sampling and jargon threshold."""
import ast
import os
import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from typing import Any, List, Optional

ROOT = Path(os.environ.get("DEEPSLEEP_WORKSPACE") or Path(__file__).resolve().parents[1])
FB_PATH = ROOT / "MaiBot/src/learners/expression_feedback.py"
SEL_PATH = ROOT / "MaiBot/src/chat/replyer/maisaka_expression_selector.py"
JAR_PATH = ROOT / "MaiBot/src/learners/jargon_miner.py"


class _Col:
    def __eq__(self, other):
        return other


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def where(self, value):
        return FakeQuery([self.rows.get(value)] if value in self.rows else [])

    def first(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    def exec(self, query):
        return FakeQuery(self.rows).where(getattr(query, "_target_id", None))

    def add(self, row):
        self.added.append(row)


def install_stubs():
    class Expression:  # minimal stand-in
        id = _Col()

        def __init__(self, **kw):
            self.__dict__.update(kw)

    fake_sqlmodel = types.ModuleType("sqlmodel")
    fake_sqlmodel.select = lambda model: types.SimpleNamespace(where=lambda v: types.SimpleNamespace(_target_id=v))
    sys.modules.setdefault("sqlmodel", fake_sqlmodel)
    sys.modules.setdefault("src", types.ModuleType("src"))
    sys.modules.setdefault("src.common", types.ModuleType("src.common"))
    sys.modules.setdefault("src.common.database", types.ModuleType("src.common.database"))
    db_mod = types.ModuleType("src.common.database.database")

    @contextmanager
    def get_db_session():
        yield TESTING["session"]

    db_mod.get_db_session = get_db_session
    sys.modules["src.common.database.database"] = db_mod
    dm_mod = types.ModuleType("src.common.database.database_model")
    dm_mod.Expression = Expression
    sys.modules["src.common.database.database_model"] = dm_mod
    log_mod = types.ModuleType("src.common.logger")
    log_mod.get_logger = lambda name: types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                                             exception=lambda *a, **k: None, debug=lambda *a, **k: None)
    sys.modules["src.common.logger"] = log_mod
    cfg_mod = types.ModuleType("src.config.config")
    cfg_mod.global_config = types.SimpleNamespace(expression=types.SimpleNamespace(use_vector_expression=False))
    sys.modules["src.config.config"] = cfg_mod
    sys.modules.setdefault("src.chat", types.ModuleType("src.chat"))
    sys.modules.setdefault("src.chat.replyer", types.ModuleType("src.chat.replyer"))
    evi_mod = types.ModuleType("src.chat.replyer.expression_vector_index")
    evi_mod.expression_vector_index = types.SimpleNamespace(upsert_expressions=None)
    evi_mod.ExpressionVectorIndexUpsertItem = lambda **kw: kw
    sys.modules["src.chat.replyer.expression_vector_index"] = evi_mod
    return Expression


TESTING = {}


def load_feedback_module(db_path):
    spec = importlib.util.spec_from_file_location("expression_feedback_under_test", FB_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB_PATH = db_path
    return module


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_stubs()
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "out", prefix="fb_test_")
        self.addCleanup(self.temp.cleanup)
        self.module = load_feedback_module(Path(self.temp.name) / "feedback.sqlite3")
        self.service = self.module.ExpressionFeedbackService(Path(self.temp.name) / "feedback.sqlite3")

    def make_message(self, text="", reply_to=""):
        return types.SimpleNamespace(processed_plain_text=text, reply_to=reply_to)

    async def test_record_lookup_and_negative_penalize(self):
        TESTING["session"] = FakeSession({11: types.SimpleNamespace(id=11, count=5, situation="被催作业", style="敷衍",
                                                                   session_id="s", checked=False, modified_by="ai")})
        self.service.record_reply("s", ["10001", "10002"], [11, 12])
        result = await self.service.observe_incoming(self.make_message("闭嘴吧", reply_to="10001"))
        self.assertEqual(result["kind"], "负反馈(被嫌弃)")
        self.assertEqual(result["updated"][0]["new"], 4)
        self.assertEqual(TESTING["session"].added[0].count, 4)

    async def test_floor_one_and_positive_cap(self):
        TESTING["session"] = FakeSession({
            11: types.SimpleNamespace(id=11, count=1, situation="x", style="y", session_id="s", checked=False, modified_by=""),
            12: types.SimpleNamespace(id=12, count=499, situation="x", style="y", session_id="s", checked=False, modified_by=""),
        })
        self.service.record_reply("s", ["20001"], [11])
        result = await self.service.observe_incoming(self.make_message("别说了 行吗 烦死了", reply_to="20001"))
        self.assertIsNone(result)  # count 已在地板 1，无变化则不产生事件
        self.assertEqual(TESTING["session"].added, [])
        self.service.record_reply("s", ["20002"], [12])
        result = await self.service.observe_incoming(
            self.make_message("[事件-群消息表情回应] 甲 对消息(ID:20002)表达了 [赞]"))
        self.assertEqual(result["kind"], "正反馈(表情回应)")
        self.assertEqual(result["updated"][0]["new"], 500)  # cap

    async def test_neutral_and_foreign_reply_do_nothing(self):
        self.service.record_reply("s", ["30001"], [11])
        neutral = await self.service.observe_incoming(self.make_message("哈哈哈哈好", reply_to="30001"))
        self.assertIsNone(neutral)
        self.service.record_reply("s", ["30002"], [11])
        notice = await self.service.observe_incoming(
            self.make_message("[事件-群消息表情回应] 甲 对消息(ID:99999)表达了 [赞]"))
        self.assertIsNone(notice)
        none_reply = await self.service.observe_incoming(self.make_message("闭嘴", reply_to=""))
        self.assertIsNone(none_reply)

    async def test_mapping_ttl_lookup(self):
        self.service.record_reply("s", ["40001"], [11])
        con = self.service._connect()
        try:
            with con:
                con.execute("UPDATE sent_map SET created_at=?", (0.0,))
        finally:
            con.close()
        mappings = self.service._lookup(["40001"])
        self.assertEqual(mappings, [])


def load_selector_helpers():
    tree = ast.parse(SEL_PATH.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MaisakaExpressionSelector")
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            ast.ImportFrom(module="typing", names=[ast.alias(name="Any"), ast.alias(name="List"), ast.alias(name="Optional")], level=0),
            ast.Import(names=[ast.alias(name="re")])]
    for name in ("_sample_legacy_expression_candidates", "_contextual_overlap",
                 "_sample_contextual_expression_candidates"):
        body.append(next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name))
    body.append(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_text_bigrams"))
    body.extend(n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and (t.id.startswith("_CONTEXT") or t.id.startswith("_WORD"))
        for t in n.targets))
    env = {"logger": types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None),
           "weighted_sample": lambda items, n: list(items)[:n],
           "Any": Any, "List": List, "Optional": Optional}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SEL_PATH), "exec"), env)
    return env


class SelectorBoostTests(unittest.TestCase):
    def setUp(self):
        self.env = load_selector_helpers()

    def run_sample(self, candidates, query):
        legacy = self.env["_sample_legacy_expression_candidates"]
        fake_self = types.SimpleNamespace(
            _sample_legacy_expression_candidates=lambda all_candidates: legacy(None, all_candidates),
            _contextual_overlap=self.env["_contextual_overlap"],
            _sample_contextual_expression_candidates=self.env["_sample_contextual_expression_candidates"],
        )
        return self.env["_sample_contextual_expression_candidates"](fake_self, candidates, query)

    def test_relevant_candidate_boosted_into_pool(self):
        candidates = [dict(id=i, situation=f"话题{i}完全无关的内容{i}", style="说法", count=1) for i in range(20)]
        candidates[7]["situation"] = "被催写线代作业时赖皮撒娇"
        pool = self.run_sample(candidates, "回复参考：测试用户催我写线代作业，我想赖皮撒娇一下")
        self.assertIn(7, [c["id"] for c in pool])
        self.assertLessEqual(len(pool), 10)

    def test_irrelevant_query_keeps_legacy_pool(self):
        candidates = [dict(id=i, situation=f"话题{i}无关内容", style="说法", count=1) for i in range(20)]
        pool = self.run_sample(candidates, "回复参考：完全不相干的另一件事")
        legacy = self.env["_sample_legacy_expression_candidates"](None, candidates)
        self.assertEqual(pool, legacy)

    def test_small_pool_returns_legacy_empty(self):
        candidates = [dict(id=i, situation="x", style="y", count=1) for i in range(5)]
        self.assertEqual(self.run_sample(candidates, "催作业"), [])


class JargonThresholdTests(unittest.TestCase):
    def test_graduation_line_lowered(self):
        source = JAR_PATH.read_text(encoding="utf-8")
        self.assertIn("is_complete = (jargon_obj.count or 0) >= 20", source)
        self.assertNotIn(">= 100", source)


if __name__ == "__main__":
    unittest.main()
