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

# ZCode 自己的模型配置：provider.*.models.*.limit.context 是用户在模型设置里看到的值
MODEL_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".zcode", "v2", "config.json")
PROVIDER_RULES_PATH = os.path.join(os.path.expanduser("~"), ".zcode", "v2", "provider_config.json")

MODEL_RATE_COLS = (
    "id, session_id, turn_id, provider_id, model_id, status, query_source, "
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


def cache_hit_rate(cache_read, input_tokens):
    """缓存命中率（%）：cache_read / input；input 已含 cache read。"""
    if not input_tokens:
        return 0.0
    return round((cache_read or 0) * 100.0 / input_tokens, 1)


# ---------------------------------------------------------------- 上下文窗口

_MODEL_CTX_CACHE = {"sig": None, "catalog": None}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _stat_sig(paths):
    sig = []
    for path in paths:
        try:
            st = os.stat(path)
            sig.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((path, 0, 0))
    return tuple(sig)


def build_model_context_catalog(config_data=None, provider_data=None):
    """从 ZCode 配置构建上下文窗口目录。

    provider_config.json 的模型规则是模型设置界面的当前值，优先级高于
    config.json 的 provider 模型目录（legacy）。返回：
      scoped:  {(provider_id, model_id) -> (window, source)}
      by_model: {model_id -> (window, source)}
    """
    best = {}  # (pid, mid) -> (priority, window, source)

    def add(pid, mid, win, priority, source):
        try:
            win = int(win or 0)
        except (TypeError, ValueError):
            return
        if not mid or win <= 0:
            return
        mid = str(mid).lower()
        pid = str(pid or "").lower()
        key = (pid, mid)
        old = best.get(key)
        if old is None or priority > old[0] or (priority == old[0] and win > old[1]):
            best[key] = (priority, win, source)

    providers = ((config_data or {}).get("provider") or {})
    if isinstance(providers, dict):
        for pid, prov in providers.items():
            models = (prov or {}).get("models") if isinstance(prov, dict) else None
            if not isinstance(models, dict):
                continue
            for mid, model in models.items():
                limit = (model or {}).get("limit") if isinstance(model, dict) else None
                if isinstance(limit, dict):
                    add(pid, mid, limit.get("context"), 20, "zcode-config")

    rules = (((provider_data or {}).get("config") or {}).get("modelConfigRules") or {})
    if isinstance(rules, dict):
        for bucket in ("providerModelRules", "manualProviderModelRules", "modelRules"):
            for rule in rules.get(bucket) or []:
                if not isinstance(rule, dict):
                    continue
                props = ((rule.get("config") or {}).get("properties") or {})
                add(rule.get("providerId"), rule.get("modelId"),
                    props.get("contextWindow"), 30, "zcode-rules")

    scoped = {key: (win, src) for key, (_pri, win, src) in best.items()}
    by_model = {}
    for (_pid, mid), (pri, win, src) in best.items():
        cur = by_model.get(mid)
        if cur is None or (pri, win) > (cur[0], cur[1]):
            by_model[mid] = (pri, win, src)
    return {
        "scoped": scoped,
        "by_model": {mid: (win, src) for mid, (_pri, win, src) in by_model.items()},
    }


def model_context_catalog():
    """带 mtime 缓存的目录；模型配置变化后下一次查询自动生效。"""
    paths = (MODEL_CONFIG_PATH, PROVIDER_RULES_PATH)
    sig = _stat_sig(paths)
    if _MODEL_CTX_CACHE["sig"] == sig:
        return _MODEL_CTX_CACHE["catalog"]
    catalog = build_model_context_catalog(_read_json(MODEL_CONFIG_PATH),
                                          _read_json(PROVIDER_RULES_PATH))
    _MODEL_CTX_CACHE["sig"] = sig
    _MODEL_CTX_CACHE["catalog"] = catalog
    return catalog


def resolve_model_context_ex(model_id, provider_id="", catalog=None,
                             user_windows=None, user_default=0):
    """模型 -> (窗口, 来源)。优先插件显式覆盖，其次该 provider 的模型配置。"""
    if not model_id:
        return (0, "")
    model = str(model_id).lower()
    provider = str(provider_id or "").lower()
    best_win, best_len = 0, -1
    for key, value in (user_windows or {}).items():
        try:
            win = int(value)
        except (TypeError, ValueError):
            continue
        if win <= 0:
            continue
        key = str(key).lower()
        if (provider and key == provider + "/" + model) or key == model:
            return (win, "plugin-config")
        if key and key in model and len(key) > best_len:
            best_win, best_len = win, len(key)
    if best_win:
        return (best_win, "plugin-config")
    catalog = catalog or model_context_catalog()
    if provider:
        hit = catalog["scoped"].get((provider, model))
        if hit:
            return hit
    hit = catalog["by_model"].get(model)
    if hit:
        return hit
    if "/" in model:
        hit = catalog["by_model"].get(model.split("/", 1)[1])
        if hit:
            return hit
    try:
        user_default = int(user_default or 0)
    except (TypeError, ValueError):
        user_default = 0
    return (user_default, "default") if user_default > 0 else (0, "")


def resolve_model_context(model_id, provider_id="", catalog=None,
                          user_windows=None, user_default=0):
    return resolve_model_context_ex(model_id, provider_id, catalog,
                                    user_windows, user_default)[0]


def attach_context_windows(snap, catalog=None, user_windows=None, user_default=0):
    """给会话/当前视图附上 contextWindow 与来源，覆盖本地与远端快照。"""
    catalog = catalog or model_context_catalog()

    def apply(item):
        if not isinstance(item, dict):
            return
        model = item.get("model") or item.get("runningModel") or ""
        provider = item.get("provider") or item.get("runningProvider") or ""
        win, source = resolve_model_context_ex(model, provider, catalog,
                                               user_windows, user_default)
        if win:
            item["contextWindow"] = win
            item["contextWindowSource"] = source

    apply(snap.get("current"))
    for item in snap.get("sessions") or []:
        apply(item)
    for item in (snap.get("sidStats") or {}).values():
        apply(item)
    return snap


_EMPTY_MODEL_CATALOG = {"scoped": {}, "by_model": {}}


def apply_user_windows(snap, user_windows):
    """只应用插件 config 的显式覆盖；不覆盖远端自己的模型配置。"""
    if not user_windows:
        return snap

    def apply(item):
        if not isinstance(item, dict):
            return
        model = item.get("model") or item.get("runningModel") or ""
        provider = item.get("provider") or item.get("runningProvider") or ""
        win, _ = resolve_model_context_ex(model, provider, _EMPTY_MODEL_CATALOG,
                                          user_windows, 0)
        if win:
            item["contextWindow"] = win
            item["contextWindowSource"] = "plugin-config"

    apply(snap.get("current"))
    for item in snap.get("sessions") or []:
        apply(item)
    for item in (snap.get("sidStats") or {}).values():
        apply(item)
    return snap


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
        "then m.computed_total_tokens else 0 end),0) total_tokens, "
        "coalesce(sum(case when m.status='completed' then m.input_tokens else 0 end),0) input_tokens, "
        "coalesce(sum(case when m.status='completed' then m.cache_read_input_tokens else 0 end),0) cache_read "
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
            "select started_at, model_id, provider_id from model_usage "
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
            "input": s["input_tokens"],
            "cacheRead": s["cache_read"],
            "cacheHitRate": cache_hit_rate(s["cache_read"], s["input_tokens"]),
            "model": (latest[0]["model_id"] if latest else ""),
            "provider": (latest[0]["provider_id"] if latest else ""),
            "rate": aggregate_rate(latest[:LATEST_N]),
            "lastTps": last_valid_tps(latest),
        }
        if running_row:
            item["runningModel"] = running_row[0]["model_id"]
            item["runningProvider"] = running_row[0]["provider_id"]
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
        "coalesce(sum(input_tokens),0) input, coalesce(sum(output_tokens),0) output, "
        "coalesce(sum(reasoning_tokens),0) reasoning, "
        "coalesce(sum(cache_read_input_tokens),0) cache_read, "
        "coalesce(sum(cache_creation_input_tokens),0) cache_write "
        "from model_usage where session_id=? and status='completed'"
    ), (sid,))[0]
    latest = _latest_rows(con, sid, RATE_LOOKBACK)
    running_row = _rows(con, (
        "select started_at, model_id, provider_id from model_usage "
        "where session_id=? and status='running' order by started_at desc limit 1"
    ), (sid,))
    last_row = _rows(con, (
        "select max(coalesce(completed_at, started_at)) last_ms from model_usage where session_id=?"
    ), (sid,))
    last_ms = (last_row[0]["last_ms"] if last_row else None) or now_ms
    ctx_row = _rows(con, (
        "select input_tokens, model_id, provider_id from model_usage where session_id=? and status='completed' "
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
        "reasoning": agg["reasoning"],
        "cacheRead": agg["cache_read"],
        "cacheWrite": agg["cache_write"],
        "cacheHitRate": cache_hit_rate(agg["cache_read"], agg["input"]),
        "model": (ctx_row[0]["model_id"] if ctx_row else (latest[0]["model_id"] if latest else "")),
        "provider": (ctx_row[0]["provider_id"] if ctx_row else (latest[0]["provider_id"] if latest else "")),
        "context": (ctx_row[0]["input_tokens"] if ctx_row else 0),
        "rate": aggregate_rate(latest[:LATEST_N]),
        "lastTps": last_valid_tps(latest),
    }
    if running_row:
        item["runningModel"] = running_row[0]["model_id"]
        item["runningProvider"] = running_row[0]["provider_id"]
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
        " and m2.status='completed' order by m2.completed_at desc, m2.id desc limit 1) ctx, "
        "(select m2.model_id from model_usage m2 where m2.session_id=? "
        " and m2.status='completed' order by m2.completed_at desc, m2.id desc limit 1) model, "
        "(select m2.provider_id from model_usage m2 where m2.session_id=? "
        " and m2.status='completed' order by m2.completed_at desc, m2.id desc limit 1) provider "
        "from model_usage where session_id=? and status='completed'"
    ), (sid, sid, sid, sid))[0]
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
        "cacheHitRate": cache_hit_rate(agg["cache_read"], agg["input"]),
        "model": agg["model"] or (latest[0]["model_id"] if latest else ""),
        "provider": agg["provider"] or (latest[0]["provider_id"] if latest else ""),
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


