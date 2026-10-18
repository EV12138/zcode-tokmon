#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token monitor CLI —— 查询本地/远程用量与速率，也是 /usage 命令的兜底通道。

子命令：
  now | today | week | days:N | sessions | remote | all | watch
  remote/all 会触发 SSH 查询（自动部署远端 agent、ControlMaster 复用）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent  # noqa: E402
import config as cfg_mod  # noqa: E402


def _fmt_remote_target(key, res):
    lines = []
    label = res.get("label") or key
    if not res.get("ok"):
        lines.append("☁ %s — 查询失败：%s" % (label, res.get("error") or "unknown"))
        return "\n".join(lines)
    d = res.get("data") or {}
    lat = res.get("latency_ms")
    host = d.get("host") or res.get("host") or ""
    lines.append("☁ %s（%s · SSH %dms）" % (label, host, lat or -1))
    t = d.get("today") or {}
    if t:
        lines.append("  【远端今日】总 %s（输入 %s · 输出 %s · 缓存读 %s · %d 次请求）" % (
            agent.fmt_tok(t.get("total")), agent.fmt_tok(t.get("input")),
            agent.fmt_tok(t.get("output")), agent.fmt_tok(t.get("cache_read")),
            t.get("requests", 0)))
    cur = d.get("current")
    if cur:
        r = cur.get("rate") or {}
        tr = cur.get("turnRate") or {}
        lines.append("  【远端当前会话】%s" % (cur.get("title") or cur.get("sid", "")[:20]))
        lines.append("    累计 %s · 上下文 %s · 最近%d次加权 %s tok/s · 上轮 %s tok/s" % (
            agent.fmt_tok(cur.get("total")), agent.fmt_tok(cur.get("context")),
            r.get("requests", 0), agent.fmt_tps(r.get("tps")),
            agent.fmt_tps(tr.get("tps"))))
    delta = d.get("delta")
    if delta:
        lines.append("  【窗口速率】最近 %d 秒完成 %d 次请求 · 输出 %s · 加权 %s tok/s%s" % (
            max(1, int(delta.get("windowMs") or 0) // 1000), delta.get("requests", 0),
            agent.fmt_tok(delta.get("output")), agent.fmt_tps(delta.get("tps")),
            " · ⚡生成中(%ds)" % delta.get("runningElapsedSec", 0) if delta.get("running") else ""))
    sess = d.get("sessions") or []
    if sess:
        lines.append("  【远端活跃会话】")
        for s in sess[:6]:
            state = "⚡生成中" if s.get("running") else "idle %ds" % s.get("idleSec", 0)
            sub = " · 子代理" if s.get("isSubagent") else ""
            sub = " · 子代理" if s.get("isSubagent") else ""
            lines.append("    - %s%s [%s] 总%s · 最近 %s tok/s · %s" % (
                s.get("title") or s.get("sid", "")[:16], sub,
                s.get("directory") or "—", agent.fmt_tok(s.get("totalTokens")),
                agent.fmt_tps(s.get("lastTps")), state))
    return "\n".join(lines)


def render_remote(cfg=None, since_ms=None):
    import remote
    cfg = cfg or cfg_mod.load()
    targets = remote.load_targets(cfg)
    if not targets:
        return ("未发现远程目标。可让 ZCode 连接一次 SSH 远程工作区（自动发现），"
                "或在 ~/.zcode/token-monitor/config.json 的 remote.targets 手动添加。")
    results = remote.query_all(targets, cfg, since_ms)
    parts = ["【SSH 远程】共 %d 个目标" % len(targets)]
    for key in sorted(results):
        parts.append(_fmt_remote_target(key, results[key]))
    return "\n".join(parts)


def render_all(cfg=None):
    cfg = cfg or cfg_mod.load()
    con = agent.connect()
    try:
        local = agent.render_now(agent.snapshot(con))
    finally:
        con.close()
    return "【本地】\n" + local + "\n\n" + render_remote(cfg)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "now"
    rest = argv[1:]
    if cmd in ("now", "current"):
        con = agent.connect()
        try:
            print(agent.render_now(agent.snapshot(con)))
        finally:
            con.close()
    elif cmd == "today":
        con = agent.connect()
        try:
            snap = agent.snapshot(con)
        finally:
            con.close()
        t = snap["today"]
        print("【今日】总 %s（输入 %s · 输出 %s · 缓存读 %s · 缓存写 %s · %d 次请求）" % (
            agent.fmt_tok(t["total"]), agent.fmt_tok(t["input"]), agent.fmt_tok(t["output"]),
            agent.fmt_tok(t["cache_read"]), agent.fmt_tok(t["cache_write"]), t["requests"]))
    elif cmd == "week":
        print(agent.render_days(7))
    elif cmd.startswith("days:"):
        print(agent.render_days(max(1, min(90, int(cmd.split(":", 1)[1])))))
    elif cmd == "sessions":
        con = agent.connect()
        try:
            snap = agent.snapshot(con)
            for s in snap.get("sessions") or []:
                state = "⚡生成中" if s.get("running") else "idle %ds" % s.get("idleSec", 0)
                sub = " · 子代理" if s.get("isSubagent") else ""
                print("%s%s [%s] 总%s · 最近 %s tok/s · %s" % (
                    s.get("title") or s.get("sid", "")[:16], sub, s.get("directory") or "—",
                    agent.fmt_tok(s.get("totalTokens")), agent.fmt_tps(s.get("lastTps")), state))
        finally:
            con.close()
    elif cmd == "remote":
        print(render_remote())
    elif cmd == "all":
        print(render_all())
    elif cmd == "watch":
        import monitor
        monitor.main(rest)
    elif cmd in ("-h", "--help", "help"):
        print(__doc__)
    else:
        print("未知子命令: %r\n%s" % (cmd, __doc__))
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
