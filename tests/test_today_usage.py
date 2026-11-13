"""Offline tests; no model calls, QQ traffic or production database writes."""
import ast
import os
from contextlib import closing
import asyncio
import importlib.util
import json
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(os.environ.get("DEEPSLEEP_WORKSPACE") or Path(__file__).resolve().parents[1])
PLUGIN_PATH = ROOT / "MaiBot/plugins/deepsleep_daily_report/plugin.py"
spec = importlib.util.spec_from_file_location("daily_report_under_test", PLUGIN_PATH)
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 12, 14, 30)


def trusted(user=None, group=False):
    user = user or plugin_module._OWNER_QQ
    return {"user_id": user, "platform": "qq", "session_id": "real-session",
            "group_id": "200000000" if group else "", "is_group_chat": group}


def host_payload(arguments, context):
    path = ROOT / "MaiBot/src/plugin_runtime/component_query.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ComponentQueryService")
    methods = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in
               ("_build_tool_context_payload", "_build_tool_invocation_payload")]
    cls = ast.ClassDef(name="ComponentQueryService", bases=[], keywords=[], body=methods, decorator_list=[])
    namespace = {}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    entry = types.SimpleNamespace(plugin_id="deepsleep.daily-report", name="query_today_usage", invoke_method="plugin.invoke_tool")
    invocation = types.SimpleNamespace(arguments=arguments, stream_id="forged", reasoning="")
    return namespace["ComponentQueryService"]._build_tool_invocation_payload(entry, invocation, context)


class UsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "out", prefix="usage_test_")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "ledger.db"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript("""
                CREATE TABLE llm_usage (timestamp TEXT,session_id TEXT,task_name TEXT,
                  request_type TEXT,model_api_provider_name TEXT,prompt_tokens INTEGER,
                  completion_tokens INTEGER,total_tokens INTEGER,prompt_cache_hit_tokens INTEGER,cost REAL);
                CREATE TABLE chat_sessions(session_id TEXT,group_id TEXT,user_id TEXT,
                  group_name TEXT,user_nickname TEXT);
                INSERT INTO chat_sessions VALUES('math','200000000',NULL,'测试群（临时）',NULL);
                INSERT INTO chat_sessions VALUES('private',NULL,'100000000',NULL,'测试用户');
            """)
            c.execute("UPDATE chat_sessions SET user_id=? WHERE session_id='private'", (plugin_module._OWNER_QQ,))
            rows = [
                ("2026-11-12 10:00:00", "math", "planner", "maisaka.planner", "geili-flex", 100, 20, 120, 50, .2),
                ("2026-11-12 11:00:00", "private", "replyer", "maisaka.replyer", "geili-flex", 100, 20, 120, 0, .1),
                ("2026-11-12 12:00:00", "", "memory", "A_Memorix.person_fact_writeback", "free", 100, 20, 120, 0, 0),
                ("2026-11-12 14:31:00", "math", "planner", "maisaka.planner", "geili-flex", 100, 20, 120, 0, 9),
                ("2026-11-11 23:59:00", "math", "planner", "maisaka.planner", "geili-flex", 100, 20, 120, 0, 9),
            ]
            c.executemany("INSERT INTO llm_usage VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        self.plugin = plugin_module.DailyReportPlugin()
        self.plugin.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "data": {"db_path": str(self.db),
            "image_quota_db": str(Path(self.temp.name) / "missing-image.db")}})
        self.plugin._ctx = types.SimpleNamespace(logger=logging.getLogger("usage-test"))
        self.plugin._resolve_stream_id = AsyncMock(return_value="owner-private")
        self.plugin._ctx.send = types.SimpleNamespace(text=AsyncMock(return_value=True))
        self.clock_patch = patch.object(plugin_module, "datetime", Clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    async def test_owner_private_uses_actual_bounded_ledger(self):
        before = self.db.read_bytes()
        result = await self.plugin.handle_query_today_usage(_usage_context=trusted())
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["usage"]["llm_cost"], .3)
        self.assertEqual(result["usage"]["total_tokens"], 360)
        self.assertEqual(result["usage"]["last_record"], "2026-11-12 12:00:00")
        self.assertIn("14:30", result["content"])
        self.assertIn("估算", result["content"])
        self.plugin._ctx.send.text.assert_not_awaited()
        self.assertEqual(before, self.db.read_bytes())

    async def test_group_report_private_only(self):
        result = await self.plugin.handle_query_today_usage(detailed=True, _usage_context=trusted(group=True))
        self.assertTrue(result["success"])
        self.assertNotIn("usage", result)
        self.assertNotIn("0.3", json.dumps(result))
        self.assertNotIn("测试群", json.dumps(result, ensure_ascii=False))
        self.plugin._resolve_stream_id.assert_awaited_once_with(plugin_module._OWNER_QQ)
        args = self.plugin._ctx.send.text.await_args.args
        self.assertEqual(args[1], "owner-private")
        self.assertIn("截至", args[0])

    async def test_nonowner_missing_scope_and_spoof_denied(self):
        invalid = [None, {}, trusted("attacker"), {**trusted(), "platform": "other"},
                   {k:v for k,v in trusted().items() if k != "is_group_chat"},
                   {**trusted(), "session_id": ""},
                   {**trusted(), "is_group_chat": False, "group_id": "123"}]
        for context in invalid:
            result = await self.plugin.handle_query_today_usage(user_id=plugin_module._OWNER_QQ, _usage_context=context)
            self.assertFalse(result["success"])
            self.assertNotIn("usage", result)
        self.plugin._ctx.send.text.assert_not_awaited()

    async def test_group_send_failure_reveals_no_figures(self):
        self.plugin._ctx.send.text.return_value = False
        result = await self.plugin.handle_query_today_usage(_usage_context=trusted(group=True))
        self.assertFalse(result["success"])
        self.assertNotIn("usage", result)
        self.assertNotIn("0.3", json.dumps(result))

    async def test_group_name_filter_and_unknown_group(self):
        result = await self.plugin.handle_query_today_usage(group_name="测试群", _usage_context=trusted())
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["usage"]["llm_cost"], .2)
        self.assertEqual(result["usage"]["total_tokens"], 120)
        result = await self.plugin.handle_query_today_usage(group_name="不存在", _usage_context=trusted())
        self.assertFalse(result["success"])
        self.assertNotIn("usage", result)

    async def test_missing_database_is_error_not_zero(self):
        self.db.unlink()
        result = await self.plugin.handle_query_today_usage(_usage_context=trusted())
        self.assertFalse(result["success"])
        self.assertNotIn("usage", result)
        self.assertNotIn(str(self.db), json.dumps(result))

    async def test_host_overwrites_forged_identity_and_fails_closed(self):
        context = types.SimpleNamespace(stream_id="actual", session_id="actual", group_id="", user_id="attacker",
                                        platform="qq", is_group_chat=False, metadata={})
        payload = host_payload({"_usage_context": trusted(), "user_id": plugin_module._OWNER_QQ}, context)
        result = await self.plugin.handle_query_today_usage(**payload)
        self.assertFalse(result["success"])
        payload = host_payload({"_usage_context": trusted()}, None)
        result = await self.plugin.handle_query_today_usage(**payload)
        self.assertFalse(result["success"])

    async def test_detail_shape_unavailable_images_and_invalid_flag(self):
        result = await self.plugin.handle_query_today_usage(_usage_context=trusted())
        self.assertFalse(result["usage"]["image_available"])
        self.assertIsNone(result["usage"]["estimated_total_cost"])
        self.assertNotIn("渠道：", result["content"])
        result = await self.plugin.handle_query_today_usage(detailed=True, _usage_context=trusted())
        self.assertIn("渠道：", result["content"])
        result = await self.plugin.handle_query_today_usage(detailed="false", _usage_context=trusted())
        self.assertFalse(result["success"])

    async def test_ambiguous_group_and_exact_now_exclusion(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO chat_sessions VALUES('math2','another-group',NULL,'另一个测试群',NULL)")
            c.execute("INSERT INTO llm_usage VALUES('2026-11-12 14:30:00','math','planner','maisaka.planner','free',1,1,2,0,99)")
        result = await self.plugin.handle_query_today_usage(group_name="测试群", _usage_context=trusted())
        self.assertFalse(result["success"])
        result = await self.plugin.handle_query_today_usage(_usage_context=trusted())
        self.assertAlmostEqual(result["usage"]["llm_cost"], .3)

    async def test_existing_daily_report_unchanged(self):
        before = ast.parse(PLUGIN_PATH.with_name("plugin.py.bak_20261112_usage").read_text(encoding="utf-8"))
        after = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
        def methods(tree):
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DailyReportPlugin")
            return {n.name: ast.dump(n) for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        old, new = methods(before), methods(after)
        for name, node in old.items():
            self.assertEqual(node, new[name], name)

    async def test_empty_ledger_and_image_quota(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("DELETE FROM llm_usage")
        result = await self.plugin.handle_query_today_usage(_usage_context=trusted())
        self.assertTrue(result["success"])
        self.assertEqual(result["usage"]["llm_cost"], 0)
        self.assertIsNone(result["usage"]["last_record"])
        image = Path(self.temp.name) / "images.db"
        with closing(sqlite3.connect(image)) as c, c:
            c.execute("CREATE TABLE daily_usage(day TEXT,used INTEGER)")
            c.execute("INSERT INTO daily_usage VALUES('2026-11-12',2)")
        self.plugin.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "data": {"db_path": str(self.db), "image_quota_db": str(image)}})
        result = await self.plugin.handle_query_today_usage(_usage_context=trusted())
        self.assertEqual(result["usage"]["image_count"], 2)
        self.assertAlmostEqual(result["usage"]["image_cost"], .06)


if __name__ == "__main__":
    unittest.main()
