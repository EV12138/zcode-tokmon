#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token monitor agent —— ZCode 用量/速率查询核心（单文件、零依赖、Python 3.8+）。

双重身份：
  1. 本地：被 usage_mcp.py / cli.py / monitor.py import 作为查询库；
  2. 远端：由 remote.py 推送到远程主机 ~/.zcode/token-monitor/agent.py 执行，
     查询远端自己的 ~/.zcode/cli/db/db.sqlite 并输出 JSON。

数据源只读（mode=ro），绝不写入。

速率口径（分子/分母沿真库裁决过的规则）：
  分子 = output_tokens（provider 原生口径已包含 reasoning，不重复相加）；
  分母 = 纯生成时长：
    - 默认（无思考段）: completed_at - first_token_at
    - 思考段（reasoning_tokens>0）: completed_at - started_at（思考 token 在首字前流出）
    - Claude 系模型: reasoning 恒为 0 但 output 已含思考，同样从 started_at 起算
  窗口守卫 200ms ~ 1h，越界样本不参与评分；
  聚合 = Σ(有效样本 output) / Σ(有效样本 genMs) × 1000（加权，绝不用全部 output 当分子）。

CLI：
  agent.py                     人读报表（当前 + 今日）
  agent.py --json              完整快照 JSON（monitor/MCP 用）
  agent.py --json --since MS   快照 + since 之后完成的增量聚合 delta
  agent.py today|week|sessions 子命令报表
