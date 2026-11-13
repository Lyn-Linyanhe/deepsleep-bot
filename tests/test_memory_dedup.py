"""Offline tests for memory injection dedup and placeholder skip; no DB or network."""
import ast
import os
import asyncio
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock

ROOT = Path(os.environ.get("DEEPSLEEP_WORKSPACE") or Path(__file__).resolve().parents[1])
INJ = ROOT / "MaiBot/src/maisaka/memory/heuristic_injector.py"
IMP = ROOT / "MaiBot/src/A_memorix/core/utils/summary_importer.py"

LINE_DA = "deepsleep聊到学线性代数时卡在第二题半小时，草稿纸快画穿，觉得线代卡住时总有“再算一步就出来了”的感觉。"
LINE_DA_B = ("deepsleep 在群里聊到自己学线性代数时卡在第二题半小时，草稿纸快画穿了，觉得线代卡住时总有"
             "“再算一步就出来了”的感觉。天羽夕莉提到自己的机器人现在应该不吵了，不会一直说下去。")
PLACE_1 = "本段为群聊中的技术闲聊，各方未确认新的长期事实，无新增可保留信息。"
PLACE_2 = "未发现新的、经确认且对未来对话有用的事实增量，本轮内容为群内闲聊及机器人配置询问，无可保留信息。"
CONFESS = "测试用户与deepsleep在聊天中互相表达喜欢，deepsleep承认喜欢的人是测试用户，两人约定玩真心话大冒险。"
LOTTERY = "群内讨论抽奖开奖时间，deepsleep称自己在群里待了很久、算是看着群规模长大的。"
FISH = "群里的成员拿「鱼」这个外号跟deepsleep开玩笑，deepsleep澄清自己是人，只是被叫鱼叫习惯了。"
LONG_WITH_PHRASE = ("测试用户和deepsleep聊了很长的关于以后一起做饭的话题，两人约定等她期末考完试回到家里就尝试一次"
                    "简单的晚饭，虽然嘴上说着没有新增什么计划，但其实两个人都很期待那顿饭到底是什么味道，聊到很晚才睡。")


def load_module_functions(path: Path, names):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(n for n in tree.body if isinstance(n, ast.Import))
    for name in names:
        node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        body.append(node)
    body.extend(n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id.startswith("_") for t in n.targets))
    env = {"AsyncMock": AsyncMock, "types": types, "logging": logging, "asyncio": asyncio,
           "MemoryHit": dict, "Sequence": list, "re": __import__("re"), "set": set}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), env)
    return env


