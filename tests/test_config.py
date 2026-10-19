#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文窗口解析单测（不依赖本机 ZCode 模型目录）。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "zcode-token-monitor"))
import config  # noqa: E402


class ContextWindowTest(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            "exact": {"k3-256k": 262144, "k3": 1048576, "deepseek-flash": 1048576},
            "patterns": [
                (".*", 200000),
                (r".*glm-5\.3(?:-flash)?(?:[.\-:/\[].*)?", 1000000),
            ],
            "default": 200000,
        }

    def test_exact_model_id(self):
        self.assertEqual(config.resolve_context_window("k3-256k", self.catalog), 262144)

    def test_longest_substring_wins(self):
        self.assertEqual(config.resolve_context_window("k3-256k-latest", self.catalog), 262144)
        self.assertEqual(config.resolve_context_window("k3", self.catalog), 1048576)

    def test_pattern_case_insensitive(self):
        self.assertEqual(config.resolve_context_window("GLM-5.3", self.catalog), 1000000)

    def test_default(self):
        self.assertEqual(config.resolve_context_window("unknown-model", self.catalog), 200000)

    def test_user_override(self):
        self.assertEqual(
            config.resolve_context_window("GLM-5.3", self.catalog, {"glm-5.3": 500000}), 500000)

    def test_user_default(self):
        self.assertEqual(
            config.resolve_context_window("unknown-model", self.catalog, {}, 128000), 128000)

    def test_empty_model(self):
        self.assertEqual(config.resolve_context_window("", self.catalog), 0)


class CatalogSourceTest(unittest.TestCase):
    def test_model_rule_beats_legacy_provider_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            rules_path = os.path.join(tmp, "provider_config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"provider": {"p": {"models": {
                    "deepseek-flash": {"limit": {"context": 1048576}}}}}}, f)
            with open(rules_path, "w", encoding="utf-8") as f:
                json.dump({"config": {"modelConfigRules": {"providerModelRules": [
                    {"providerId": "p", "modelId": "deepseek-flash",
                     "config": {"properties": {"contextWindow": 300000}}}]}}}, f)
            with mock.patch.object(config, "USER_MODEL_CONFIG_PATH", cfg_path), \
                    mock.patch.object(config, "CATALOG_PATHS", [rules_path]):
                catalog = config.context_catalog()
                self.assertEqual(catalog["exact"]["deepseek-flash"], 300000)
                self.assertEqual(
                    config.resolve_context_window("deepseek-flash", catalog), 300000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
