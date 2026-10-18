#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""速率口径纯函数单测：python3 tests/test_rate.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "zcode-token-monitor"))
import agent  # noqa: E402

S = 1_000_000_000_000  # 任意基准毫秒


def row(completed=S + 5000, first_tok=S + 1000, started=S, out=1000,
        reasoning=0, model="GLM-5.3"):
    return {"started_at": started, "first_token_at": first_tok,
            "completed_at": completed, "output_tokens": out,
            "reasoning_tokens": reasoning, "model_id": model}


class TestGenWindow(unittest.TestCase):
    def test_plain_uses_first_token(self):
        # 无思考：窗口 = completed - first_token = 4000ms
        gen, why = agent.gen_window(row())
        self.assertEqual(gen, 4000)
        self.assertIsNone(why)

    def test_reasoning_uses_started(self):
        # 思考段：窗口 = completed - started = 5000ms
        gen, _ = agent.gen_window(row(reasoning=500))
        self.assertEqual(gen, 5000)

    def test_claude_uses_started_even_zero_reasoning(self):
        # Claude 系：reasoning 恒 0 但 output 含思考 → 从 started 起算
        gen, _ = agent.gen_window(row(model="claude-opus-4-5"))
        self.assertEqual(gen, 5000)

    def test_missing_first_token(self):
        gen, why = agent.gen_window(row(first_tok=None))
        self.assertIsNone(gen)
        self.assertEqual(why, "missingGenerationWindow")

    def test_missing_first_token_ok_when_reasoning(self):
        # 思考段用 started，first_token 缺失也有效
        gen, _ = agent.gen_window(row(first_tok=None, reasoning=10))
        self.assertEqual(gen, 5000)

    def test_window_too_short(self):
        gen, why = agent.gen_window(row(completed=S + 100, first_tok=S))
        self.assertIsNone(gen)
        self.assertEqual(why, "generationWindowOutOfRange")

    def test_window_too_long(self):
        gen, why = agent.gen_window(row(completed=S + 3_600_001, first_tok=S))
        self.assertIsNone(gen)
        self.assertEqual(why, "generationWindowOutOfRange")

    def test_no_output(self):
        gen, why = agent.gen_window(row(out=0))
        self.assertIsNone(gen)
        self.assertEqual(why, "noOutput")

    def test_running(self):
        gen, why = agent.gen_window(row(completed=None))
        self.assertIsNone(gen)
        self.assertEqual(why, "running")


class TestAggregate(unittest.TestCase):
    def test_weighted(self):
        # 1000 tok / 4s = 250 tps；2000 tok / 1s = 2000 tps
        # 加权 = 3000 / 5000ms = 600 tps；峰值 2000
        agg = agent.aggregate_rate([row(out=1000), row(out=2000, first_tok=S + 4000)])
        self.assertEqual(agg["tps"], 600.0)
        self.assertEqual(agg["peak"], 2000.0)
        self.assertEqual(agg["ratedRequests"], 2)
        self.assertEqual(agg["coverage"], 1.0)

    def test_invalid_rows_excluded(self):
        agg = agent.aggregate_rate([row(out=1000), row(out=0)])  # 第二行 noOutput
        self.assertEqual(agg["tps"], 250.0)
        self.assertEqual(agg["coverage"], 0.5)

    def test_all_invalid(self):
        agg = agent.aggregate_rate([row(out=0), row(completed=None)])
        self.assertEqual(agg["tps"], 0.0)
        self.assertEqual(agg["coverage"], 0.0)

    def test_last_valid_tps_scanback(self):
        # 最新行缺 first_token（无效），回扫到上一条有效行
        tps = agent.last_valid_tps([row(first_tok=None), row()])
        self.assertEqual(tps, 250.0)


class TestFormat(unittest.TestCase):
    def test_fmt_tok(self):
        self.assertEqual(agent.fmt_tok(940), "940")
        self.assertEqual(agent.fmt_tok(128_400), "128.4k")
        self.assertEqual(agent.fmt_tok(2_100_000), "2.10M")
        self.assertEqual(agent.fmt_tok(0), "0")

    def test_fmt_tps(self):
        self.assertEqual(agent.fmt_tps(42.7), "42.7")
        self.assertEqual(agent.fmt_tps(0), "—")


if __name__ == "__main__":
    unittest.main(verbosity=2)
