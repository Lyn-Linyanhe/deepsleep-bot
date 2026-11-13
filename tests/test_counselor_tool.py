"""Offline tests: deepsleep_counselor keyword matching / compose / crisis. No maibot_sdk, no network.

AST-extract the pure functions + class methods from plugin.py, stub config, run assertions.
"""
import ast
import os
import re
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(os.environ.get("DEEPSLEEP_WORKSPACE") or Path(__file__).resolve().parents[1])
PLUGIN = ROOT / "MaiBot/plugins/deepsleep_counselor/plugin.py"
KNOWLEDGE = ROOT / "MaiBot/plugins/deepsleep_counselor/knowledge"

FUNC_NAMES = ["_normalize", "_bigrams", "_load_topics", "_score_topic", "_select_topics"]
METHOD_NAMES = ["_pick_topics", "_read_general", "_compose"]
ASSIGN_PREFIXES = ("_",)


def load_env():
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8-sig"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(n for n in tree.body if isinstance(n, ast.Import))
    body.extend(
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id.startswith(ASSIGN_PREFIXES) for t in n.targets)
    )
    body.extend(
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNC_NAMES
    )
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CounselorPlugin")
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in METHOD_NAMES:
            node.decorator_list = []
            body.append(node)
    env = {
        "Any": Any, "Dict": dict, "List": list, "Optional": Any, "Path": Path,
        "re": re, "asyncio": __import__("asyncio"), "types": types,
        "__file__": str(PLUGIN),  # 模块里 _KNOWLEDGE_DIR 依赖 __file__
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(PLUGIN), "exec"), env)
    assert env["_KNOWLEDGE_DIR"] == KNOWLEDGE, env["_KNOWLEDGE_DIR"]
    return env


def make_plugin(env, max_topics=2, max_inject_chars=4200):
    cfg = types.SimpleNamespace(
        counselor=types.SimpleNamespace(max_topics=max_topics, max_inject_chars=max_inject_chars)
    )
    fake = types.SimpleNamespace(config=cfg)
    for name in METHOD_NAMES:
        setattr(fake, name, types.MethodType(env[name], fake))
    return fake


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()

    def test_all_topic_files_have_keywords(self):
        topics = self.env["_load_topics"]()
        names = [t["name"] for t in topics]
        self.assertEqual(len(topics), 22, f"期望 22 个主题，实际 {names}")
        for topic in topics:
            self.assertTrue(topic["keywords"], topic["name"])
            self.assertGreater(len(topic["text"]), 200, topic["name"])

    def test_general_file_excluded_from_topics(self):
        names = [t["name"] for t in self.env["_load_topics"]()]
        self.assertNotIn("00-通用方法", names)

    def test_normalize_and_bigrams(self):
        self.assertEqual(self.env["_normalize"]("  Get ChA t \n"), "getchat")
        self.assertEqual(self.env["_bigrams"]("abc"), {"ab", "bc"})
        self.assertEqual(self.env["_bigrams"]("a"), {"a"})
        self.assertEqual(self.env["_bigrams"](""), set())


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.topics = self.env["_load_topics"]()

    def top(self, query, limit=2):
        return [t["name"] for t in self.env["_select_topics"](query, self.topics, limit)]

    def test_reply_talk(self):
        picks = self.top("对方说好累，我该怎么回他比较好")
        self.assertIn("01-回复话术", picks)

    def test_conflict_cold_war(self):
        picks = self.top("我们吵架了现在冷战 谁也不理谁 好难受")
        self.assertEqual(picks[0], "11-冲突与吵架")

    def test_imbalance(self):
        picks = self.top("感觉这段关系一直是我单方面付出 他好像不在乎我")
        self.assertIn("12-投入失衡与退出", picks)

    def test_screenshot_analysis(self):
        picks = self.top("帮我看看这张聊天截图，她是不是对我没意思")
        self.assertIn("10-网聊与截图解读", picks)

    def test_pua(self):
        picks = self.top("他说我拒绝就是在测试诚意 这是不是PUA")
        self.assertIn("04-操控手法识别", picks)

    def test_quarrel_apology(self):
        picks = self.top("跟女朋友吵架了 想道歉不知道怎么说")
        self.assertIn("11-冲突与吵架", picks)

    def test_isolated(self):
        picks = self.top("新宿舍没人跟我说话 感觉被孤立了")
        self.assertIn("19-被孤立破局", picks)

    def test_irrelevant_query_returns_nothing(self):
        self.assertEqual(self.top("今天中午吃什么好呢 食堂好难吃"), [])

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.top("   "), [])

    def test_exact_hit_beats_partial(self):
        env = self.env
        topic = {"name": "t", "keywords": ["怎么回"], "text": ""}
        exact = env["_score_topic"]("怎么回", env["_bigrams"]("怎么回"), topic)
        partial = env["_score_topic"]("怎么回复他", env["_bigrams"]("怎么回复他"), topic)
        # 部分命中走 bigram 覆盖率（3/3 也算命中词内全部 bigram，但整词不在串里）
        self.assertGreater(exact, 0)
        self.assertGreater(partial, 0)
        self.assertLessEqual(partial, exact)


class CrisisTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(self.env)

    def test_crisis_pattern(self):
        pattern = self.env["_CRISIS_PATTERN"]
        for text in ["他昨天家暴我", "我想自杀", "对方拿私密影像威胁我", "被骗了五千块"]:
            self.assertTrue(pattern.search(text), text)
        self.assertFalse(pattern.search("我们吵架了"), "正常人吵架不该触发危机")

    def test_crisis_forces_crisis_topic(self):
        picks = self.plugin._pick_topics("他喝醉了打我，还威胁不让我走")
        names = [t["name"] for t in picks]
        self.assertEqual(names[0], "21-危机与安全")

    def test_non_crisis_has_no_crisis_topic(self):
        picks = self.plugin._pick_topics("我们吵架了现在冷战")
        names = [t["name"] for t in picks]
        self.assertNotIn("21-危机与安全", names)

    def test_max_topics_respected(self):
        plugin = make_plugin(self.env, max_topics=1)
        picks = plugin._pick_topics("吵架了想分手又舍不得 一直是我付出")
        self.assertLessEqual(len(picks), 1)


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.env = load_env()
        self.plugin = make_plugin(self.env)

    def test_compose_within_cap_keeps_general(self):
        picks = self.env["_select_topics"]("我们吵架了", self.env["_load_topics"](), 1)
        general = self.plugin._read_general()
        body = self.plugin._compose(picks, general)
        self.assertIn("冲突与吵架", body)
        self.assertIn("通用方法", body)

    def test_tight_cap_skips_whole_files_not_truncate(self):
        plugin = make_plugin(self.env, max_inject_chars=1200)
        picks = self.env["_select_topics"]("我们吵架了", self.env["_load_topics"](), 1)
        general = plugin._read_general()
        body = plugin._compose(picks, general)
        # 第一份主题能装下就装，装不下就一份都不要；绝不出现半份截断
        self.assertNotIn("…（参考知识已截断）", body)
        if picks:
            full = picks[0]["text"].strip()
            if len(full) <= 1200:
                self.assertIn(full[:100], body)

    def test_zero_cap_means_unlimited(self):
        plugin = make_plugin(self.env, max_inject_chars=0)
        picks = self.env["_select_topics"]("我们吵架了", self.env["_load_topics"](), 2)
        general = plugin._read_general()
        body = plugin._compose(picks, general)
        for item in picks:
            self.assertIn(item["text"].strip(), body, f"{item['name']} 应被完整纳入")
        self.assertIn(general.strip(), body, "通用方法应被完整纳入")


if __name__ == "__main__":
    unittest.main()