def q_models(con, days=7, now_ms=None):
    """按模型聚合（默认近 7 天）：请求数、输入/输出/缓存、总量、缓存命中率、平均 TTFT。"""
    now_ms = now_ms or int(time.time() * 1000)
    days = max(1, min(365, int(days)))
    since = now_ms - days * 86400000
    rows = _rows(con, (
        "select provider_id, model_id, count(*) requests, "
        "coalesce(sum(input_tokens),0) input, coalesce(sum(output_tokens),0) output, "
        "coalesce(sum(reasoning_tokens),0) reasoning, "
        "coalesce(sum(cache_read_input_tokens),0) cache_read, "
        "coalesce(sum(cache_creation_input_tokens),0) cache_write, "
        "coalesce(sum(computed_total_tokens),0) total, "
        "avg(case when time_to_first_token_ms is not null then time_to_first_token_ms end) avg_ttft_ms "
        "from model_usage where status='completed' and completed_at>=? and completed_at<=? "
        "group by provider_id, model_id order by total desc"
    ), (since, now_ms + 1000))
    for r in rows:
        r["cacheHitRate"] = cache_hit_rate(r["cache_read"], r["input"])
        r["avgTtftMs"] = round(r["avg_ttft_ms"]) if r["avg_ttft_ms"] is not None else None
    return rows


