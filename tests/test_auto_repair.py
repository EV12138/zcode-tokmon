#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自修复代理单测：LaunchAgent 配置生成与开关解析。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "zcode-token-monitor"))
import auto_repair  # noqa: E402


class AutoRepairTest(unittest.TestCase):
    def test_plist_contents(self):
        data = auto_repair.build_plist(
            "/usr/bin/python3", "/tmp/auto_repair.py", "/tmp/app.asar", "/tmp/launch.log")
        self.assertEqual(data["Label"], "ai.tokmon.repair")
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/python3", "/tmp/auto_repair.py"])
        self.assertEqual(data["WatchPaths"], ["/tmp/app.asar"])
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["StartInterval"], 60)
        self.assertEqual(data["StandardOutPath"], "/tmp/launch.log")

    def test_version_sort(self):
        self.assertGreater(auto_repair.version_key("1.1.13"), auto_repair.version_key("1.1.9"))
        self.assertGreater(auto_repair.version_key("1.2.0"), auto_repair.version_key("1.1.99"))

    def test_enabled_default_and_off(self):
        with mock.patch.object(auto_repair, "CONFIG", auto_repair.Path("/nonexistent/config.json")):
            self.assertTrue(auto_repair.enabled())
        with tempfile.TemporaryDirectory() as tmp:
            cfg = auto_repair.Path(tmp) / "config.json"
            cfg.write_text('{"auto_repair": false}', encoding="utf-8")
            with mock.patch.object(auto_repair, "CONFIG", cfg):
                self.assertFalse(auto_repair.enabled())


if __name__ == "__main__":
    unittest.main()
