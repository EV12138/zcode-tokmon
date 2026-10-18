---
description: 查询 ZCode token 用量与生成速率（本地 + SSH 远程主机）
argument-hint: "[now|today|week|days:N|sessions|remote|all]"
---

查询 ZCode token 用量与速率，把工具返回的报表原样整理后回复用户；用户要详细表格时用完整数值（带千分位），不要自行缩写或编造数字。

取数顺序：
1. 优先调用 MCP 工具 `token_monitor`（参数 scope）。
2. 工具不可用时，用 Bash 跑 CLI 兜底：`python3 "<插件根目录>/cli.py" <子命令>`。

$ARGUMENTS 到 scope 的映射（为空默认 now）：
- 空 / "now" / "当前" → `now`（当前会话 + 今日 + 速率）
- "today" / "今日" → `today`
- "week" / "近N天" → `week` 或 `days:N`
- "sessions" / "活跃会话" → `sessions`
- "remote" / "远程" / "服务器" → `remote`（SSH 查询所有远程主机，首次会自动部署远端 agent，约需数秒，提前告知用户）
- "all" / "全部" → `all`（本地 + 远程汇总）

速率口径说明（用户问及时解释）：tok/s = output tokens ÷ 生成时长（有思考段的请求从 started_at 起算），聚合为最近 5 次请求的加权平均；「窗口」速率指最近一次轮询之后完成请求的聚合速率。
