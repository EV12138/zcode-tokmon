#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token monitor 配置：~/.zcode/token-monitor/config.json

首次运行自动生成默认配置；损坏的配置会备份为 *.bad 后回退默认值。
"""
import copy
import json
import os
import time

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".zcode", "token-monitor")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "python_path": "",             # 主进程数据泵用的 python3 绝对路径（空 = 用 PATH 里的 python3）
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