def q_recent(con, n=10):
    """最近 N 次完成请求（含模型、TTFT、重试、工具调用、速率）。"""
    n = max(1, min(100, int(n)))
    rows = _rows(con, (
        "select m.id, m.session_id, s.title, m.model_id, m.provider_id, m.turn_id, "
        "m.started_at, m.first_token_at, m.completed_at, m.duration_ms, "
        "m.time_to_first_token_ms, m.input_tokens, m.output_tokens, m.reasoning_tokens, "
        "m.cache_read_input_tokens, m.cache_creation_input_tokens, m.computed_total_tokens, "
        "m.retry_count, m.tool_call_count, m.status "
        "from model_usage m left join session s on s.id = m.session_id "
        "where m.status='completed' and m.completed_at is not null "
        "order by m.completed_at desc, m.id desc limit ?"
    ), (n,))
    for r in rows:
        gen, why = gen_window(r)
        r["tps"] = round((r["output_tokens"] or 0) * 1000.0 / gen, 1) if gen else 0.0
        r["rateReason"] = why
        r["cacheHitRate"] = cache_hit_rate(r["cache_read_input_tokens"], r["input_tokens"])
        r["title"] = (r["title"] or "")[:40]
    return rows


def render_models(models, days=7):
    lines = ["【按模型 · 近 %d 天】" % days]
    if not models:
        lines.append("  无完成请求")
        return "\n".join(lines)
    lines.append("  %-22s %6s %9s %9s %9s %9s %8s %8s" % (
        "模型", "请求", "输入", "输出", "缓存读", "总计", "命中率", "均TTFT"))
    for m in models:
        ttft = ("%.1fs" % (m["avgTtftMs"] / 1000.0)) if m.get("avgTtftMs") else "—"
        lines.append("  %-22s %6d %9s %9s %9s %9s %7.1f%% %8s" % (
            (m["model_id"] or "?")[:22], m["requests"], fmt_tok(m["input"]),
            fmt_tok(m["output"]), fmt_tok(m["cache_read"]), fmt_tok(m["total"]),
            m["cacheHitRate"], ttft))
    return "\n".join(lines)


