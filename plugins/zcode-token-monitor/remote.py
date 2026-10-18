#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSH 远程层：目标发现 + ControlMaster 连接复用 + 远端 agent 自动部署 + 并行查询。

修复原插件 (ByteRax/zcode-token-usage-statusbar) 的远程问题：
  - MCP/命令不跨机        → 本模块被 MCP/CLI/monitor 共用，任何入口都能查远程；
  - 远端须手工部署脚本    → bootstrap：SHA256 校验后自动推送/更新 agent.py；
  - 每次轮询全新 SSH 握手  → ControlMaster=auto + ControlPersist 复用连接；
  - identity 匹配苛刻      → 按主机维度聚合（host 级），不依赖 workspace identity。

零依赖（标准库），远端只要求 python3 + sshd。
"""
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg_mod  # noqa: E402

SETTING_PATH = os.path.join(os.path.expanduser("~"), ".zcode", "v2", "setting.json")
REMOTE_AGENT_DIR = "~/.zcode/token-monitor"
CONTROL_PATH = "~/.ssh/cm-tokmon-%r@%h:%p"
NEG_CACHE = {}  # key -> 失败时间戳

SID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


class Target(object):
    def __init__(self, label, user, host, port=22, ssh_argv=None, key_opt=None):
        self.label = label
        self.user = user
        self.host = host
        self.port = int(port or 22)
        # ssh_argv: 完整 argv（含 "ssh"），定位参数放最后；未提供则由 user/host/port 构造
        self.ssh_argv = ssh_argv
        self.key_opt = key_opt
        self.last_error = None
        self.last_ok_ms = None

    @property
    def key(self):
        if self.host:
            return "%s@%s:%d" % (self.user or "", self.host, self.port)
        # ssh argv 型目标（跳板机等）没有 host 字段：用 label/argv 去重，避免键冲突
        return "argv:%s" % (self.label or " ".join(self.ssh_argv or []))

    def base_argv(self, persist_s=300):
        common = [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=6",
            "-o", "ServerAliveInterval=15",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=" + CONTROL_PATH,
            "-o", "ControlPersist=%d" % persist_s,
        ]
        if self.ssh_argv:
            argv = list(self.ssh_argv)
            if not any(a == "ssh" for a in argv[:1]):
                argv.insert(0, "ssh")
            if not any("ControlMaster" in a for a in argv):
                argv[1:1] = common
            return argv
        argv = ["ssh"] + common
        if self.port != 22:
            argv += ["-p", str(self.port)]
        if self.key_opt:
            argv += ["-i", os.path.expanduser(self.key_opt), "-o", "IdentitiesOnly=yes"]
        argv.append("%s@%s" % (self.user, self.host) if self.user else self.host)
        return argv

    def __repr__(self):
        return "Target(%s, %s)" % (self.label, self.key)


# ---------------------------------------------------------------- 目标发现

def discover_targets(setting_path=None):
    """读 ZCode 桌面 setting.json 的 lastWorkspaceSession，提取 SSH 目标（跳过密码型）。"""
    path = setting_path or SETTING_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = {}
    entries = []
    for k in ("lastWorkspaceSession", "remoteWorkspaceHistory"):
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, list):
            entries += v
        elif isinstance(v, dict):
            entries.append(v)
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        tgt = ent.get("target") or {}
        if ent.get("kind") == "remote" or tgt.get("kind") == "ssh":
            host = tgt.get("host")
            user = tgt.get("username")
            port = int(tgt.get("port") or 22)
            if not host:
                continue
            label = tgt.get("label") or host
            key = "%s@%s:%d" % (user or "", host, port)
            out.setdefault(key, Target(label, user, host, port))
    return list(out.values())


def manual_targets(cfg):
    out = []
    for t in (cfg.get("remote", {}).get("targets") or []):
        if not isinstance(t, dict):
            continue
        label = t.get("label") or t.get("host") or "?"
        ssh = t.get("ssh")
        if isinstance(ssh, str) and ssh.strip():
            try:
                ssh = shlex.split(ssh)
            except ValueError:
                ssh = None
        if isinstance(ssh, list) and ssh:
            out.append(Target(label, t.get("username"), t.get("host", ""), 22, ssh_argv=[str(a) for a in ssh]))
        elif t.get("host"):
            out.append(Target(label, t.get("username"), t["host"], t.get("port") or 22,
                              key_opt=t.get("key")))
    return out


def load_targets(cfg=None):
    """手动 + 自动发现合并去重（手动优先）。"""
    cfg = cfg or cfg_mod.load()
    rcfg = cfg.get("remote", {})
    merged = {}
    for t in manual_targets(cfg):
        merged[t.key] = t
    if rcfg.get("auto_discover", True):
        for t in discover_targets():
            merged.setdefault(t.key, t)
    return list(merged.values())


# ---------------------------------------------------------------- SSH 执行

def _run_ssh(target, remote_cmd, timeout, persist_s=300):
    """执行远端命令，返回 (stdout, stderr, rc)。"""
    argv = target.base_argv(persist_s) + [remote_cmd]
    try:
        # 关键：ssh 绝不能继承 serve 的 stdin——否则它会偷读行协议数据甚至导致 EOF
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.stdout or "", p.stderr or "", p.returncode
    except subprocess.TimeoutExpired:
        return "", "ssh timeout after %ss" % timeout, 124
    except FileNotFoundError:
        return "", "ssh executable not found", 127
    except OSError as e:
        return "", str(e), 128


def master_alive(target, persist_s=300):
    argv = list(target.base_argv(persist_s)) + ["-O", "check"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
        return p.returncode == 0
    except Exception:
        return False


def close_master(target, persist_s=300):
    argv = target.base_argv(persist_s) + ["-O", "exit"]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=5,
                       stdin=subprocess.DEVNULL)
    except Exception:
        pass


_LOCAL_SHA = {"mtime": None, "sha": ""}
_VERIFIED = {}   # target.key -> 已确认部署的 agent sha（进程内缓存，减少每轮 SSH 往返）


def _local_agent_sha():
    """本地 agent.py 的 SHA256（按 mtime 缓存，避免每次轮询都读文件）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "agent.py")
    st = os.stat(path)
    if _LOCAL_SHA["mtime"] != st.st_mtime_ns:
        with open(path, "rb") as f:
            _LOCAL_SHA["sha"] = hashlib.sha256(f.read()).hexdigest()
        _LOCAL_SHA["mtime"] = st.st_mtime_ns
    return _LOCAL_SHA["sha"]


