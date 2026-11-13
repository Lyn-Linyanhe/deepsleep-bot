"""Offline tests: speech injector card loading / matching / system-item injection / cooldown."""
import ast
import os
import asyncio
import re
import time as time_module
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(os.environ.get("DEEPSLEEP_WORKSPACE") or Path(__file__).resolve().parents[1])
PLUGIN = ROOT / "MaiBot/plugins/deepsleep_speech/plugin.py"
CARDS = ROOT / "MaiBot/plugins/deepsleep_speech/cards"

FUNC_NAMES = [
    "_normalize", "_load_cards", "_collect_user_texts", "_score_card",
    "_pick_card", "_build_prompt", "_inject_into_system", "_already_injected",
    "_classify_session_id",
]
METHOD_NAMES = ["inject_speech_card", "inject_planner_directive", "_cooldown_ok", "_record", "_policy_for", "_remember_session", "capture_session_type", "_remember_session_user", "_special_directive_for", "_owner_directive_for"]


def load_env():
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8-sig"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(n for n in tree.body if isinstance(n, ast.Import))
    body.extend(
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id.startswith("_") for t in n.targets)
    )
    body.extend(
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNC_NAMES
    )
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SpeechPlugin")
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in METHOD_NAMES:
            node.decorator_list = []
            body.append(node)
    env = {
        "Any": Any, "Dict": dict, "List": list, "Path": Path, "re": re,
        "time": time_module, "types": types, "asyncio": asyncio,
        "deque": __import__("collections").deque,
        "__file__": str(PLUGIN),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(PLUGIN), "exec"), env)
    assert env["_CARDS_DIR"] == CARDS
    return env


def user_item(text):
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": text}]}


def system_item(text):
    return {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": text}]}


def make_plugin(env, **overrides):
    defaults = dict(
        enabled=True, min_score=2.0, per_card_cooldown_seconds=900,
        min_interval_seconds=90, max_per_hour=10,
        private_min_score=1.0, private_per_card_cooldown_seconds=300,
        private_min_interval_seconds=30, private_max_per_hour=30,
        special_user_ids=[], special_directive="",
        owner_user_ids=[], owner_directive="",
    )
    defaults.update(overrides)
    cfg = types.SimpleNamespace(speech=types.SimpleNamespace(**defaults))
    fake = types.SimpleNamespace(
        config=cfg,
        _last_card_at={},
        _recent_injections=__import__("collections").deque(),
        _session_types={},
        _session_users={},
        ctx=types.SimpleNamespace(logger=types.SimpleNamespace(info=lambda *a, **k: None,
                                                               warning=lambda *a, **k: None)),
    )
    for name in METHOD_NAMES:
        setattr(fake, name, types.MethodType(env[name], fake))
    return fake


class CardLoadTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()

    def test_all_cards_valid(self):
        cards = self.env["_load_cards"]()
        self.assertEqual(len(cards), 14, [c["name"] for c in cards])
        for card in cards:
            self.assertTrue(card["keywords"], card["name"])
            self.assertTrue(card["situation"], card["name"])
            self.assertGreater(len(card["tip"]), 30, card["name"])

    def test_strong_signal_weight(self):
        cards = {c["name"]: c for c in self.env["_load_cards"]()}
        weights = {kw: w for kw, w, _ in cards["01-报喜"]["keywords"]}
        self.assertEqual(weights["考上"], 2.0)   # !考上
        self.assertEqual(weights["终于"], 1.0)

    def test_anchored_keywords(self):
        cards = {c["name"]: c for c in self.env["_load_cards"]()}
        anchored = [kw for kw, _, anc in cards["05-冷场单字"]["keywords"] if anc]
        self.assertIn("嗯", anchored)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.cards = self.env["_load_cards"]()

    def pick(self, texts):
        items = [system_item("sys")] + [user_item(t) for t in texts]
        result = self.env["_pick_card"](items, self.cards, 2.0)
        return result[0]["name"] if result else None

    def test_good_news(self):
        self.assertEqual(self.pick(["我考上研究生啦！！"]), "01-报喜")

    def test_venting_beats_threshold(self):
        self.assertEqual(self.pick(["今天被老板骂了，好难受"]), "02-倾诉受挫")

    def test_short_reply_cold_scene(self):
        self.assertEqual(self.pick(["今天去看了那个展", "嗯"]), "05-冷场单字")

    def test_apology(self):
        self.assertEqual(self.pick(["对不起嘛，是我不好"]), "11-对方道歉")

    def test_irrelevant_no_match(self):
        self.assertIsNone(self.pick(["明天几点开会"]))
        self.assertIsNone(self.pick(["123456"]))

    def test_need_two_hits_by_default(self):
        # "买了" 只有一个弱信号，不该注入
        self.assertIsNone(self.pick(["我买了杯奶茶"]))
        # 加上具体场景就够两分
        self.assertEqual(self.pick(["今天去新开的店，买了杯奶茶"]), "04-分享日常")

    def test_comfort_on_bad_body(self):
        self.assertEqual(self.pick(["昨晚又熬夜了，今天头疼"]), "12-表达关心")


class InjectTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()

    def test_append_to_first_system_item(self):
        items = [system_item("sys prompt"), {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "hi"}]}]
        new_items = self.env["_inject_into_system"](items, "【说话小提示】x")
        self.assertEqual(new_items[0]["parts"][-1]["text"], "【说话小提示】x")
        self.assertEqual(len(new_items), 2)
        # 原 item 不被原地修改
        self.assertEqual(len(items[0]["parts"]), 1)

    def test_insert_when_no_system_item(self):
        items = [user_item("hi")]
        new_items = self.env["_inject_into_system"](items, "【说话小提示】x")
        self.assertEqual(new_items[0]["item_type"], "SystemMessageItem")
        self.assertEqual(len(new_items), 2)

    def test_already_injected_marker(self):
        items = [system_item("本体提示【说话小提示·报喜】..."), user_item("我考上了")]
        self.assertTrue(self.env["_already_injected"](items))
        self.assertFalse(self.env["_already_injected"]([system_item("干净")]))

    def test_prompt_contains_disclaimer(self):
        prompt = self.env["_build_prompt"]({"name": "01-报喜", "situation": "对方在报喜", "tip": "追问细节"})
        self.assertIn("【说话小提示·对方在报喜】", prompt)
        self.assertIn("不是台词", prompt)


class SessionTypeTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(self.env)

    def test_classify_session_id(self):
        classify = self.env["_classify_session_id"]
        self.assertEqual(classify("qq_group_200000001"), "group")
        self.assertEqual(classify("qq_private_100000004"), "private")
        self.assertEqual(classify("4701ff81c441ae0107a15d74e6e50885"), "")
        self.assertEqual(classify(""), "")

    def test_capture_group_message(self):
        msg = {"session_id": "qq_group_200000001", "message_info": {"group_info": {"group_id": "200000001"}}}
        asyncio.run(self.plugin.capture_session_type(message=msg))
        self.assertEqual(self.plugin._session_types["qq_group_200000001"], "group")

    def test_capture_private_message(self):
        msg = {"session_id": "qq_private_100000000", "message_info": {"user_info": {"user_id": "100000000"}}}
        asyncio.run(self.plugin.capture_session_type(message=msg))
        self.assertEqual(self.plugin._session_types["qq_private_100000000"], "private")

    def test_capture_fallback_sid_from_additional_config(self):
        msg = {"message_info": {"group_info": {"group_id": "1"}, "additional_config": {"session_id": "sess_x"}}}
        asyncio.run(self.plugin.capture_session_type(message=msg))
        self.assertEqual(self.plugin._session_types["sess_x"], "group")

    def test_session_map_bounded(self):
        for i in range(250):
            self.plugin._remember_session(f"s{i}", "group")
        self.assertLessEqual(len(self.plugin._session_types), 200)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(self.env)

    def test_group_policy_defaults(self):
        policy = self.plugin._policy_for("qq_group_1")
        self.assertEqual(policy.scope, "group")
        self.assertEqual(policy.min_score, 2.0)
        self.assertEqual(policy.max_per_hour, 10)

    def test_private_policy_relaxed(self):
        policy = self.plugin._policy_for("qq_private_100000000")
        self.assertEqual(policy.scope, "private")
        self.assertEqual(policy.min_score, 1.0)
        self.assertEqual(policy.min_interval_seconds, 30)

    def test_captured_private_session_wins(self):
        self.plugin._remember_session("weird_hash_123", "private")
        policy = self.plugin._policy_for("weird_hash_123")
        self.assertEqual(policy.scope, "private")

    def test_unknown_session_uses_default(self):
        policy = self.plugin._policy_for("4701ff81c441ae0107a15d74e6e50885")
        self.assertEqual(policy.scope, "unknown")
        self.assertEqual(policy.min_score, 2.0)

    def test_single_signal_fires_in_private_only(self):
        items = [system_item("sys"), user_item("我买了杯奶茶")]
        cards = self.env["_load_cards"]()
        group_pick = self.env["_pick_card"](items, cards, 2.0)
        private_pick = self.env["_pick_card"](items, cards, 1.0)
        self.assertIsNone(group_pick, "群聊单信号不该触发")
        self.assertIsNotNone(private_pick, "私聊单信号应触发")
        self.assertEqual(private_pick[0]["name"], "04-分享日常")

    def test_private_hook_injects_on_single_signal(self):
        self.plugin._remember_session("qq_private_1", "private")
        result = asyncio.run(self.plugin.inject_speech_card(
            items=[system_item("sys"), user_item("我买了杯奶茶")], session_id="qq_private_1",
        ))
        self.assertIn("modified_kwargs", result, "私聊单信号应当注入")