def render_recent(rows):
    lines = ["【最近 %d 次完成请求】" % len(rows)]
    if not rows:
        lines.append("  无完成请求")
        return "\n".join(lines)
    lines.append("  %-8s %-14s %8s %8s %8s %8s %6s %4s %4s  %s" % (
        "时间", "模型", "输入", "输出", "总计", "速率", "TTFT", "重试", "工具", "会话"))
    for r in rows:
        t = time.strftime("%H:%M:%S", time.localtime((r["completed_at"] or 0) / 1000.0))
        ttft = ("%.1fs" % (r["time_to_first_token_ms"] / 1000.0)) if r.get("time_to_first_token_ms") else "—"
        tps = ("%.1f t/s" % r["tps"]) if r.get("tps") else "—"
        lines.append("  %-8s %-14s %8s %8s %8s %8s %6s %4d %4d  %s" % (
            t, (r["model_id"] or "?")[:14], fmt_tok(r["input_tokens"]),
            fmt_tok(r["output_tokens"]), fmt_tok(r["computed_total_tokens"]), tps, ttft,
            r["retry_count"] or 0, r["tool_call_count"] or 0, (r["title"] or "")[:18]))
    return "\n".join(lines)


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
    attach_context_windows(snap)
    # 本机模型配置的全量窗口：切换模型后前端可立即按新模型重算
    cat = model_context_catalog()
    windows = {}
    for mid, (win, _src) in cat["by_model"].items():
        if win:
            windows[mid] = win
    for (pid, mid), (win, _src) in cat["scoped"].items():
        if pid and win:
            windows[pid + "/" + mid] = win
    snap["contextWindows"] = windows
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
    def _mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return 0

    cfg = cfg_mod.load()
    remote_poll_s = float(cfg.get("remote", {}).get("poll_s", 3)) or remote_poll_s
    config_path = getattr(cfg_mod, "CONFIG_PATH", "")
    setting_path = getattr(remote_mod, "SETTING_PATH", "")
    runtime = {
        "cfg": cfg,
        "targets": remote_mod.load_targets(cfg),
        "sigs": (_mtime(config_path), _mtime(setting_path)),
    }

    state = {"remote": {}, "refreshed": 0.0, "sids": frozenset(), "refreshing": False}
    lock = threading.Lock()

    def target_sig(targets):
        return tuple(sorted(
            (t.key, t.label, t.user or "", t.host or "", t.port, tuple(t.ssh_argv or ()))
            for t in targets))

    def reload_if_changed():
        """config.json / setting.json 变化时热重载目标，新连的 SSH 工作区无需重启数据泵。

        只有目标集合/参数真正变化才强制远程刷新；ZCode 会频繁写 setting.json，
        无关字段变化只更新配置，不触发 SSH 轮询。
        """
        sigs = (_mtime(config_path), _mtime(setting_path))
        if sigs == runtime["sigs"]:
            return
        now = time.monotonic()
        if now - runtime.get("last_reload", 0) < 1.0:
            return   # 去抖：避免高频写入时每个请求都读取/解析
        runtime["last_reload"] = now
        try:
            new_cfg = cfg_mod.load()
            new_targets = remote_mod.load_targets(new_cfg)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("[tokmon-agent] reload targets failed: %s\n" % e)
            return
        changed = target_sig(new_targets) != target_sig(runtime["targets"])
        runtime["cfg"] = new_cfg
        runtime["targets"] = new_targets
        runtime["sigs"] = sigs
        if changed:
            with lock:
                state["refreshed"] = 0.0   # 强制下一轮刷新
            sys.stderr.write("[tokmon-agent] targets reloaded: %d\n" % len(new_targets))

    def do_refresh(sids, targets, cfg):
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
        targets = runtime["targets"]
        if not targets:
            with lock:
                state["remote"] = {}
            return
        now = time.monotonic()
        with lock:
            if state["refreshing"]:
                return
            same = frozenset(sids) == state["sids"]
            if same and now - state["refreshed"] < remote_poll_s:
                return
            state["refreshing"] = True
        threading.Thread(target=do_refresh, args=(list(sids), list(targets), runtime["cfg"]),
                         daemon=True).start()

    def collect_models(snap, remote_view):
        pairs = set()

        def add(obj):
            if isinstance(obj, dict):
                m = obj.get("model") or obj.get("runningModel")
                if m:
                    pairs.add((obj.get("provider") or obj.get("runningProvider") or "", m))

        add(snap.get("current") or {})
        for sess in snap.get("sessions") or []:
            add(sess)
        for stat in (snap.get("sidStats") or {}).values():
            add(stat)
        for res in remote_view.values():
            data = res.get("data") or {}
            add(data.get("current") or {})
            for sess in data.get("sessions") or []:
                add(sess)
            for stat in (data.get("sidStats") or {}).values():
                add(stat)
        return pairs

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
        reload_if_changed()
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
        cfg_now = runtime["cfg"]
        user_windows = cfg_now.get("context_windows") or {}
        user_default = cfg_now.get("context_window_default") or 0
        catalog = model_context_catalog()
        # 本地快照：模型设置里的窗口；显式 config 覆盖优先
        attach_context_windows(snap, catalog, user_windows, user_default)
        apply_user_windows(snap, user_windows)
        # 远端快照由各自主机 agent 附带自己的模型配置，仅应用本地显式覆盖
        for item in remote_view.values():
            apply_user_windows(item.get("data") or {}, user_windows)
        windows = {}
        for provider, model_id in collect_models(snap, remote_view):
            win = resolve_model_context(model_id, provider, catalog,
                                        user_windows, user_default)
            if win:
                windows[model_id] = win
                if provider:
                    windows[provider + "/" + model_id] = win
        # 把模型配置里的所有模型也带上：切换模型后前端可立即按新窗口重算，
        # 不必等该模型产生第一条请求。
        for mid, (win, _src) in catalog["by_model"].items():
            resolved = resolve_model_context(mid, "", catalog, user_windows, user_default)
            if resolved:
                windows.setdefault(mid, resolved)
        for (pid, mid), (win, _src) in catalog["scoped"].items():
            if not pid:
                continue
            resolved = resolve_model_context(mid, pid, catalog, user_windows, user_default)
            if resolved:
                windows[pid + "/" + mid] = resolved
        out = {
            "local": snap,
            "remote": remote_view,
            "ts": now_ms,
            "settings": {
                "contextWindows": windows,
            },
        }
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
    if args.scope == "models" or args.scope.startswith("models:"):
        days = max(1, min(365, int(args.scope.split(":", 1)[1]))) if ":" in args.scope else 7
        con = connect()
        try:
            print(render_models(q_models(con, days), days))
        finally:
            con.close()
        return
    if args.scope == "recent" or args.scope.startswith("recent:"):
        n = max(1, min(100, int(args.scope.split(":", 1)[1]))) if ":" in args.scope else 10
        con = connect()
        try:
            print(render_recent(q_recent(con, n)))
        finally:
            con.close()
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
