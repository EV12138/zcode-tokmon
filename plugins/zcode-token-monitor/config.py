#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token monitor 配置：~/.zcode/token-monitor/config.json

首次运行自动生成默认配置；损坏的配置会备份为 *.bad 后回退默认值。
"""
import copy
import json
import os
import re
import time

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".zcode", "token-monitor")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

CATALOG_PATHS = [
    os.path.join(os.path.expanduser("~"), ".zcode", "v2", "provider_config.json"),
    "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json",
    "/opt/ZCode/resources/config/provider/zcode-builtin.json",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "ZCode",
                 "resources", "config", "provider", "zcode-builtin.json"),
]

DEFAULTS = {
    "python_path": "",             # 主进程数据泵用的 python3 绝对路径（空 = 用 PATH 里的 python3）
    "context_windows": {},         # 模型 -> 上下文窗口覆盖（按模型 id 子串匹配）
    "context_window_default": 0,   # 未匹配时的窗口（0 = 用 ZCode 模型目录默认值）
    "remote": {
        "auto_discover": True,     # 自动读 ~/.zcode/v2/setting.json 的 SSH 工作区记录
        "targets": [],             # 手动目标，见 README；与自动发现按 (user,host,port) 去重，手动优先
        "poll_s": 2,               # watch 面板轮询间隔（秒）
        "timeout_s": 6,            # 单目标 SSH 查询超时（秒）
        "control_persist_s": 300,  # SSH ControlMaster 复用连接保活（秒）
        "bootstrap": True,         # 自动部署/更新远端 agent.py
        "neg_cache_s": 60,         # 失败目标的负缓存时长（秒）
    }
}


def load(path=None):
    path = path or CONFIG_PATH
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        try:
            save(cfg, path)
        except OSError:
            pass   # 只读环境：用内存默认值继续，不阻塞查询
        return cfg
    except (ValueError, OSError):
        try:
            os.replace(path, path + ".bad-%d" % int(time.time()))
        except OSError:
            pass
        try:
            save(cfg, path)
        except OSError:
            pass
        return cfg
    if isinstance(user, dict):
        merge(cfg, user)
    return cfg


def merge(base, extra):
    for k, v in extra.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            merge(base[k], v)
        else:
            base[k] = v


def save(cfg, path=None):
    path = path or CONFIG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 上下文窗口

def context_catalog():
    """从 ZCode 的 provider 模型目录读取上下文窗口。

    provider_config.json 里的精确 modelId 优先；内置 catalog 的 modelMatch
    规则按顺序求值（后者覆盖前者，"" .* "" 提供默认值）。
    """
    exact = {}
    patterns = []
    default = 0
    for path in CATALOG_PATHS:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        rules = ((data.get("config") or {}).get("modelConfigRules") or {})
        for bucket in ("providerModelRules", "modelRules"):
            for rule in rules.get(bucket) or []:
                if not isinstance(rule, dict):
                    continue
                props = ((rule.get("config") or {}).get("properties") or {})
                try:
                    window = int(props.get("contextWindow") or 0)
                except (TypeError, ValueError):
                    window = 0
                if window <= 0:
                    continue
                mid = rule.get("modelId")
                pattern = rule.get("modelMatch")
                if mid:
                    exact.setdefault(str(mid).lower(), window)
                elif pattern:
                    patterns.append((str(pattern), window))
                    if pattern == ".*":
                        default = window
    return {"exact": exact, "patterns": patterns, "default": default}


def resolve_context_window(model, catalog=None, user_windows=None, user_default=0):
    """模型 id -> 上下文窗口。用户配置优先，其次精确 id / 最长子串 / 正则规则。"""
    if not model:
        return 0
    catalog = catalog or context_catalog()
    m = str(model).lower()
    exact = catalog.get("exact") or {}
    if m in exact:
        return exact[m]
    merged = {}
    for k, v in (user_windows or {}).items():
        try:
            merged[str(k).lower()] = int(v)
        except (TypeError, ValueError):
            pass
    for k, v in exact.items():
        merged.setdefault(k, v)
    best = 0
    best_len = -1
    for k, v in merged.items():
        if k and k in m and len(k) > best_len:
            best = v
            best_len = len(k)
    if best:
        return best
    window = int(user_default or catalog.get("default") or 0)
    for pattern, value in catalog.get("patterns") or []:
        if pattern == ".*":
            continue   # 兜底规则只提供默认值，不覆盖用户自定义默认
        try:
            if re.search(pattern, str(model), re.I):
                window = int(value)
        except re.error:
            continue
    return window
