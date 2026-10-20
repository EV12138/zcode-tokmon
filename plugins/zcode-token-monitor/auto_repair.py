#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tokmon 自修复：ZCode 更新覆盖 app.asar 后自动重新注入悬浮条。

由 ~/Library/LaunchAgents/ai.tokmon.repair.plist 触发：
  - WatchPaths 监听 app.asar 变化
  - StartInterval 300s 兜底
  - RunAtLoad 登录时检查
注入完成后，如果 ZCode 是刚启动的（更新后自动拉起），且没有正在生成的请求，
自动重启一次让新注入生效；否则发通知等下次启动。
"""
import fcntl
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
ASAR = Path(os.environ.get("TOKMON_ASAR") or
            "/Applications/ZCode.app/Contents/Resources/app.asar")
RUNTIME = HOME / ".zcode" / "token-monitor"
CONFIG = RUNTIME / "config.json"
LOG = RUNTIME / "auto-repair.log"
STATE = RUNTIME / "auto-repair-state.json"
LOCK = RUNTIME / "auto-repair.lock"
DB = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
CACHE_ROOT = HOME / ".zcode" / "cli" / "plugins" / "cache" / "token-monitor-local" / "zcode-token-monitor"
INSTALLED = HOME / ".zcode" / "cli" / "plugins" / "installed_plugins.json"
LABEL = "ai.tokmon.repair"
PLIST = HOME / "Library" / "LaunchAgents" / (LABEL + ".plist")
LAUNCH_LOG = RUNTIME / "auto-repair.launchd.log"
TEST = bool(os.environ.get("TOKMON_ASAR"))


def log(msg):
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 1024 * 1024:
            shutil.move(str(LOG), str(LOG) + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def notify(msg):
    if TEST:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title "Tokmon"' % json.dumps(msg, ensure_ascii=False)],
            timeout=10, capture_output=True)
    except Exception:
        pass


def acquire_lock():
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        f = open(LOCK, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        return None


def enabled():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return cfg.get("auto_repair", True) is not False
    except (OSError, ValueError):
        return True


def version_key(text):
    out = []
    for part in str(text).replace("-", ".").split("."):
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out)


def patch_script():
    """优先用插件缓存里最新版本的 patch_asar.py（会同步运行时文件）。"""
    candidates = []
    try:
        data = json.loads(INSTALLED.read_text(encoding="utf-8"))
        for item in data.get("plugins") or []:
            if item.get("id") == "zcode-token-monitor@token-monitor-local":
                path = item.get("installPath")
                if path:
                    candidates.append(Path(path) / "patch_asar.py")
    except (OSError, ValueError, AttributeError):
        pass
    try:
        for d in sorted(CACHE_ROOT.iterdir(), key=lambda p: version_key(p.name), reverse=True):
            candidates.append(d / "patch_asar.py")
    except OSError:
        pass
    candidates.append(RUNTIME / "patch_asar.py")
    for path in candidates:
        if path.is_file():
            return path
    return None


def check(patch):
    try:
        env = os.environ.copy()
        env["ZCODE_ASAR"] = str(ASAR)
        r = subprocess.run([sys.executable, str(patch), "check"],
                           capture_output=True, text=True, timeout=60, env=env)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def wait_stable(path, rounds=12):
    last = None
    for _ in range(rounds):
        try:
            st = path.stat()
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            return False
        if sig == last:
            return True
        last = sig
        time.sleep(0.5)
    return False


def app_pid():
    try:
        r = subprocess.run(["pgrep", "-x", "ZCode"], capture_output=True, text=True, timeout=5)
        pids = [int(x) for x in r.stdout.split() if x.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def app_uptime(pid):
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "etimes="],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip())
    except Exception:
        return None


def generation_running():
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=2)
        try:
            return con.execute(
                "select count(*) from model_usage where status='running'").fetchone()[0]
        finally:
            con.close()
    except Exception:
        return 0


def restart_app():
    try:
        subprocess.run(["osascript", "-e", 'tell application id "dev.zcode.app" to quit'],
                       timeout=10, capture_output=True)
    except Exception:
        return False
    for _ in range(20):
        if app_pid() is None:
            break
        time.sleep(0.5)
    if app_pid() is not None:
        return False
    try:
        subprocess.run(["open", "-a", "/Applications/ZCode.app"], timeout=15, capture_output=True)
        return True
    except Exception:
        return False


def state_repairs():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return [float(x) for x in data.get("repairs", []) if isinstance(x, (int, float))]
    except (OSError, ValueError, AttributeError):
        return []


def save_repairs(repairs):
    try:
        STATE.write_text(json.dumps({"repairs": repairs[-20:]}), encoding="utf-8")
    except OSError:
        pass


def build_plist(python_exe, script, asar, launch_log):
    """生成 LaunchAgent 配置（纯函数，便于测试）。"""
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python_exe), str(script)],
        "RunAtLoad": True,
        "WatchPaths": [str(asar)],
        "StartInterval": 60,
        "ThrottleInterval": 30,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(launch_log),
        "StandardErrorPath": str(launch_log),
    }


def _launchctl(*args):
    try:
        return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, timeout=15)
    except Exception:
        return None


def install_agent():
    if sys.platform != "darwin":
        print("自动修复代理仅支持 macOS；当前平台请更新后手动运行 patch_asar.py install")
        return 0
    if not ASAR.is_file():
        print("✗ 找不到 app.asar，无法安装自动修复代理：%s" % ASAR)
        return 1
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        data = build_plist(sys.executable, Path(__file__).resolve(), ASAR, LAUNCH_LOG)
        PLIST.write_bytes(plistlib.dumps(data))
    except OSError as e:
        print("✗ 写入 LaunchAgent 失败：%s" % e)
        return 1
    uid = os.getuid()
    _launchctl("bootout", "gui/%d/%s" % (uid, LABEL))
    r = _launchctl("bootstrap", "gui/%d" % uid, str(PLIST))
    if r is None or r.returncode != 0:
        # 旧系统/非 GUI 会话兜底
        _launchctl("load", "-w", str(PLIST))
    _launchctl("enable", "gui/%d/%s" % (uid, LABEL))
    print("✓ 自动修复代理已安装：%s" % PLIST)
    print("  监听 %s；ZCode 更新覆盖后会重新注入，必要时自动重启一次" % ASAR)
    return 0


def uninstall_agent():
    if sys.platform != "darwin":
        return 0
    uid = os.getuid()
    _launchctl("bootout", "gui/%d/%s" % (uid, LABEL))
    _launchctl("unload", "-w", str(PLIST))
    try:
        PLIST.unlink()
        print("✓ 自动修复代理已卸载：%s" % PLIST)
    except FileNotFoundError:
        print("✓ 自动修复代理未安装")
    except OSError as e:
        print("✗ 删除 LaunchAgent 失败：%s" % e)
        return 1
    return 0


def agent_status():
    print("自动修复代理：%s" % ("已安装" if PLIST.is_file() else "未安装"))
    print("  plist: %s" % PLIST)
    print("  script: %s" % (RUNTIME / "auto_repair.py"))
    print("  asar: %s" % ASAR)
    if PLIST.is_file() and sys.platform == "darwin":
        r = _launchctl("print", "gui/%d/%s" % (os.getuid(), LABEL))
        if r and r.stdout:
            for line in r.stdout.splitlines():
                if any(k in line for k in ("state =", "last exit code", "run interval", "watching =")):
                    print("  " + line.strip())
    return 0


def run_repair():
    if not enabled():
        return 0
    if not ASAR.is_file():
        log("asar missing: %s" % ASAR)
        return 0
    lock = acquire_lock()
    if lock is None:
        log("another repair is running; skip")
        return 0

    patch = patch_script()
    if not patch:
        log("patch_asar.py not found")
        notify("Tokmon 自动修复失败：找不到 patch_asar.py")
        return 0

    ok, out = check(patch)
    if ok:
        log("already injected; nothing to do")
        return 0  # 已注入且运行时一致，什么都不用做

    now = time.time()
    repairs = [t for t in state_repairs() if now - t < 600]
    if len(repairs) >= 3:
        log("circuit breaker: %d repairs in 10min" % len(repairs))
        notify("Tokmon 自动修复多次仍未稳定，请检查 ZCode 更新或手动运行 install")
        return 0

    if not wait_stable(ASAR):
        log("asar is still changing; retry later")
        return 0

    log("injecting into %s via %s" % (ASAR, patch))
    env = os.environ.copy()
    env["ZCODE_ASAR"] = str(ASAR)
    try:
        r = subprocess.run([sys.executable, str(patch), "install"],
                           capture_output=True, text=True, timeout=240, env=env)
        log("install exit=%s %s" % (r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()[-600:]))
    except Exception as e:  # noqa: BLE001
        log("install failed: %s" % e)
        notify("Tokmon 自动修复失败，请手动重装悬浮条")
        return 0

    ok, out = check(patch)
    if not ok:
        log("post-install check failed: %s" % out.strip()[-400:])
        notify("Tokmon 自动修复失败，请手动重装悬浮条")
        return 0

    save_repairs(repairs + [time.time()])

    if TEST:
        log("test mode: injection ok, skip app handling")
        return 0

    pid = app_pid()
    if not pid:
        log("repaired; ZCode not running")
        return 0

    uptime = app_uptime(pid)
    running = generation_running()
    log("repaired; app pid=%s uptime=%s running=%s" % (pid, uptime, running))
    # 刚更新后拉起的 ZCode（5 分钟内）且没有正在生成：自动重启一次让注入生效
    if uptime is not None and uptime <= 300 and running == 0:
        if restart_app():
            log("auto-restarted ZCode")
            notify("Tokmon 已自动修复并重启 ZCode")
        else:
            log("auto-restart failed")
            notify("Tokmon 已修复；ZCode 未能自动重启，请手动重启")
    else:
        notify("Tokmon 已自动修复；重启 ZCode 后生效")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "run"
    if cmd == "install":
        return install_agent()
    if cmd == "uninstall":
        return uninstall_agent()
    if cmd == "status":
        return agent_status()
    return run_repair()


if __name__ == "__main__":
    sys.exit(main())
