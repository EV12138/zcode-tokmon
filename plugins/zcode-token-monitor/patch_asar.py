#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tokmon app.asar 注入/卸载工具（输入框下方 token 悬浮条）。

用法：
  python3 patch_asar.py install   # 同步运行时文件到 ~/.zcode/token-monitor/ 并注入（幂等）
  python3 patch_asar.py remove    # 剥离注入行（不依赖备份，完整还原客户端入口）
  python3 patch_asar.py check     # 只读检查注入状态与运行时文件一致性

原理：asar 主入口 out/main/index.js 尾部追加一行 dynamic import 指向运行时目录的
inject-main.cjs（入口是 ESM，必须 dynamic import；Electron fuse
EmbeddedAsarIntegrityValidation=0，asar 可改）。安装/卸载只增删本工具自己的注入行
（[tokmon] 标记识别），不做 asar 备份；remove 后入口与官方版本逐字节一致。

ZCode 升级会覆盖 app.asar，届时重跑 install 即可（悬浮条消失 = 提示该重装了）。
"""
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

IS_MAC = sys.platform == "darwin"
ASAR = Path(os.environ.get("ZCODE_ASAR") or
            ("/Applications/ZCode.app/Contents/Resources/app.asar" if IS_MAC else "app.asar"))
RUNTIME = Path(os.environ.get("ZCODE_RUNTIME") or Path.home() / ".zcode" / "token-monitor")
HERE = Path(__file__).parent.resolve()
ENTRY = "out/main/index.js"
ALIGN = 4
BLOCK = 4 * 1024 * 1024
RUNTIME_FILES = ("agent.py", "overlay.js", "inject-main.cjs", "config.py", "remote.py")

INJECT_LINE_TMPL = (
    '\n;import("{url}").then(()=>null,(e)=>console.error("[tokmon] load failed",e));'
)
INJECT_LINE_RE = re.compile(
    rb'\n;import\("[^"]*inject-main\.cjs"\)\.then\(\(\)=>null,'
    rb'\(e\)=>console\.error\("\[tokmon\] load failed",e\)\);'
)


def inject_line():
    return INJECT_LINE_TMPL.format(url=(RUNTIME / "inject-main.cjs").as_uri())


def zcode_exe():
    if not IS_MAC:
        return None
    exe = ASAR.parent.parent / "MacOS" / "ZCode"
    return exe if exe.is_file() else None


def client_running():
    try:
        return subprocess.run(["pgrep", "-x", "ZCode"], capture_output=True).returncode == 0
    except Exception:
        return True


# ---------- asar 读写（格式与重打包算法沿用成熟实现） ----------

def read_header(f):
    f.seek(0)
    a, b, c, d = struct.unpack("<4I", f.read(16))
    assert a == 4, "unexpected pickle prefix %d" % a
    header = json.loads(f.read(d))
    return header, 8 + b


def iter_files(node, path=""):
    for name, ch in node.get("files", {}).items():
        p = "%s/%s" % (path, name)
        if "files" in ch:
            for x in iter_files(ch, p):
                yield x
        else:
            yield p, ch


def compute_integrity(data):
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(data).hexdigest(),
        "blockSize": BLOCK,
        "blocks": [hashlib.sha256(data[i:i + BLOCK]).hexdigest()
                   for i in range(0, len(data), BLOCK)],
    }


def repack(src_path, modify, dst_path):
    """重建 asar。modify: {asar 内路径: 新内容 bytes}。"""
    with open(src_path, "rb") as src:
        header, base = read_header(src)
        header = json.loads(json.dumps(header))
        nodes = dict(iter_files(header))
        missing = set(modify) - set(nodes)
        assert not missing, "paths not in asar: %s" % missing

        data_parts = []
        offset = 0
        for p, node in nodes.items():
            if node.get("unpacked"):
                node["offset"] = "0"
                continue
            if p in modify:
                data = modify[p]
                node["size"] = len(data)
                node["integrity"] = compute_integrity(data)
            else:
                src.seek(base + int(node["offset"]))
                data = src.read(node["size"])
            node["offset"] = str(offset)
            data_parts.append(data)
            offset += len(data)

        payload = b"".join(data_parts)
        header_bytes = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        d = len(header_bytes)
        c = (d + 4 + ALIGN - 1) // ALIGN * ALIGN
        b_total = c + 4
        with open(dst_path, "wb") as out:
            out.write(struct.pack("<4I", 4, b_total, c, d))
            out.write(header_bytes)
            out.write(b"\0" * (c - d - 4))
            assert out.tell() == 8 + b_total, "header pad mismatch"
            out.write(payload)
    return dst_path


def entry_bytes(asar_path):
    with open(asar_path, "rb") as f:
        header, base = read_header(f)
        nodes = dict(iter_files(header))
        node = nodes["/" + ENTRY]
        f.seek(base + int(node["offset"]))
        return f.read(node["size"])


def syntax_check():
    exe = zcode_exe()
    if not exe:
        print("  [check] 未找到 ZCode 可执行文件，跳过语法检查")
        return True
    src = entry_bytes(ASAR)
    fd, name = tempfile.mkstemp(prefix=".tokmon-check.", suffix=".js")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(src)
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        r = subprocess.run([str(exe), "--check", name], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=60)
        if r.returncode == 0:
            print("  [check] 语法检查 exit=0 ✓")
            return True
        print("  [check] 语法检查失败:\n%s" % (r.stderr or "")[:400])
        return False
    except OSError as e:
        print("  [check] 语法检查无法执行（%s），跳过" % e)
        return True
    finally:
        Path(name).unlink(missing_ok=True)


# ---------- 运行时同步 ----------

def sync_runtime():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in RUNTIME_FILES:
        src = HERE / name
        dst = RUNTIME / name
        if not src.is_file():
            raise FileNotFoundError("runtime source missing: %s" % src)
        s = src.read_bytes()
        if not dst.is_file() or dst.read_bytes() != s:
            dst.write_bytes(s)
            changed.append(name)
    # Electron 主进程（GUI 环境）PATH 很瘦，常找不到框架版 python3：
    # 把当前解释器绝对路径固化进 config，inject-main 优先用它拉起数据泵。
    cfg_path = RUNTIME / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
        if not isinstance(cfg, dict):
            cfg = {}
        if not cfg.get("python_path"):
            cfg["python_path"] = sys.executable or "python3"
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            print("  [runtime] config.json 固化 python_path=%s" % cfg["python_path"])
    except (OSError, ValueError) as e:
        print("  [runtime] config.json 处理失败（不阻断）: %s" % e)
    if changed:
        print("  [runtime] 同步 %d 个文件 → %s（%s）" % (len(changed), RUNTIME, ", ".join(changed)))
    else:
        print("  [runtime] 已是最新（%s）" % RUNTIME)
    return changed


def runtime_stale():
    stale = []
    for name in RUNTIME_FILES:
        src, dst = HERE / name, RUNTIME / name
        if not dst.is_file() or src.read_bytes() != dst.read_bytes():
            stale.append(name)
    return stale


# ---------- 安装/卸载 ----------

def install():
    print("[install] 目标 %s" % ASAR)
    if not ASAR.is_file():
        print("✗ 找不到 %s（自定义位置请设 ZCODE_ASAR）" % ASAR)
        return 1
    sync_runtime()
    entry = entry_bytes(ASAR)
    line = inject_line().encode()
    if line in entry:
        print("✓ 已注入（幂等，无需重复）；运行时已在上方同步")
        _self_check(expect=True)
        _restart_hint()
        return 0
    entry = INJECT_LINE_RE.sub(b"", entry)      # 幂等：剥掉任何历史 tokmon 行
    new_entry = entry + inject_line().encode()
    tmp = ASAR.with_name(ASAR.name + ".tokmon.tmp")
    print("  重打包（追加注入行，约需十几秒）...")
    repack(ASAR, {"/" + ENTRY: new_entry}, tmp)
    os.replace(tmp, ASAR)
    if not _self_check(expect=True) or not syntax_check():
        print("✗ 自检失败，回滚")
        entry2 = INJECT_LINE_RE.sub(b"", entry_bytes(ASAR))
        repack(ASAR, {"/" + ENTRY: entry2}, tmp)
        os.replace(tmp, ASAR)
        return 1
    print("✓ 注入完成")
    _restart_hint()
    return 0


def remove():
    print("[remove] 目标 %s" % ASAR)
    if not ASAR.is_file():
        print("✗ 找不到 %s" % ASAR)
        return 1
    entry = entry_bytes(ASAR)
    if not INJECT_LINE_RE.search(entry):
        print("✓ 未注入（无需卸载）")
        return 0
    new_entry = INJECT_LINE_RE.sub(b"", entry)
    tmp = ASAR.with_name(ASAR.name + ".tokmon.tmp")
    print("  重打包（剥离注入行，约需十几秒）...")
    repack(ASAR, {"/" + ENTRY: new_entry}, tmp)
    os.replace(tmp, ASAR)
    ok = _self_check(expect=False)
    print("✓ 卸载完成" if ok else "✗ 自检异常，请检查")
    _restart_hint()
    return 0 if ok else 1


def check():
    print("[check] %s（%d bytes）" % (ASAR, ASAR.stat().st_size if ASAR.exists() else -1))
    if not ASAR.is_file():
        print("✗ 找不到 asar")
        return 1
    entry = entry_bytes(ASAR)
    hit = INJECT_LINE_RE.search(entry)
    print("注入状态: %s" % ("已注入 → %s" % (RUNTIME / "inject-main.cjs") if hit else "未注入"))
    stale = runtime_stale()
    print("运行时目录 %s: %s" % (RUNTIME, "文件缺失/过期 → %s" % stale if stale else "完整一致"))
    if hit and not stale:
        print("结论: ✓ 悬浮条应正常（客户端需已重启）")
        return 0
    if hit and stale:
        print("结论: ⚠ 已注入但运行时旧；重跑 install 更新")
        return 1
    print("结论: 未安装悬浮条（跑 install 安装）")
    return 1


def _self_check(expect):
    with open(ASAR, "rb") as f:
        header, _ = read_header(f)
        n = sum(1 for _ in iter_files(header))
    entry = entry_bytes(ASAR)
    has = bool(INJECT_LINE_RE.search(entry))
    ok = has == expect
    print("  [check] header ok, 文件数=%d, 注入行%s" % (n, "符合预期" if ok else "不符合预期！！"))
    return ok


def _restart_hint():
    if client_running():
        print("ℹ ZCode 正在运行：完全退出并重开 ZCode 后生效")


def main(argv=None):
    cmd = (argv or sys.argv[1:])[0] if (argv or sys.argv[1:]) else ""
    if cmd == "install":
        return install()
    if cmd == "remove":
        return remove()
    if cmd == "check":
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
