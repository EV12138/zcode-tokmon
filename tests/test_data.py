#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据层口径单测：用临时 SQLite 库验证 agent 的聚合/上下文/速率/delta。

运行：python3 tests/test_data.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins", "zcode-token-monitor"))
import agent  # noqa: E402

NOW = int(datetime(2026, 10, 18, 12, 0, 0).timestamp() * 1000)
MIN = 60 * 1000
HOUR = 60 * MIN

SCHEMA = """
CREATE TABLE session (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  directory TEXT NOT NULL,
  task_type TEXT NOT NULL DEFAULT 'interactive',
  parent_id TEXT,
  time_created INTEGER NOT NULL
);
CREATE TABLE model_usage (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  turn_id TEXT,
  model_id TEXT NOT NULL,
  status TEXT NOT NULL,
  query_source TEXT NOT NULL,
  provider_id TEXT NOT NULL DEFAULT 'test-provider',
  started_at INTEGER NOT NULL,
  first_token_at INTEGER,
  completed_at INTEGER,
  duration_ms INTEGER,
  time_to_first_token_ms INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  tool_call_count INTEGER NOT NULL DEFAULT 0,
  computed_total_tokens INTEGER NOT NULL DEFAULT 0
);
"""


def row(rid, sid, status="completed", started=None, first=None, completed=None,
        inp=0, out=0, reasoning=0, model="GLM-5.3", turn="turn_1",
        cache_read=0, ttft=None):
    return {
        "id": rid, "session_id": sid, "turn_id": turn, "model_id": model,
        "status": status, "query_source": "main_turn",
        "provider_id": "test-provider",
        "started_at": started, "first_token_at": first, "completed_at": completed,
        "duration_ms": None, "time_to_first_token_ms": ttft,
        "input_tokens": inp, "output_tokens": out,
        "reasoning_tokens": reasoning, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": 0, "retry_count": 0, "tool_call_count": 0,
        "computed_total_tokens": inp + out,
    }


class DataLayerTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.con = sqlite3.connect(self.path)
        self.con.executescript(SCHEMA)
        self.con.execute("insert into session values (?,?,?,?,?,?)",
                         ("sess_A", "主会话", "/tmp/proj", "interactive", None, NOW - 10 * HOUR))
        self.con.execute("insert into session values (?,?,?,?,?,?)",
                         ("sess_B", "子代理", "/tmp/proj", "subagent_child", "sess_A", NOW - 5 * HOUR))
        self.con.execute("insert into session values (?,?,?,?,?,?)",
                         ("sess_C", "昨天会话", "/tmp/old", "interactive", None, NOW - 30 * HOUR))
        rows = [
            # 主会话：今天三次完成（其中最后一次 first_token 缺失→不可评分）、一次错误、一次进行中
            row("r1", "sess_A", started=NOW - 15 * MIN - 5000, first=NOW - 15 * MIN - 4500,
                completed=NOW - 15 * MIN, inp=1000, out=200, ttft=500),
            row("r2", "sess_A", started=NOW - 5 * MIN - 10000, first=NOW - 5 * MIN - 3000,
                completed=NOW - 5 * MIN, inp=2000, out=400, reasoning=100,
                cache_read=1500, ttft=7000),
            row("r3", "sess_A", started=NOW - 2 * MIN - 10000, first=None,
                completed=NOW - 2 * MIN, inp=3000, out=50),
            row("e1", "sess_A", status="error", started=NOW - MIN, first=NOW - MIN,
                completed=NOW - MIN, inp=9000, out=999),
            row("run1", "sess_A", status="running", started=NOW - 30000,
                first=None, completed=None, inp=4000, out=0),
            # 子代理：今天一次
            row("r4", "sess_B", started=NOW - 10 * MIN - 5000, first=NOW - 10 * MIN - 4000,
                completed=NOW - 10 * MIN, inp=500, out=100, ttft=1000),
            # 昨天（不应计入今日）
            row("old", "sess_C", started=NOW - 26 * HOUR, first=NOW - 26 * HOUR + 1000,
                completed=NOW - 26 * HOUR + 5000, inp=7000, out=770),
        ]
        for r in rows:
            self.con.execute(
                "insert into model_usage values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["session_id"], r["turn_id"], r["model_id"], r["status"],
                 r["query_source"], r["provider_id"], r["started_at"], r["first_token_at"], r["completed_at"],
                 r["duration_ms"], r["time_to_first_token_ms"], r["input_tokens"],
                 r["output_tokens"], r["reasoning_tokens"],
                 r["cache_read_input_tokens"], r["cache_creation_input_tokens"],
                 r["retry_count"], r["tool_call_count"], r["computed_total_tokens"]))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        os.unlink(self.path)

    # ---------- 今日 ----------
    def test_today_excludes_error_running_and_other_days(self):
        t = agent.q_today(self.con, NOW)
        self.assertEqual(t["requests"], 4)          # r1 r2 r3 r4
        self.assertEqual(t["total"], 1200 + 2400 + 3050 + 600)
        self.assertEqual(t["output"], 200 + 400 + 50 + 100)
        self.assertEqual(t["input"], 1000 + 2000 + 3000 + 500)

    # ---------- 会话 ----------
    def test_session_totals_context_and_rate(self):
        st = agent.q_session(self.con, "sess_A", NOW)
        self.assertIsNotNone(st)
        self.assertEqual(st["requests"], 3)              # completed only
        self.assertEqual(st["totalTokens"], 1200 + 2400 + 3050)
        self.assertEqual(st["context"], 3000)            # 最近一次完成请求的输入
        self.assertEqual(st["model"], "GLM-5.3")
        self.assertEqual(st["cacheHitRate"], 25.0)       # 1500 / 6000
        self.assertEqual(st["running"], 1)
        # 最近一次完成请求 first_token 缺失 → lastTps 回扫到 r2：400tok/10s = 40
        self.assertEqual(st["lastTps"], 40.0)
        # 加权样本：r2(400/10000) + r1(200/4500)，r3 无效
        self.assertEqual(st["rate"]["ratedRequests"], 2)
        self.assertEqual(st["rate"]["coverage"], round(2 / 3, 3))
        self.assertAlmostEqual(st["rate"]["tps"], round(600 / 14.5, 1))
        # 当天所有完成请求的最高单条：r1 200tok / 4.5s = 44.4（r3 无 first_token 无效）
        self.assertEqual(st["todayPeak"], 44.4)
        # 当天加权均速：(200+400) / (4.5s+10s) = 41.4
        self.assertEqual(st["todayTps"], 41.4)
        self.assertEqual(st["todayRequests"], 3)

    def test_last_tps_scans_past_five_invalid_rows(self):
        # 最近 6 条完成请求都缺 first_token_at（不可评分），
        # lastTps 应回扫到更早的有效请求 r2（400tok / 10s = 40），而不是显示 0/—
        for i in range(6):
            self.con.execute(
                "insert into model_usage values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("x%d" % i, "sess_A", "t", "GLM-5.3", "completed", "main_turn",
                 "test-provider", NOW - (i + 1) * 1000 - 10000, None, NOW - (i + 1) * 1000,
                 None, None, 100, 10, 0, 0, 0, 0, 0, 110))
        self.con.commit()
        st = agent.q_session(self.con, "sess_A", NOW)
        self.assertEqual(st["lastTps"], 40.0)

    def test_models_and_recent(self):
        models = agent.q_models(self.con, 1, NOW)
        self.assertEqual(len(models), 1)
        m = models[0]
        self.assertEqual(m["model_id"], "GLM-5.3")
        self.assertEqual(m["requests"], 4)               # r1 r2 r3 r4
        self.assertEqual(m["input"], 6500)
        self.assertEqual(m["output"], 750)
        self.assertEqual(m["cache_read"], 1500)
        self.assertEqual(m["cacheHitRate"], 23.1)
        self.assertEqual(m["avgTtftMs"], 2833)           # (500+7000+1000)/3

        recent = agent.q_recent(self.con, 3)
        self.assertEqual([r["id"] for r in recent], ["r3", "r2", "r4"])
        self.assertEqual(recent[0]["tps"], 0.0)          # r3 缺 first_token_at
        self.assertEqual(recent[0]["rateReason"], "missingGenerationWindow")
        self.assertEqual(recent[1]["tps"], 40.0)         # 400tok / 10s
        self.assertEqual(recent[2]["tps"], 25.0)         # 100tok / 4s
        self.assertEqual(recent[1]["model_id"], "GLM-5.3")
        self.assertEqual(recent[1]["cacheHitRate"], 75.0)

    def test_session_missing_returns_none(self):
        self.assertIsNone(agent.q_session(self.con, "sess_missing", NOW))

    # ---------- delta ----------
    def test_delta_sql_aggregate_window(self):
        since = NOW - 30 * MIN
        d = agent.q_delta(self.con, since, NOW)
        self.assertEqual(d["requests"], 4)               # r1 r2 r3 r4
        self.assertEqual(d["total"], 1200 + 2400 + 3050 + 600)
        self.assertEqual(d["output"], 200 + 400 + 50 + 100)
        self.assertEqual(d["windowMs"], 30 * MIN)
        self.assertFalse(d["sampleTruncated"])

    def test_delta_not_truncated_by_rate_sample_cap(self):
        # 600 条完成请求：总量必须完整，速率样本上限 500 只影响采样
        since = NOW - MIN
        for i in range(600):
            self.con.execute(
                "insert into model_usage values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bulk%d" % i, "sess_A", "t", "GLM-5.3", "completed", "main_turn",
                 "test-provider", NOW - 50000, NOW - 49000, NOW - 1000, None, None,
                 5, 5, 0, 0, 0, 0, 0, 10))
        self.con.commit()
        d = agent.q_delta(self.con, since, NOW)
        self.assertEqual(d["requests"], 600)
        self.assertEqual(d["total"], 6000)
        self.assertEqual(d["sampled"], 500)
        self.assertTrue(d["sampleTruncated"])

    # ---------- 活跃会话 / 当前会话 ----------
    def test_active_sessions_and_current(self):
        sessions = agent.q_active_sessions(self.con, NOW)
        ids = [s["sid"] for s in sessions]
        self.assertIn("sess_A", ids)
        self.assertIn("sess_B", ids)
        self.assertNotIn("sess_C", ids)                  # 昨天的会话不活跃
        a = [s for s in sessions if s["sid"] == "sess_A"][0]
        self.assertTrue(a["isSubagent"] is False)
        b = [s for s in sessions if s["sid"] == "sess_B"][0]
        self.assertTrue(b["isSubagent"])
        cur = agent.q_current(self.con, NOW)
        self.assertEqual(cur["sid"], "sess_A")           # 子代理不参与“当前会话”
        self.assertEqual(cur["context"], 3000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
