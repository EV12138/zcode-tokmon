---
description: 启动 token 实时监控面板（本地 + SSH 远程速率刷新）
argument-hint: "[--duration N] [--interval S] [--once]"
---

启动 ZCode token 实时监控面板，实时刷新本地与 SSH 远程主机的用量和生成速率。

执行方式（用 Bash 工具）：

```
python3 "<插件根目录>/monitor.py" --duration 10 --interval 2
```

- `--duration N`：跑 N 秒后自动退出并输出最后一帧（会话内推荐 10~15 秒，观察速率变化够用了）
- `--interval S`：刷新间隔秒数（默认读配置 2s）
- `--once`：只出一帧快照，不循环
- 不带参数运行是常驻面板，Ctrl+C 退出（适合用户自己在终端里跑）

把面板输出原样贴给用户，并用一句话解读：本地/各远程主机的今日用量、当前会话速率（tok/s）、是否「⚡生成中」。首次查远程会自动部署远端 agent（数秒）。

如果用户想在自己终端常驻监控，告诉用户命令：
`python3 ~/.zcode/cli/plugins/cache/<市场名>/zcode-token-monitor/<版本>/monitor.py`
（或直接给出插件安装目录下的 monitor.py 绝对路径。）