"""
import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote

AGENT_VERSION = 1

DB_PATH = os.environ.get(
    "ZCODE_USAGE_DB",
    os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite"),
)

MIN_GEN_MS = 200
MAX_GEN_MS = 3_600_000
SESSION_ACTIVE_MS = 30 * 60 * 1000      # 30 分钟内有请求视为活跃会话
LATEST_N = 5                             # 最近 N 次完成请求算加权速率
RATE_LOOKBACK = 20                       # lastTps 回扫上限（first_token_at 缺失很常见）

MODEL_RATE_COLS = (
    "id, session_id, turn_id, model_id, status, query_source, "
    "started_at, first_token_at, completed_at, duration_ms, "
    "input_tokens, output_tokens, reasoning_tokens, "
    "cache_read_input_tokens, cache_creation_input_tokens, computed_total_tokens"
)


# ---------------------------------------------------------------- 连接

def connect(path=None):
    """只读连接；WAL 侧文件不可用或库不存在时给出明确错误。"""
    path = path or DB_PATH
    real = os.path.realpath(path)
    if not os.path.exists(real):
        raise FileNotFoundError("usage db not found: %s" % path)
    try:
        return sqlite3.connect("file:%s?mode=ro" % quote(real, safe="/"), uri=True, timeout=3)
    except sqlite3.OperationalError:
        # 极少数环境 WAL sidecar 不可用时退回普通连接（仅执行 SELECT）
        return sqlite3.connect(real, timeout=3)


# ---------------------------------------------------------------- 速率口径

def hides_reasoning(model_id):
    """Claude 系：reasoning_tokens 恒为 0 但 output 已含思考，窗口须从 started_at 起算。"""
    return "claude" in (model_id or "").lower()


def gen_window(row):
    """单个完成请求的生成窗口（ms）。返回 (gen_ms, reason)；reason 非 None 表示样本无效。"""
    started = row["started_at"]
    first_tok = row["first_token_at"]
    completed = row["completed_at"]
    out = row["output_tokens"] or 0
    if completed is None:
        return None, "running"
    if out <= 0:
        return None, "noOutput"
    use_started = (row["reasoning_tokens"] or 0) > 0 or hides_reasoning(row["model_id"])
    begin = started if use_started else first_tok
    if begin is None or completed is None:
        return None, "missingGenerationWindow"
    gen = completed - begin
    if gen < MIN_GEN_MS or gen > MAX_GEN_MS:
        return None, "generationWindowOutOfRange"
    return gen, None


def aggregate_rate(rows):
    """rows: model_usage 行（dict）。返回加权速率 + 峰值 + 覆盖率。"""
    rated_out = 0
    rated_ms = 0
    peak = 0.0
    total = len(rows)
    rated_n = 0
    for r in rows:
        gen, _reason = gen_window(r)
        if gen is None:
            continue
        rated_n += 1
        out = r["output_tokens"] or 0
        rated_out += out
        rated_ms += gen
        tps = out * 1000.0 / gen
        if tps > peak:
            peak = tps
    return {
        "tps": round(rated_out * 1000.0 / rated_ms, 1) if rated_ms else 0.0,
        "peak": round(peak, 1),
        "ratedRequests": rated_n,
        "ratedOutput": rated_out,
        "ratedMs": rated_ms,
        "coverage": round(rated_n / total, 3) if total else 0.0,
        "requests": total,
    }


def last_valid_tps(rows):
    """最近一次有效完成请求的速率；first_token 缺失时向前回扫最近 TPS_LOOKBACK 条。"""
    for r in rows:
        gen, _ = gen_window(r)
        if gen is not None:
            return round((r["output_tokens"] or 0) * 1000.0 / gen, 1)
    return 0.0


# ---------------------------------------------------------------- 查询

def _rows(con, sql, args=()):
    con.row_factory = sqlite3.Row
    cur = con.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def local_day_bounds(now_ms=None):
    """本地时区今日 [start_ms, end_ms)。"""
    now = datetime.now() if now_ms is None else datetime.fromtimestamp(now_ms / 1000.0)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def q_today(con, now_ms=None):
    s, e = local_day_bounds(now_ms)
    rows = _rows(con, (
        "select count(*) requests, coalesce(sum(input_tokens),0) input, "
        "coalesce(sum(output_tokens),0) output, "
        "coalesce(sum(reasoning_tokens),0) reasoning, "
        "coalesce(sum(cache_read_input_tokens),0) cache_read, "
        "coalesce(sum(cache_creation_input_tokens),0) cache_write, "
        "coalesce(sum(computed_total_tokens),0) total "
        "from model_usage where status='completed' and completed_at>=? and completed_at<?"
    ), (s, e))
    return rows[0]


def q_days(con, days=7, now_ms=None):
    now = datetime.now() if now_ms is None else datetime.fromtimestamp(now_ms / 1000.0)
    out = []
    for i in range(days):
        day = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        s, e = int(day.timestamp() * 1000), int((day + timedelta(days=1)).timestamp() * 1000)
        rows = _rows(con, (
            "select count(*) requests, coalesce(sum(computed_total_tokens),0) total, "
            "coalesce(sum(output_tokens),0) output from model_usage "
            "where status='completed' and completed_at>=? and completed_at<?"
        ), (s, e))
        r = rows[0]
        r["date"] = day.strftime("%Y-%m-%d")
        out.append(r)
    return out


def _latest_rows(con, session_id, n=LATEST_N):
    """某会话最近 n 条完成请求（新→旧）。"""
    return _rows(con, (
        "select %s from model_usage where session_id=? and status='completed' "
        "and completed_at is not null order by completed_at desc, id desc limit ?"
    ) % MODEL_RATE_COLS, (session_id, n))


def q_active_sessions(con, now_ms=None, limit=16):
    """活跃会话（30 分钟内有请求，含 running），附每会话速率。"""
    now_ms = now_ms or int(time.time() * 1000)
    sessions = _rows(con, (
        "select m.session_id sid, max(coalesce(m.completed_at, m.started_at)) last_ms, "
        "sum(case when m.status='running' then 1 else 0 end) running, "
        "count(*) requests, coalesce(sum(case when m.status='completed' "
        "then m.computed_total_tokens else 0 end),0) total_tokens "
        "from model_usage m group by m.session_id "
        "having last_ms > ? order by last_ms desc limit ?"
    ), (now_ms - SESSION_ACTIVE_MS, limit))
    out = []
    for s in sessions:
        meta = _rows(con, (
            "select title, directory, task_type, parent_id, time_created "
            "from session where id=?"
        ), (s["sid"],))
        latest = _latest_rows(con, s["sid"], RATE_LOOKBACK)
        running_row = _rows(con, (
            "select started_at, model_id from model_usage "
            "where session_id=? and status='running' order by started_at desc limit 1"
        ), (s["sid"],))
        item = {
            "sid": s["sid"],
            "title": (meta[0]["title"] if meta else "")[:40],
            "directory": (os.path.basename(meta[0]["directory"]) if meta else "")[:30],
            "taskType": (meta[0]["task_type"] if meta else "interactive"),
            "isSubagent": bool(meta and meta[0]["parent_id"]),
            "lastActiveMs": s["last_ms"],
            "idleSec": max(0, (now_ms - s["last_ms"]) // 1000),
            "running": s["running"],
            "requests": s["requests"],
            "totalTokens": s["total_tokens"],
            "rate": aggregate_rate(latest[:LATEST_N]),
            "lastTps": last_valid_tps(latest),
        }
        if running_row:
            item["runningModel"] = running_row[0]["model_id"]
            item["runningElapsedSec"] = max(0, (now_ms - running_row[0]["started_at"]) // 1000)
        out.append(item)
    return out


def q_session(con, sid, now_ms=None):
    """按会话 id 精确查询（不受 30 分钟活跃窗口/条数上限影响）。

    悬浮条按窗口真实会话 id 请求它——本地库没有该 id 时返回 None（可能是远端会话）。
    """
    now_ms = now_ms or int(time.time() * 1000)
    meta = _rows(con, (
        "select title, directory, task_type, parent_id from session where id=?"
    ), (sid,))
    if not meta:
        return None
    agg = _rows(con, (
        "select count(*) requests, coalesce(sum(computed_total_tokens),0) total, "
        "coalesce(sum(input_tokens),0) input, coalesce(sum(output_tokens),0) output "
        "from model_usage where session_id=? and status='completed'"
    ), (sid,))[0]
    latest = _latest_rows(con, sid, RATE_LOOKBACK)
    running_row = _rows(con, (
        "select started_at, model_id from model_usage "
        "where session_id=? and status='running' order by started_at desc limit 1"
    ), (sid,))
    last_row = _rows(con, (
        "select max(coalesce(completed_at, started_at)) last_ms from model_usage where session_id=?"
    ), (sid,))
    last_ms = (last_row[0]["last_ms"] if last_row else None) or now_ms
    ctx_row = _rows(con, (
        "select input_tokens from model_usage where session_id=? and status='completed' "
        "and completed_at is not null order by completed_at desc, id desc limit 1"
    ), (sid,))
    item = {
        "sid": sid,
        "title": (meta[0]["title"] or "")[:40],
        "directory": os.path.basename(meta[0]["directory"] or "")[:30],
        "taskType": meta[0]["task_type"],
        "isSubagent": bool(meta[0]["parent_id"]),
        "lastActiveMs": last_ms,
        "idleSec": max(0, (now_ms - last_ms) // 1000),
        "running": 1 if running_row else 0,
        "requests": agg["requests"],
        "totalTokens": agg["total"],
        "input": agg["input"],
        "output": agg["output"],
        "context": (ctx_row[0]["input_tokens"] if ctx_row else 0),
        "rate": aggregate_rate(latest[:LATEST_N]),
        "lastTps": last_valid_tps(latest),
    }
    if running_row:
        item["runningModel"] = running_row[0]["model_id"]
        item["runningElapsedSec"] = max(0, (now_ms - running_row[0]["started_at"]) // 1000)
    return item


def q_current(con, now_ms=None):
    """最近活跃会话的聚焦视图（活跃会话里 last_ms 最大的主会话）。"""
    now_ms = now_ms or int(time.time() * 1000)
    rows = _rows(con, (
        "select m.session_id sid, max(coalesce(m.completed_at, m.started_at)) last_ms "
        "from model_usage m join session s on s.id = m.session_id "
        "where s.parent_id is null and s.task_type != 'subagent_child' "
        "group by m.session_id having last_ms > ? order by last_ms desc limit 1"
    ), (now_ms - SESSION_ACTIVE_MS,))
    if not rows:
        return None
    sid = rows[0]["sid"]
    agg = _rows(con, (
        "select count(*) requests, coalesce(sum(input_tokens),0) input, "
        "coalesce(sum(output_tokens),0) output, "
        "coalesce(sum(cache_read_input_tokens),0) cache_read, "
        "coalesce(sum(computed_total_tokens),0) total, "
        "(select m2.input_tokens from model_usage m2 where m2.session_id=? "
        " and m2.status='completed' order by m2.completed_at desc, m2.id desc limit 1) ctx "
        "from model_usage where session_id=? and status='completed'"
    ), (sid, sid))[0]
    latest = _latest_rows(con, sid, RATE_LOOKBACK)
    last_turn = _rows(con, (
        "select turn_id from model_usage where session_id=? and turn_id is not null "
        "and status='completed' order by completed_at desc limit 1"
    ), (sid,))
    turn_rate = None
    if last_turn:
        turn_rows = _rows(con, (
            "select %s from model_usage where session_id=? and turn_id=? and status='completed' "
            "and completed_at is not null order by completed_at desc"
        ) % MODEL_RATE_COLS, (sid, last_turn[0]["turn_id"]))
        turn_rate = aggregate_rate(turn_rows)
    meta = _rows(con, "select title, directory from session where id=?", (sid,))
    return {
        "sid": sid,
        "title": (meta[0]["title"] if meta else "")[:48],
        "directory": meta[0]["directory"] if meta else "",
        "requests": agg["requests"],
        "input": agg["input"],
        "output": agg["output"],
        "cacheRead": agg["cache_read"],
        "total": agg["total"],
        "context": agg["ctx"] or 0,
        "rate": aggregate_rate(latest[:LATEST_N]),
        "turnRate": turn_rate,
    }


def q_delta(con, since_ms, now_ms=None):
    """since 之后完成的请求聚合（轮询窗口速率）+ 当前 running 数。

    总量用 SQL 聚合（不因采样上限截断）；速率用最近 500 条明细加权
    （500 条在 1.5s 轮询窗口内远超实际并发，标 sampleTruncated 备查）。
    """
    now_ms = now_ms or int(time.time() * 1000)
    agg = _rows(con, (
        "select count(*) requests, coalesce(sum(computed_total_tokens),0) total, "
        "coalesce(sum(input_tokens),0) input, coalesce(sum(output_tokens),0) output "
        "from model_usage where status='completed' and completed_at>? and completed_at<=?"
    ), (since_ms, now_ms + 1000))[0]
    rows = _rows(con, (
        "select %s from model_usage where status='completed' and completed_at>? "
        "and completed_at<=? order by completed_at desc limit 500"
    ) % MODEL_RATE_COLS, (since_ms, now_ms + 1000))
    running = _rows(con, (
        "select count(*) n, min(started_at) earliest from model_usage where status='running'"
    ))[0]
    delta = aggregate_rate(rows)
    delta["sinceMs"] = since_ms
    delta["windowMs"] = max(0, now_ms - since_ms)
    delta["requests"] = agg["requests"]
    delta["total"] = agg["total"]
    delta["input"] = agg["input"]
    delta["output"] = agg["output"]
    delta["sampled"] = len(rows)
    delta["sampleTruncated"] = agg["requests"] > len(rows)
    delta["running"] = running["n"]
    if running["n"] and running["earliest"]:
        delta["runningElapsedSec"] = max(0, (now_ms - running["earliest"]) // 1000)
    return delta


def snapshot(con, since_ms=None, now_ms=None, sids=None):
    """完整快照：今日 + 活跃会话 + 当前会话 + 指定会话明细 + （可选）增量窗口。"""
    now_ms = now_ms or int(time.time() * 1000)
    snap = {
        "agentVersion": AGENT_VERSION,
        "generatedAtMs": now_ms,
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "today": q_today(con, now_ms),
        "sessions": q_active_sessions(con, now_ms),
        "current": q_current(con, now_ms),
    }
    if sids:
        snap["sidStats"] = {sid: q_session(con, sid, now_ms) for sid in sids}
    if since_ms is not None:
        snap["delta"] = q_delta(con, since_ms, now_ms)
    return snap


# ---------------------------------------------------------------- 渲染

def fmt_tok(n):
    n = n or 0
    if n >= 1_000_000:
        return "%.2fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.1fk" % (n / 1_000)
    return str(n)


def fmt_tps(tps):
    return ("%.1f" % tps) if tps else "—"


def render_now(snap):
    """人读：当前会话 + 今日 + 活跃会话表。"""
    lines = []
    today = snap["today"]
    lines.append("【今日】总 %s（输入 %s · 输出 %s · 缓存读 %s · 缓存写 %s · %d 次请求）" % (
        fmt_tok(today["total"]), fmt_tok(today["input"]), fmt_tok(today["output"]),
        fmt_tok(today["cache_read"]), fmt_tok(today["cache_write"]), today["requests"]))
    cur = snap.get("current")
    if cur:
        lines.append("【当前会话】%s（%s）" % (cur["title"] or cur["sid"][:20],
                                             os.path.basename(cur["directory"] or "") or "—"))
        lines.append("  累计 %s（输入 %s · 输出 %s · 上下文 %s · %d 次请求）" % (
            fmt_tok(cur["total"]), fmt_tok(cur["input"]), fmt_tok(cur["output"]),
            fmt_tok(cur["context"]), cur["requests"]))
        r = cur.get("rate") or {}
        tr = cur.get("turnRate") or {}
        lines.append("  速率: 最近%d次加权 %s tok/s · 峰值 %s · 上轮 %s（%s tok / %d 请求）" % (
            r.get("requests", 0), fmt_tps(r.get("tps")), fmt_tps(r.get("peak")),
            fmt_tps(tr.get("tps")), fmt_tok(tr.get("ratedOutput", tr.get("total", 0))),
            tr.get("requests", 0)))
    else:
        lines.append("【当前会话】近 30 分钟无活跃会话")
    sess = snap.get("sessions") or []
    if sess:
        lines.append("【活跃会话】")
        for s in sess:
            state = "⚡生成中" if s["running"] else "idle %ds" % s["idleSec"]
            sub = " · 子代理" if s.get("isSubagent") else ""
            lines.append("  - %s%s [%s] 总%s · 最近 %s tok/s · %s" % (
                s["title"] or s["sid"][:16], sub, s["directory"] or "—",
                fmt_tok(s["totalTokens"]), fmt_tps(s["lastTps"]), state))
    return "\n".join(lines)


def render_days(days):
    con = connect()
    try:
        rows = q_days(con, days)
        total = sum(r["total"] for r in rows)
        out = ["【近 %d 天】合计 %s" % (days, fmt_tok(total))]
        for r in rows:
            out.append("  %s  %s（输出 %s · %d 次请求）" % (
                r["date"], fmt_tok(r["total"]), fmt_tok(r["output"]), r["requests"]))
        return "\n".join(out)
    finally:
        con.close()


# ---------------------------------------------------------------- serve（悬浮条数据泵）

def serve(remote_poll_s=3.0):
    """stdin/stdout 行协议（inject-main.cjs 常驻调用）。

    每读一行请求（{"since": <ms>, "sids": [...]} ）输出一行响应：
      {"local": snapshot(含 delta), "remote": {key: {ok,label,latency_ms,data|error,ageMs}}, "ts": ms}

    远程查询在后台线程刷新（节流 remote_poll_s）：SSH 慢/主机不可达时
    只影响远程数据的新鲜度，绝不阻塞本地快照与 UI 刷新。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import config as cfg_mod
        import remote as remote_mod
    except ImportError as e:
        sys.stderr.write("[tokmon-agent] serve needs local plugin dir: %s\n" % e)
        return 1
    cfg = cfg_mod.load()
    remote_poll_s = float(cfg.get("remote", {}).get("poll_s", 3)) or remote_poll_s
    targets = remote_mod.load_targets(cfg)

    state = {"remote": {}, "refreshed": 0.0, "sids": frozenset(), "refreshing": False}
    lock = threading.Lock()

    def do_refresh(sids):
        try:
            res = remote_mod.query_all(targets, cfg, sids=sids)
            for key, r in res.items():
                r["ts"] = int(time.time() * 1000)
            with lock:
                state["remote"] = res
                state["sids"] = frozenset(sids)
                state["refreshed"] = time.monotonic()
        except Exception as e:  # noqa: BLE001
            with lock:
                state["refreshed"] = time.monotonic()  # 失败也节流，避免每请求都撞 SSH
            sys.stderr.write("[tokmon-agent] remote refresh error: %s\n" % e)
        finally:
            with lock:
                state["refreshing"] = False

    def maybe_refresh(sids):
        """按节流策略触发后台刷新；会话集合变化时立即刷新一次。"""
        if not targets:
            return
        now = time.monotonic()
        with lock:
            if state["refreshing"]:
                return
            same = frozenset(sids) == state["sids"]
            if same and now - state["refreshed"] < remote_poll_s:
                return
            state["refreshing"] = True
        threading.Thread(target=do_refresh, args=(list(sids),), daemon=True).start()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            req = {}
        since = req.get("since")
        sids = []
        raw_sids = req.get("sids")
        if isinstance(raw_sids, list):
            for item in raw_sids[:8]:
                if isinstance(item, str):
                    item = item.strip()
                    if item and len(item) <= 120:
                        sids.append(item)
        maybe_refresh(sids)
        con = connect()
        try:
            snap = snapshot(con, since_ms=since, sids=sids)
        finally:
            con.close()
        now_ms = int(time.time() * 1000)
        with lock:
            remote_src = state["remote"]
        remote_view = {}
        for key, r in remote_src.items():
            item = dict(r)
            item["ageMs"] = now_ms - int(r.get("ts") or 0)
            remote_view[key] = item
        out = {"local": snap, "remote": remote_view, "ts": now_ms}
        print(json.dumps(out, ensure_ascii=False), flush=True)
    sys.stderr.write("[tokmon-agent] stdin EOF, exiting\n")
    sys.stderr.flush()
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="ZCode token 用量/速率查询 agent")
    ap.add_argument("scope", nargs="?", default="now",
                    help="now | today | week | days:N | serve")
    ap.add_argument("--json", action="store_true", help="输出 JSON 快照")
    ap.add_argument("--since", type=int, default=None, metavar="MS",
                    help="epoch 毫秒；附带返回此后完成请求的增量聚合 delta")
    ap.add_argument("--sid", action="append", default=[], metavar="ID",
                    help="指定会话 id（可重复/逗号分隔），返回该会话明细 sidStats")
    args = ap.parse_args(argv)

    sids = []
    for item in args.sid:
        sids += [x.strip() for x in item.split(",") if x.strip()]
    sids = sids[:8]

    if args.scope == "serve":
        return serve()

    if args.scope == "week":
        print(render_days(7))
        return
    if args.scope.startswith("days:"):
        print(render_days(max(1, min(90, int(args.scope.split(":", 1)[1])))))
        return

    con = connect()
    try:
        snap = snapshot(con, since_ms=args.since, sids=sids)
    finally:
        con.close()
    if args.json:
        print(json.dumps(snap, ensure_ascii=False))
    elif args.scope == "today":
        t = snap["today"]
        print("【今日】总 %s（输入 %s · 输出 %s · 缓存读 %s · 缓存写 %s · %d 次请求）" % (
            fmt_tok(t["total"]), fmt_tok(t["input"]), fmt_tok(t["output"]),
            fmt_tok(t["cache_read"]), fmt_tok(t["cache_write"]), t["requests"]))
    else:
        print(render_now(snap))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
