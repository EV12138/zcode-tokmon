#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZCode token monitor MCP server（stdio JSON-RPC，零依赖）。

工具 token_monitor(scope)：
  now | today | week | days:N | sessions | remote | all
  —— remote/all 触发 SSH 远程查询（自动部署远端 agent、连接复用），
     修复原插件「MCP/命令不跨机」的问题。

协议仅实现 initialize / tools/list / tools/call；调试信息只走 stderr。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import agent  # noqa: E402
import cli  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"


def tool_token_monitor(scope="now"):
    if scope in ("now", "current", ""):
        con = agent.connect()
        try:
            return agent.render_now(agent.snapshot(con))
        finally:
            con.close()
    if scope == "today":
        con = agent.connect()
        try:
            snap = agent.snapshot(con)
        finally:
            con.close()
        t = snap["today"]
        return "【今日】总 %s（输入 %s · 输出 %s · 缓存读 %s · 缓存写 %s · %d 次请求）" % (
            agent.fmt_tok(t["total"]), agent.fmt_tok(t["input"]), agent.fmt_tok(t["output"]),
            agent.fmt_tok(t["cache_read"]), agent.fmt_tok(t["cache_write"]), t["requests"])
    if scope == "week":
        return agent.render_days(7)
    if scope.startswith("days:"):
        return agent.render_days(max(1, min(90, int(scope.split(":", 1)[1]))))
    if scope == "sessions":
        con = agent.connect()
        try:
            snap = agent.snapshot(con)
        finally:
            con.close()
        out = []
        for s in snap.get("sessions") or []:
            state = "⚡生成中" if s.get("running") else "idle %ds" % s.get("idleSec", 0)
            sub = " · 子代理" if s.get("isSubagent") else ""
            out.append("%s%s [%s] 总%s · 最近 %s tok/s · %s" % (
                s.get("title") or s.get("sid", "")[:16], sub, s.get("directory") or "—",
                agent.fmt_tok(s.get("totalTokens")), agent.fmt_tps(s.get("lastTps")), state))
        return "\n".join(out) or "近 30 分钟无活跃会话"
    if scope in ("remote", "remotes"):
        return cli.render_remote()
    if scope == "all":
        return cli.render_all()
    return ("未知 scope: %r。可用: now | today | week | days:N | sessions | remote | all" % scope)


TOOLS = [
    {
        "name": "token_monitor",
        "description": (
            "查询 ZCode 的 token 用量与生成速率（只读 db.sqlite；remote/all 会通过 SSH 查询远程主机，"
            "首次查询自动部署远端 agent，约需数秒）。"
            "scope 可选: now(当前会话+今日+速率,默认), today, week, days:N(近N天), "
            "sessions(活跃会话), remote(所有 SSH 远程主机的用量/速率), all(本地+远程汇总)。"
            "速率口径: output_tokens ÷ 生成时长（思考段从 started_at 起算），加权聚合。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "查询范围，默认 now"},
            },
        },
    }
]


def reply(req_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False), flush=True)


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        method = msg.get("method", "")
        req_id = msg.get("id")
        if method == "initialize":
            reply(req_id, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "zcode-token-monitor", "version": "0.1.0"},
            })
        elif method == "tools/list":
            reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            args = params.get("arguments") or {}
            try:
                text = tool_token_monitor(**args)
                reply(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:  # noqa: BLE001
                hint = ""
                if isinstance(e, sqlite3.OperationalError):
                    hint = "\n多半是 ZCode 升级改了用量库结构（表/列变更）。请更新本插件后再查询。"
                elif isinstance(e, FileNotFoundError):
                    hint = "\n本机还没有 ZCode 用量数据库（~/.zcode/cli/db/db.sqlite）。"
                reply(req_id, {
                    "content": [{"type": "text", "text": "查询失败: %s%s" % (e, hint)}],
                    "isError": True,
                })
        elif req_id is not None:
            print(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": "unknown method %s" % method},
            }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