def extract_search_method():
    tree = ast.parse(INJ.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HeuristicMemoryInjector")
    names = {"_search_related_memory", "_is_current_session_hit", "_resolve_source_session_id"}
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(methods)
    return body


def make_hit(content, chat, *, hash_value="", hit_type="paragraph", source_type="chat_summary"):
    source = f"chat_summary:{chat}"
    return types.SimpleNamespace(
        hash_value=hash_value or f"h_{abs(hash(content))}", content=content, hit_type=hit_type,
        source=source, metadata={"chat_id": chat, "source_type": source_type, "source": source})


class PlaceholderTests(unittest.TestCase):
    def setUp(self):
        self.env = load_module_functions(IMP, ["_is_placeholder_summary"])
        self.check = self.env["_is_placeholder_summary"]

    def test_placeholders_detected(self):
        for text in (PLACE_1, PLACE_2, "本轮聊天仅为群内围绕戳一戳的玩笑互动，没有新增经过确认、对以后对话有用的事实。",
                     "这些属于轻松闲聊与角色自述，未形成新的稳定事实，也没有值得记住的内容。"):
            self.assertTrue(self.check(text), text[:20])

    def test_real_memories_kept(self):
        for text in (LINE_DA, CONFESS, LOTTERY, FISH, LONG_WITH_PHRASE,
                     "群聊中大家闲聊脑机接口与数字飞升等话题，deepsleep自述记性不好、睡一觉就忘掉一半聊天内容。这些属于轻松闲聊与角色自述，未形成新的稳定事实。"):
            self.assertFalse(self.check(text), text[:20])

    def test_empty_and_long_guard(self):
        self.assertFalse(self.check(""))
        self.assertFalse(self.check("无新增" * 61))  # >120 chars: length guard


class InjectorLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = load_module_functions(INJ, ["_text_bigrams", "_looks_like_placeholder", "_is_near_duplicate"])
        self.loop_env = {"global_config": types.SimpleNamespace(a_memorix=types.SimpleNamespace(integration=types.SimpleNamespace(
            heuristic_memory_recall_limit=5, heuristic_memory_cross_chat_enabled=True))),
            "memory_service": types.SimpleNamespace(search=AsyncMock()), "logger": logging.getLogger("dedup-test")}
        self.loop_env.update(self.env)
        exec(compile(ast.fix_missing_locations(ast.Module(body=extract_search_method(), type_ignores=[])), str(INJ), "exec"), self.loop_env)

    def make_self(self, allowed_all=True):
        fake_cls = type("S", (), {"_resolve_hit_sources_by_hash": AsyncMock(return_value={}),
                                  "_search_related_memory": self.loop_env["_search_related_memory"],
                                  "_is_current_session_hit": self.loop_env["_is_current_session_hit"],
                                  "_resolve_source_session_id": self.loop_env["_resolve_source_session_id"],
                                  "_is_hit_allowed": (lambda self, hit, context, resolved_source="": allowed_all)})
        return fake_cls()

    async def run_filter(self, hits, limit=None, allowed_all=True):
        payload = {"success": True, "filtered": False, "hits": hits}
        self.loop_env["memory_service"].search = AsyncMock(return_value=types.SimpleNamespace(**payload))
        fake_self = self.make_self(allowed_all)
        if limit is not None:
            self.loop_env["global_config"].a_memorix.integration.heuristic_memory_recall_limit = limit
        context = types.SimpleNamespace(session=types.SimpleNamespace(user_id="u", group_id="g", session_id="s_cur"))
        return await fake_self._search_related_memory("impression", context)

    async def test_duplicates_and_placeholders_removed(self):
        hits = [make_hit(c, "other", hash_value=h) for h, c in [
            ("a1", LINE_DA), ("a2", LINE_DA_B), ("p1", PLACE_1), ("b1", CONFESS),
            ("c1", LOTTERY), ("a1", LINE_DA), ("d1", FISH), ("p2", PLACE_2)]]
        kept = await self.run_filter(hits)
        contents = [k.content for k in kept]
        self.assertEqual(contents, [LINE_DA, CONFESS, LOTTERY, FISH])
        for kept_item in kept:
            self.assertNotIn("无新增", kept_item.content)

    async def test_limit_respected_after_filtering(self):
        topics = ("修电脑", "做晚饭", "背单词", "取快递", "浇花", "跑步", "看书", "画画")
        hits = [make_hit(f"第{i}个独立记忆：{topic}的日常安排", "other") for i, topic in enumerate(topics)]
        kept = await self.run_filter(hits, limit=3)
        self.assertEqual(len(kept), 3)

    async def test_all_placeholder_yields_empty(self):
        kept = await self.run_filter([make_hit(PLACE_1, "other")])
        self.assertEqual(kept, [])

    async def test_distinct_similar_topic_not_dropped(self):
        pair = [("x1", "测试用户说要教deepsleep写线代作业，两人约好周日下午一起在图书馆自习。"),
                ("x2", "测试用户说这周末要带deepsleep去吃新开的那家火锅店，庆祝她期末周结束。")]
        kept = await self.run_filter([make_hit(c, "other", hash_value=h) for h, c in pair])
        self.assertEqual(len(kept), 2)

    async def test_helpers_consistent_with_real_duplicate_pair(self):
        self.assertTrue(self.loop_env["_is_near_duplicate"](LINE_DA, LINE_DA_B))
        self.assertFalse(self.loop_env["_is_near_duplicate"](LINE_DA, CONFESS))


class QuotaTests(InjectorLoopTests):
    """复用 InjectorLoopTests 的夹具，只验证会话配额逻辑。"""

    async def test_quota_guarantees_current_session(self):
        cross_topics = ("模型部署调试", "显卡选购比较", "周末聚餐地点", "新番动画追更", "健身计划安排")
        current_topics = ("一起复习线代和作业", "昨晚约好周末去看展览")
        cross = [make_hit(f"外群记忆：{topic}的独立讨论记录", f"other{i}") for i, topic in enumerate(cross_topics)]
        current = [make_hit(f"本会话记忆：{topic}", "s_cur") for topic in current_topics]
        kept = await self.run_filter(cross + current)
        chats = [(h.metadata or {}).get("chat_id") for h in kept]
        self.assertEqual(chats.count("s_cur"), 2)
        self.assertEqual(len(kept), 5)

    async def test_quota_no_hole_when_current_few(self):
        cross_topics = ("模型部署调试", "显卡选购比较", "周末聚餐地点", "新番动画追更", "健身计划安排")
        cross = [make_hit(f"外群记忆：{topic}的独立讨论记录", f"other{i}") for i, topic in enumerate(cross_topics)]
        current = [make_hit("本会话唯一记忆：昨晚说过要早睡", "s_cur")]
        kept = await self.run_filter(cross + current, limit=5)
        chats = [(h.metadata or {}).get("chat_id") for h in kept]
        self.assertEqual(chats.count("s_cur"), 1)
        self.assertEqual(len(kept), 5)

    async def test_final_order_preserves_rank(self):
        cross_topics = ("模型部署调试", "显卡选购比较", "周末聚餐地点")
        cross = [make_hit(f"外群记忆：{topic}的独立讨论记录", f"other{i}") for i, topic in enumerate(cross_topics)]
        current = [make_hit("本会话记忆：一起复习线代", "s_cur")]
        kept = await self.run_filter(cross + current, limit=5)
        contents = [h.content for h in kept]
        self.assertEqual(contents[0], "外群记忆：模型部署调试的独立讨论记录")
        self.assertEqual(contents[-1], "本会话记忆：一起复习线代")


if __name__ == "__main__":
    unittest.main()
