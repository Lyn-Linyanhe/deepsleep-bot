"""Offline tests: dual_path recency stamping + execution-service recency reorder. No DB/network."""
import ast
import os
import math
from pathlib import Path
import sys
import time as time_module
import types
import unittest
from typing import Any, Dict, List

ROOT = Path(os.environ.get("DEEPSLEEP_WORKSPACE") or Path(__file__).resolve().parents[1])
DUAL = ROOT / "MaiBot/src/A_memorix/core/retrieval/dual_path.py"
EXEC = ROOT / "MaiBot/src/A_memorix/core/utils/search_execution_service.py"
NOW = 1_794_600_000.0


def load_named(path: Path, class_name: str, method_names, extra_assign_prefix="_"):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(n for n in tree.body if isinstance(n, ast.Import))
    body.extend(n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id.startswith(extra_assign_prefix) for t in n.targets))
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if cls is not None:
        body.extend(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in method_names)
    else:
        body.extend(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in method_names)
    env = {"Any": Any, "Dict": dict, "List": list, "Optional": Any, "Tuple": tuple,
           "types": types, "time": time_module, "math": math, "json": __import__("json"),
           "hashlib": __import__("hashlib"), "re": __import__("re"),
           "logger": types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)}

    class SearchExecutionService:
        @staticmethod
        def _resolve_runtime_component(plugin_config, plugin_instance, name):
            return plugin_instance

    env["SearchExecutionService"] = SearchExecutionService
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), env)
    return env


def make_results(items):
    return [types.SimpleNamespace(hash_value=h, content=c, score=s, result_type=t,
                                  metadata=dict(meta)) for h, c, s, t, meta in items]


def row(last_accessed, created_at):
    return {"last_accessed": last_accessed, "created_at": created_at}


class StampTests(unittest.TestCase):
    def setUp(self):
        self.env = load_named(DUAL, "DualPathRetriever", ["_stamp_paragraph_recency_factors"])
        self.stamp = self.env["_stamp_paragraph_recency_factors"]
        self.patched = time_module.time
        time_module.time = lambda: NOW
        self.addCleanup(lambda: setattr(time_module, "time", self.patched))

    def make_self(self, rows):
        class Store:
            def get_paragraphs_by_hashes(self, hashes):
                return {h: rows[h] for h in hashes if h in rows}

        fake = types.SimpleNamespace(metadata_store=Store())
        fake._stamp_paragraph_recency_factors = types.MethodType(self.stamp, fake)
        fake._PARAGRAPH_RECENCY_HALF_LIFE_HOURS = 24.0
        fake._PARAGRAPH_RECENCY_FLOOR = 0.15
        return fake

    def test_fresh_stamped_with_decay_value(self):
        results = make_results([("a", "一小时前访问", 0.8, "paragraph", {})])
        self.make_self({"a": row(NOW - 3600.0, NOW - 90000.0)})._stamp_paragraph_recency_factors(results)
        self.assertAlmostEqual(results[0].metadata["recency_factor"], 2 ** (-1 / 24), places=3)
        self.assertEqual(results[0].score, 0.8)

    def test_stale_hits_floor_and_never_accessed_uses_created(self):
        results = make_results([
            ("old", "三个月未访问", 0.9, "paragraph", {}),
            ("new", "刚写完未被检索", 0.7, "paragraph", {}),
        ])
        self.make_self({
            "old": row(NOW - 90 * 86400.0, NOW - 95 * 86400.0),
            "new": row(0.0, NOW - 7200.0),
        })._stamp_paragraph_recency_factors(results)
        self.assertEqual(results[0].metadata["recency_factor"], 0.15)
        self.assertAlmostEqual(results[1].metadata["recency_factor"], 2 ** (-2 / 24), places=3)

    def test_relations_and_missing_rows_skipped(self):
        results = make_results([("rel", "关系", 0.5, "relation", {}), ("ghost", "无行", 0.8, "paragraph", {})])
        self.make_self({})._stamp_paragraph_recency_factors(results)
        self.assertEqual(results[0].metadata, {})
        self.assertEqual(results[1].metadata, {})


class ReorderTests(unittest.TestCase):
    def setUp(self):
        self.env = load_named(EXEC, None, ["_apply_paragraph_recency_reorder"])
        self.reorder = self.env["_apply_paragraph_recency_reorder"]

    def test_order_by_score_times_factor_without_score_mutation(self):
        results = make_results([
            ("old", "老而热", 0.9, "paragraph", {"recency_factor": 0.15}),
            ("new", "新而稍低", 0.85, "paragraph", {"recency_factor": 1.0}),
            ("rel", "关系命中", 0.5, "relation", {}),
        ])
        adjusted = self.reorder(results, caller="t")
        self.assertEqual(adjusted, 2)
        self.assertEqual([r.hash_value for r in results], ["new", "rel", "old"])
        self.assertEqual([r.score for r in results], [0.85, 0.5, 0.9])

    def test_ties_keep_original_rank_order(self):
        results = make_results([
            ("a", "甲", 0.8, "paragraph", {"recency_factor": 1.0}),
            ("b", "乙", 0.8, "paragraph", {"recency_factor": 1.0}),
        ])
        self.reorder(results, caller="t")
        self.assertEqual([r.hash_value for r in results], ["a", "b"])

    def test_no_factors_means_no_reorder(self):
        results = make_results([
            ("x", "高", 0.9, "paragraph", {}),
            ("y", "低", 0.5, "paragraph", {}),
        ])
        adjusted = self.reorder(results, caller="t")
        self.assertEqual(adjusted, 0)
        self.assertEqual([r.hash_value for r in results], ["x", "y"])

    def test_invalid_factor_treated_as_one(self):
        results = make_results([
            ("a", "坏因子", 0.7, "paragraph", {"recency_factor": "abc"}),
            ("b", "好因子", 0.7, "paragraph", {"recency_factor": 0.2}),
        ])
        self.reorder(results, caller="t")
        self.assertEqual([r.hash_value for r in results], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
