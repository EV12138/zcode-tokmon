---
description: 安装/卸载/检查输入框下方的 token 实时悬浮条（改 app.asar，需重启 ZCode 生效）
argument-hint: "[install|remove|check]"
---

管理 token 悬浮状态条（聊天输入框下方的实时速率监控栏）。

用 Bash 工具执行（把 <插件根目录> 换成实际安装路径）：

- 安装/更新：`python3 "<插件根目录>/patch_asar.py" install`
- 卸载：`python3 "<插件根目录>/patch_asar.py" remove`
- 检查：`python3 "<插件根目录>/patch_asar.py" check`

$ARGUMENTS 为空时默认 check。规则：
1. **install 会修改 /Applications/ZCode.app/Contents/Resources/app.asar**（入口尾部追加一行 dynamic import，自研 [tokmon] 标记，remove 可逐字节还原官方入口）。执行前先告知用户这一点再执行。
2. 安装/卸载后 ZCode 需要完全退出并重开才生效；工具输出里有提示，如实转告。
3. ZCode 官方升级会覆盖 app.asar，悬浮条会消失——重跑 install 即可（约十几秒）。
4. 运行时文件在 ~/.zcode/token-monitor/（agent.py/overlay.js/inject-main.cjs），install 自动同步。
5. 排障：悬浮条不出现时先跑 check；日志在 ~/.zcode/token-monitor/inject.log（执行 `tail -30 ~/.zcode/token-monitor/inject.log` 看最近记录）。

把命令输出原样转告用户，不要省略自检结果（[check] 行）。