def _remote_sha_cmd(remote_path):
    """等宽：GNU coreutils 的 sha256sum 优先，macOS 默认 shasum 兜底。"""
    return ("(sha256sum %s 2>/dev/null || shasum -a 256 %s 2>/dev/null) | cut -d' ' -f1"
            % (remote_path, remote_path))


def ensure_agent(target, cfg, force=False):
    """远端 agent.py 与本地 SHA256 一致性校验，不符则自动推送。返回 (ok, err)。

    进程内记住已验证的 (target, sha)：文件未变时跳过远端 sha 查询，
    每轮轮询少一次 SSH 往返；查询失败会清除缓存，下轮重新校验。
    """
    rcfg = cfg.get("remote", {})
    if not rcfg.get("bootstrap", True) and not force:
        return True, None
    timeout = rcfg.get("timeout_s", 6)
    persist = rcfg.get("control_persist_s", 300)
    want = _local_agent_sha()
    if not force and _VERIFIED.get(target.key) == want:
        return True, None
    remote_path = REMOTE_AGENT_DIR + "/agent.py"
    out, err, rc = _run_ssh(target, _remote_sha_cmd(remote_path), timeout, persist)
    got = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
    if rc == 0 and got and got.lower() == want:
        _VERIFIED[target.key] = want
        return True, None
    # 部署：mkdir + 通过 stdin 推送（避免引号地狱），文件权限 0644
    _run_ssh(target, "mkdir -p %s" % REMOTE_AGENT_DIR, timeout, persist)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "agent.py"), "rb") as f:
        body = f.read()
    argv = target.base_argv(persist) + ["cat > %s/agent.py && chmod 644 %s/agent.py"
                                       % (REMOTE_AGENT_DIR, REMOTE_AGENT_DIR)]
    try:
        p = subprocess.run(argv, input=body, capture_output=True, timeout=timeout + 8)
        if p.returncode != 0:
            return False, "deploy failed rc=%d: %s" % (p.returncode, (p.stderr or b"")[:160].decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return False, "deploy timeout"
    out, err, rc = _run_ssh(target, _remote_sha_cmd(remote_path), timeout, persist)
    got = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
    if got.lower() != want:
        return False, "post-deploy sha mismatch (got %s)" % got[:16]
    _VERIFIED[target.key] = want
    return True, None


def query_target(target, cfg, since_ms=None, sids=None):
    """查询单个目标。返回 dict：{ok, latency_ms, data|error}。

    sids：同一趟 SSH 里顺带请求这些会话的明细（远端库命中时返回 sidStats）。
    """
    rcfg = cfg.get("remote", {})
    timeout = rcfg.get("timeout_s", 6)
    persist = rcfg.get("control_persist_s", 300)
    neg_s = rcfg.get("neg_cache_s", 60)
    now = time.time()
    fail_at = NEG_CACHE.get(target.key)
    if fail_at is not None and now - fail_at < neg_s:
        return {"ok": False, "error": "negative cache (recent failure, %ds left)"
                % int(neg_s - (now - fail_at)), "latency_ms": None}

    ok, err = ensure_agent(target, cfg)
    if not ok:
        NEG_CACHE[target.key] = now
        return {"ok": False, "error": err, "latency_ms": None}

    cmd = "env ZCODE_TOKMON_CHILD=1 python3 %s/agent.py --json" % REMOTE_AGENT_DIR
    if since_ms is not None:
        cmd += " --since %d" % int(since_ms)
    clean = [s for s in (sids or []) if SID_RE.match(s)]
    if clean:
        cmd += " --sid " + ",".join(clean)
    t0 = time.monotonic()
    out, err, rc = _run_ssh(target, cmd, timeout, persist)
    latency = int((time.monotonic() - t0) * 1000)
    if rc != 0:
        NEG_CACHE[target.key] = now
        _VERIFIED.pop(target.key, None)   # 可能 agent 文件被删/不可执行：下轮重新校验部署
        target.last_error = "ssh rc=%d: %s" % (rc, (err or "")[:160])
        return {"ok": False, "error": target.last_error, "latency_ms": latency}
    # 容忍登录 banner：从尾部找第一个可解析的 JSON 行
    data = None
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if data is None:
        NEG_CACHE[target.key] = now
        _VERIFIED.pop(target.key, None)
        target.last_error = "no JSON in output: %s" % (out or "")[:120]
        return {"ok": False, "error": target.last_error, "latency_ms": latency}
    NEG_CACHE.pop(target.key, None)
    target.last_error = None
    target.last_ok_ms = latency
    return {"ok": True, "latency_ms": latency, "data": data}


def query_all(targets, cfg, since_ms=None, sids=None):
    """并行查询全部目标。返回 {target.key: result}，每项附 label。"""
    if not targets:
        return {}
    timeout = cfg.get("remote", {}).get("timeout_s", 6) + 10  # bootstrap 余量
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(query_target, t, cfg, since_ms, sids): t for t in targets}
        for fut in concurrent.futures.as_completed(futs):
            t = futs[fut]
            try:
                r = fut.result(timeout=timeout + 5)
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": "future error: %s" % e, "latency_ms": None}
            r["label"] = t.label
            r["host"] = t.host
            out[t.key] = r
    return out


def close_all(targets, cfg):
    for t in targets:
        close_master(t, cfg.get("remote", {}).get("control_persist_s", 300))
