#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token monitor 实时面板（零依赖，ANSI 重绘）。

  python3 monitor.py                 常驻面板（Ctrl+C 退出）
  python3 monitor.py --once          单帧快照（AI 会话内友好，无 ANSI）
  python3 monitor.py --duration 10   限时跑 10 秒后退出
  python3 monitor.py --interval 1    1 秒刷新
  python3 monitor.py --no-remote     只看本地
"""
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent  # noqa: E402
import config as cfg_mod  # noqa: E402

LINE = "─" * 62
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
CLEAR = "\033[H\033[2J"


def _t(ts_ms):
    return time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000.0))


def frame_local(snap, delta):
    out = []
    host = snap.get("host") or "localhost"
    t = snap.get("today") or {}
    out.append("%s ● 本地%s %s" % (GREEN, RESET, host))
    out.append("  今日 %s tok · 输出 %s · %d 次请求" % (
        agent.fmt_tok(t.get("total")), agent.fmt_tok(t.get("output")), t.get("requests", 0)))
    cur = snap.get("current")
    if cur:
        r = cur.get("rate") or {}
        tr = cur.get("turnRate") or {}
        out.append("  当前会话「%s」累计 %s · 上下文 %s" % (
            (cur.get("title") or "")[:24], agent.fmt_tok(cur.get("total")),
            agent.fmt_tok(cur.get("context"))))
        out.append("  速率 最近%d次加权 %s tok/s · 峰值 %s · 上轮 %s tok/s（%s tok）" % (
            r.get("requests", 0), agent.fmt_tps(r.get("tps")), agent.fmt_tps(r.get("peak")),
            agent.fmt_tps(tr.get("tps")), agent.fmt_tok(tr.get("ratedOutput", 0))))
    else:
        out.append("  近 30 分钟无活跃会话")
    if delta:
        extra = ""
        if delta.get("running"):
            extra = " · %s⚡生成中 %ds%s" % (YELLOW, delta.get("runningElapsedSec", 0), RESET)
        out.append("  窗口（上次轮询后）输出 %s · %s tok/s · %d 次完成%s" % (
            agent.fmt_tok(delta.get("output")), agent.fmt_tps(delta.get("tps")),
            delta.get("requests", 0), extra))
    sess = [s for s in (snap.get("sessions") or []) if not s.get("isSubagent")][:3]
    for s in sess:
        state = "%s⚡生成中%s" % (YELLOW, RESET) if s.get("running") else "idle %ds" % s.get("idleSec", 0)
        out.append("    · %s [%s] %s · %s tok/s · %s" % (
            (s.get("title") or s.get("sid", "")[:14])[:20], s.get("directory") or "—",
            agent.fmt_tok(s.get("totalTokens")), agent.fmt_tps(s.get("lastTps")), state))
    return out


def frame_remote(key, res):
    label = res.get("label") or key
    if not res.get("ok"):
        return ["%s ☁ 远程%s %s" % (RED, RESET, label),
                "  %s查询失败：%s%s" % (RED, (res.get("error") or "unknown")[:70], RESET)]
    d = res.get("data") or {}
    lat = res.get("latency_ms")
    out = ["%s ☁ 远程%s %s（%s · SSH %dms）" % (CYAN, RESET, label, d.get("host") or "", lat or -1)]
    t = d.get("today") or {}
    out.append("  今日 %s tok · 输出 %s · %d 次请求" % (
        agent.fmt_tok(t.get("total")), agent.fmt_tok(t.get("output")), t.get("requests", 0)))
    cur = d.get("current")
    if cur:
        r = cur.get("rate") or {}
        tr = cur.get("turnRate") or {}
        out.append("  当前会话「%s」累计 %s · 加权 %s tok/s · 上轮 %s tok/s" % (
            (cur.get("title") or "")[:24], agent.fmt_tok(cur.get("total")),
            agent.fmt_tps(r.get("tps")), agent.fmt_tps(tr.get("tps"))))
    delta = d.get("delta")
    if delta:
        extra = ""
        if delta.get("running"):
            extra = " · %s⚡生成中 %ds%s" % (YELLOW, delta.get("runningElapsedSec", 0), RESET)
        out.append("  窗口（上次轮询后）输出 %s · %s tok/s · %d 次完成%s" % (
            agent.fmt_tok(delta.get("output")), agent.fmt_tps(delta.get("tps")),
            delta.get("requests", 0), extra))
    for s in [x for x in (d.get("sessions") or []) if not x.get("isSubagent")][:4]:
        state = "%s⚡生成中%s" % (YELLOW, RESET) if s.get("running") else "idle %ds" % s.get("idleSec", 0)
        out.append("    · %s [%s] %s · %s tok/s · %s" % (
            (s.get("title") or s.get("sid", "")[:14])[:20], s.get("directory") or "—",
            agent.fmt_tok(s.get("totalTokens")), agent.fmt_tps(s.get("lastTps")), state))
    return out


def build_frame(local_snap, local_delta, remote_results, width=64):
    now_ms = int(time.time() * 1000)
    lines = ["%s%s ZCode Token Monitor %s%s %s" % (BOLD, CYAN, RESET, DIM, _t(now_ms))]
    lines.append(LINE[:width])
    if local_snap is not None:
        lines += frame_local(local_snap, local_delta)
    if remote_results:
        lines.append(DIM + LINE[:width] + RESET)
        for key in sorted(remote_results):
            lines += frame_remote(key, remote_results[key])
    lines.append(DIM + LINE[:width] + RESET)
    lines.append(DIM + "Ctrl+C 退出 · 速率 = output tokens ÷ 生成时长（思考段从 started_at 起算）" + RESET)
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    once = "--once" in argv
    no_remote = "--no-remote" in argv
    no_local = "--no-local" in argv
    explicit_interval = None
    duration = None
    for i, a in enumerate(argv):
        if a == "--interval" and i + 1 < len(argv):
            explicit_interval = max(0.5, float(argv[i + 1]))
        if a == "--duration" and i + 1 < len(argv):
            duration = max(1, int(argv[i + 1]))
    cfg = cfg_mod.load()
    interval = explicit_interval if explicit_interval is not None else float(
        cfg.get("remote", {}).get("poll_s", 2))
    targets = []
    if not no_remote:
        import remote
        targets = remote.load_targets(cfg)

    signal.signal(signal.SIGINT, signal.default_int_handler)
    t_start = time.monotonic()
    last_poll_ms = int(time.time() * 1000) - int(interval * 1000)
    try:
        while True:
            local_snap = local_delta = None
            if not no_local:
                try:
                    con = agent.connect()
                    try:
                        local_snap = agent.snapshot(con, since_ms=last_poll_ms)
                    finally:
                        con.close()
                    local_delta = local_snap.pop("delta", None)
                except Exception as e:  # noqa: BLE001
                    local_snap = None
                    sys.stderr.write("local error: %s\n" % e)
            remote_results = {}
            if targets:
                import remote
                remote_results = remote.query_all(targets, cfg, since_ms=last_poll_ms)
            last_poll_ms = int(time.time() * 1000)
            text = build_frame(local_snap, local_delta, remote_results)
            if once:
                print(text)
                return 0
            sys.stdout.write(CLEAR + text + "\n")
            sys.stdout.flush()
            if duration is not None and time.monotonic() - t_start >= duration:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        if not once:
            sys.stdout.write(CLEAR)
            sys.stdout.flush()
        return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
