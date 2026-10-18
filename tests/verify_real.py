#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对真实用量库做交叉验证：agent 的输出 vs 独立 SQL 手算。

只读；用于插件发布前/升级后确认数据口径。
运行：python3 tests/verify_real.py [db路径]
"""
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.environ.get("TOKMON_AGENT_DIR") or os.path.join(HERE, "..", "plugins", "zcode-token-monitor")
sys.path.insert(0, AGENT_DIR)
import agent  # noqa: E402

FAIL = []


def check(name, got, want, tol=0):
    ok = abs(float(got) - float(want)) <= tol if isinstance(want, (int, float)) else got == want
    print(("  ✓ " if ok else "  ✗ ") + name + ": agent=%r sql=%r" % (got, want))
    if not ok:
        FAIL.append(name)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    con = agent.connect(path)
    now = int(time.time() * 1000)
    s, e = agent.local_day_bounds(now)

    # 1) 今日汇总
    t = agent.q_today(con, now)
    d = con.execute(
        "select count(*), coalesce(sum(computed_total_tokens),0), coalesce(sum(output_tokens),0), "
        "coalesce(sum(input_tokens),0) from model_usage where status='completed' "
        "and completed_at>=? and completed_at<?", (s, e)).fetchone()
    check("今日请求数", t["requests"], d[0])
    check("今日总 token", t["total"], d[1])
    check("今日输出 token", t["output"], d[2])
    check("今日输入 token", t["input"], d[3])

    # 2) 当前主会话：总量 / 上下文
    cur = agent.q_current(con, now)
    if not cur:
        print("  (近 30 分钟无活跃主会话，跳过会话检查)")
    else:
        sid = cur["sid"]
        st = agent.q_session(con, sid, now)
        d = con.execute(
            "select count(*), coalesce(sum(computed_total_tokens),0) from model_usage "
            "where session_id=? and status='completed'", (sid,)).fetchone()
        check("会话请求数", st["requests"], d[0])
        check("会话总 token", st["totalTokens"], d[1])
        ctx = con.execute(
            "select input_tokens from model_usage where session_id=? and status='completed' "
            "and completed_at is not null order by completed_at desc, id desc limit 1",
            (sid,)).fetchone()
        check("上下文长度", st["context"], ctx[0] if ctx else 0)

        # 3) 最近 5 次完成请求的加权速率（独立手算口径）
        rows = con.execute(
            "select started_at, first_token_at, completed_at, output_tokens, "
            "reasoning_tokens, model_id from model_usage where session_id=? and "
            "status='completed' and completed_at is not null "
            "order by completed_at desc, id desc limit 5", (sid,)).fetchall()
        out_sum = ms_sum = 0
        peak = 0.0
        for started, first, completed, out, reason, model in rows:
            if not out or completed is None:
                continue
            begin = started if (reason or "claude" in (model or "").lower()) else first
            if begin is None:
                continue
            gen = completed - begin
            if gen < 200 or gen > 3600000:
                continue
            out_sum += out
            ms_sum += gen
            peak = max(peak, out * 1000.0 / gen)
        want_tps = round(out_sum * 1000.0 / ms_sum, 1) if ms_sum else 0.0
        check("最近5次加权速率", st["rate"]["tps"], want_tps, tol=0.1)
        want_last = 0.0
        for started, first, completed, out, reason, model in rows:
            if not out or completed is None:
                continue
            begin = started if (reason or "claude" in (model or "").lower()) else first
            if begin is None:
                continue
            gen = completed - begin
            if gen < 200 or gen > 3600000:
                continue
            want_last = round(out * 1000.0 / gen, 1)
            break
        check("最近一次有效速率", st["lastTps"], want_last, tol=0.1)

    # 4) 60 秒窗口 delta
    since = now - 60000
    dl = agent.q_delta(con, since, now)
    d = con.execute(
        "select count(*), coalesce(sum(computed_total_tokens),0), coalesce(sum(output_tokens),0) "
        "from model_usage where status='completed' and completed_at>? and completed_at<=?",
        (since, now + 1000)).fetchone()
    check("60s 窗口请求数", dl["requests"], d[0])
    check("60s 窗口总 token", dl["total"], d[1])
    check("60s 窗口输出", dl["output"], d[2])
    check("窗口时长 ms", dl["windowMs"], now - since, tol=50)

    con.close()
    print()
    if FAIL:
        print("✗ 存在不一致: %s" % ", ".join(FAIL))
        return 1
    print("✓ 全部指标与独立 SQL 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