class SpecialUserTests(unittest.TestCase):
    DIRECTIVE = "【对这个人放开撩】放开来玩。"

    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(
            self.env, enabled=False,
            special_user_ids=["100000003"], special_directive=self.DIRECTIVE,
        )

    def test_capture_stores_user_id(self):
        msg = {
            "session_id": "qq_private_100000003",
            "message_info": {"user_info": {"user_id": "100000003"}},
        }
        asyncio.run(self.plugin.capture_session_type(message=msg))
        self.assertEqual(self.plugin._session_users["qq_private_100000003"], "100000003")

    def test_special_directive_matches(self):
        self.plugin._remember_session_user("s1", "100000003")
        self.assertEqual(self.plugin._special_directive_for("s1"), self.DIRECTIVE)

    def test_special_directive_ignores_others(self):
        self.plugin._remember_session_user("s2", "100000000")
        self.assertEqual(self.plugin._special_directive_for("s2"), "")
        self.assertEqual(self.plugin._special_directive_for("unknown"), "")

    def test_special_injects_even_when_cards_disabled(self):
        self.plugin._remember_session_user("s1", "100000003")
        result = asyncio.run(self.plugin.inject_speech_card(
            items=[system_item("sys"), user_item("今天干嘛呢")], session_id="s1",
        ))
        self.assertIn("modified_kwargs", result)
        text = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
        self.assertIn("【对这个人放开撩】", text)

    def test_non_special_no_inject_when_cards_disabled(self):
        self.plugin._remember_session_user("s2", "100000000")
        result = asyncio.run(self.plugin.inject_speech_card(
            items=[system_item("sys"), user_item("我考上研了")], session_id="s2",
        ))
        self.assertNotIn("modified_kwargs", result, "卡片关闭时普通会话不应注入")

    def test_special_idempotent_by_marker(self):
        items = [system_item("本体【对这个人放开撩】已注入"), user_item("hi")]
        self.assertTrue(self.env["_already_injected"](items))

    def test_planner_hook_injects_for_special_user(self):
        self.plugin._remember_session_user("s1", "100000003")
        result = asyncio.run(self.plugin.inject_planner_directive(
            items=[system_item("planner sys"), user_item("你就是暗恋我")], session_id="s1",
        ))
        self.assertIn("modified_kwargs", result, "Planner 也应收到达标指令")
        text = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
        self.assertIn("【对这个人放开撩】", text)

    def test_planner_hook_skips_normal_session(self):
        self.plugin._remember_session_user("s2", "100000000")
        result = asyncio.run(self.plugin.inject_planner_directive(
            items=[system_item("planner sys")], session_id="s2",
        ))
        self.assertNotIn("modified_kwargs", result)


