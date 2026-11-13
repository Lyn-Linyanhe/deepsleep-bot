"""Replay the real reply execution path with all network/send dependencies mocked."""
import ast
import os
import asyncio
from copy import deepcopy
import json
import logging
from pathlib import Path
import re
import traceback
import types
import unittest
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

ROOT = Path(os.environ.get("DEEPSLEEP_WORKSPACE") or Path(__file__).resolve().parents[1])
PATH = ROOT / "MaiBot/src/maisaka/builtin_tool/reply.py"
BAD = '等等呀\n<tool_call name="generate_image">\n{"prompt":"晚霞","profile":"illustration"}\n</tool_call>'


def load_functions():
    tree = ast.parse(PATH.read_text(encoding="utf-8-sig"))
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes.extend(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    nodes.extend(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("_") for t in n.targets))
    env = {"asyncio": asyncio, "deepcopy": deepcopy, "re": re, "json": json,
           "traceback": traceback, "logger": logging.getLogger("leak-test")}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(PATH), "exec"), env)
    return env


class LeakTests(unittest.IsolatedAsyncioTestCase):
    async def replay(self, response, hook_response=None, final_response=None):
        env = load_functions()
        completion = types.SimpleNamespace(response_text=response)
        result = types.SimpleNamespace(completion=completion, metrics=types.SimpleNamespace(extra={}),
                                       error_message="", monitor_detail=None)
        replyer = types.SimpleNamespace(generate_reply_with_context=AsyncMock(return_value=(True, result)))
        runtime = types.SimpleNamespace(
            chat_stream=types.SimpleNamespace(platform="qq"), _chat_history=[], session_id="s",
            log_prefix="[test]", find_source_message_by_id=lambda _: types.SimpleNamespace(message_id="m"),
            _update_stage_status=lambda *a:None)
        failure = lambda name, message, **kwargs: {"success": False, "content": message}
        ctx = types.SimpleNamespace(runtime=runtime, build_failure_result=failure,
                                    post_process_reply_message_items_async=AsyncMock(side_effect=RuntimeError("PAST_VALIDATION")))
        class Extension:
            def __init__(self, **kwargs): self.targets=[]; self.reply_id="r"
            async def prepare(self, params): pass
        env.update({
            "config_module": types.SimpleNamespace(global_config=types.SimpleNamespace(
                expression=types.SimpleNamespace(use_vector_expression=False),
                chat=types.SimpleNamespace(enable_reply_at=True),
                emoji=types.SimpleNamespace(use_new_send_logic=True),
                debug=types.SimpleNamespace(replyer_retro_prompt=False))),
            "ToolAvailabilityContext": lambda **kw:kw, "ReplyExtensionExecution": Extension,
            "replyer_manager": types.SimpleNamespace(get_replyer=lambda **kw:replyer),
            "_find_recent_reply_to_target": lambda *a:"",
            "build_reply_monitor_detail": lambda *a:None, "_build_monitor_metadata": lambda *a:{},
            "_invoke_before_post_process_hook": AsyncMock(return_value=(hook_response if hook_response is not None else response, {})),
        })
        if final_response is not None:
            ctx.post_process_reply_message_items_async = AsyncMock(return_value=[
                types.SimpleNamespace(sequence=final_response)])
            env["build_visible_text_from_sequence"] = lambda sequence:sequence
        outcome = await env["execute_reply"](ctx, {"msg_id":"m","reply_reference":"请回复"}, call_id="c")
        return outcome, ctx

    async def test_actual_generation_leak_rejected_before_split(self):
        result, ctx = await self.replay(BAD)
        self.assertFalse(result["success"])
        self.assertIn("Planner", result["content"])
        ctx.post_process_reply_message_items_async.assert_not_awaited()

    async def test_hook_cannot_reintroduce_protocol(self):
        result, ctx = await self.replay("等一下呀", hook_response=BAD)
        self.assertFalse(result["success"])
        ctx.post_process_reply_message_items_async.assert_not_awaited()

    async def test_truncated_protocol_rejected(self):
        result, ctx = await self.replay('<tool_call name="generate_image">\n{"prompt":"晚霞"')
        self.assertFalse(result["success"])
        ctx.post_process_reply_message_items_async.assert_not_awaited()

    async def test_postprocess_cannot_reintroduce_protocol(self):
        result, ctx = await self.replay("等一下呀", final_response=BAD)
        self.assertFalse(result["success"])
        self.assertIn("Planner", result["content"])

    async def test_internal_tags_and_case_variants(self):
        for text in ('</mall>', '<TOOL_CALL name="x">', '<function_call>', '<invoke name="x">'):
            result, ctx = await self.replay(text)
            self.assertFalse(result["success"])
            ctx.post_process_reply_message_items_async.assert_not_awaited()

    async def test_normal_text_reaches_postprocess(self):
        result, ctx = await self.replay("这片晚霞好好看呀")
        ctx.post_process_reply_message_items_async.assert_awaited_once()
        self.assertIn("PAST_VALIDATION", result["content"])


if __name__ == "__main__":
    unittest.main()