class OwnerChannelTests(unittest.TestCase):
    OWNER = "【对男朋友放开撩】放开来玩。"

    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(
            self.env, enabled=False,
            owner_user_ids=["100000000"], owner_directive=self.OWNER,
            special_user_ids=["100000003"], special_directive="【对这个人放开撩】x",
        )

    def test_owner_directive_matches(self):
        self.plugin._remember_session_user("s_owner", "100000000")
        self.assertEqual(self.plugin._owner_directive_for("s_owner"), self.OWNER)

    def test_owner_directive_not_for_others(self):
        self.plugin._remember_session_user("s_alt", "100000003")
        self.assertEqual(self.plugin._owner_directive_for("s_alt"), "")

    def test_owner_injects_with_owner_text(self):
        self.plugin._remember_session_user("s_owner", "100000000")
        result = asyncio.run(self.plugin.inject_speech_card(
            items=[system_item("sys"), user_item("在干嘛")], session_id="s_owner",
        ))
        self.assertIn("modified_kwargs", result)
        text = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
        self.assertIn("【对男朋友放开撩】", text)

    def test_special_uses_special_text_not_owner(self):
        self.plugin._remember_session_user("s_alt", "100000003")
        result = asyncio.run(self.plugin.inject_speech_card(
            items=[system_item("sys"), user_item("在干嘛")], session_id="s_alt",
        ))
        text = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
        self.assertIn("【对这个人放开撩】", text)
        self.assertNotIn("【对男朋友", text)


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(self.env)

    def test_second_call_blocked_then_recovers(self):
        self.assertTrue(self.plugin._cooldown_ok("01-报喜", 1000.0))
        self.plugin._record("01-报喜", 1000.0)
        self.assertFalse(self.plugin._cooldown_ok("01-报喜", 1001.0))
        # 过了同卡冷却但没到全局间隔
        self.assertFalse(self.plugin._cooldown_ok("01-报喜", 1000.0 + 60))
        # 同卡+全局都过
        self.assertTrue(self.plugin._cooldown_ok("01-报喜", 1000.0 + 901))

    def test_hourly_cap(self):
        t = 0.0
        for i in range(10):
            t += 100.0  # 超过全局间隔即可
            self.plugin._record(f"card{i}", t)
        self.assertFalse(self.plugin._cooldown_ok("cardX", t + 100.0), "10 次后应触顶")

    def test_hourly_window_expires(self):
        self.plugin._record("a", 0.0)
        self.assertTrue(self.plugin._cooldown_ok("b", 3700.0), "一小时窗口过后应放行")


class HookTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(self.env)

    def run_hook(self, items):
        return asyncio.run(self.plugin.inject_speech_card(items=items, session_id="s1", extra_prompt=""))

    def test_injects_on_match(self):
        items = [system_item("sys"), user_item("我考上研究生啦！！")]
        result = self.run_hook(items)
        self.assertEqual(result["action"], "continue")
        merged = result["modified_kwargs"]
        self.assertIn("session_id", merged, "modified_kwargs 必须携带全部原 kwargs")
        self.assertEqual(merged["items"][0]["parts"][-1]["text"][:6], "【说话小提示")
        # 原始 kwargs 的 items 没被改
        self.assertEqual(len(items[0]["parts"]), 1)

    def test_no_match_returns_bare_continue(self):
        items = [system_item("sys"), user_item("明天几点开会")]
        result = self.run_hook(items)
        self.assertNotIn("modified_kwargs", result)

    def test_disabled_returns_continue(self):
        plugin = make_plugin(self.env, enabled=False)
        result = asyncio.run(plugin.inject_speech_card(items=[system_item("s"), user_item("我考上了")]))
        self.assertNotIn("modified_kwargs", result)

    def test_second_hook_call_idempotent(self):
        items = [system_item("sys"), user_item("我考上研究生啦！！")]
        first = self.run_hook(items)
        after_first = first["modified_kwargs"]["items"]
        second = self.run_hook(after_first)
        self.assertNotIn("modified_kwargs", second, "已注入过（marker 存在）不应重复注入")


if __name__ == "__main__":
    unittest.main()
