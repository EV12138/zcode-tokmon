#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文窗口来源单测：模型配置优先、provider 精确匹配、插件覆盖。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "zcode-token-monitor"))
import agent  # noqa: E402


CONFIG = {
    "provider": {
        "prov-a": {
            "models": {
                # legacy provider 目录：1M
                "deepseek-flash": {"limit": {"context": 1048576}},
                "shared-model": {"limit": {"context": 300000}},
            }
        },
        "prov-b": {
            "models": {
                "shared-model": {"limit": {"context": 1000000}},
            }
        },
    }
}

RULES = {
    "config": {
        "modelConfigRules": {
            "providerModelRules": [
                # 模型设置界面的当前值：300k，应覆盖 legacy 的 1M
                {"providerId": "prov-a", "modelId": "deepseek-flash",
                 "config": {"properties": {"contextWindow": 300000}}},
                {"providerId": "prov-b", "modelId": "rule-only",
                 "config": {"properties": {"contextWindow": 262144}}},
            ]
        }
    }
}


class ModelContextTest(unittest.TestCase):
    def setUp(self):
        self.catalog = agent.build_model_context_catalog(CONFIG, RULES)

    def test_model_rule_wins_over_legacy_provider(self):
        self.assertEqual(
            agent.resolve_model_context("deepseek-flash", "prov-a", self.catalog), 300000)

    def test_provider_scoped_models_do_not_mix(self):
        self.assertEqual(
            agent.resolve_model_context("shared-model", "prov-a", self.catalog), 300000)
        self.assertEqual(
            agent.resolve_model_context("shared-model", "prov-b", self.catalog), 1000000)

    def test_rules_are_fallback(self):
        self.assertEqual(
            agent.resolve_model_context("rule-only", "prov-b", self.catalog), 262144)

    def test_unknown_returns_zero(self):
        self.assertEqual(agent.resolve_model_context("nope", "prov-a", self.catalog), 0)

    def test_plugin_override_exact_and_substring(self):
        self.assertEqual(
            agent.resolve_model_context("deepseek-flash", "prov-a", self.catalog,
                                        {"deepseek-flash": 131072}), 131072)
        self.assertEqual(
            agent.resolve_model_context("deepseek-flash", "prov-a", self.catalog,
                                        {"deepseek": 65536}), 65536)

    def test_user_default_only_when_unknown(self):
        self.assertEqual(
            agent.resolve_model_context("nope", "prov-a", self.catalog, {}, 200000), 200000)
        # 已知模型仍用模型配置，不被默认值覆盖
        self.assertEqual(
            agent.resolve_model_context("deepseek-flash", "prov-a", self.catalog, {}, 200000), 300000)

    def test_source_labels(self):
        self.assertEqual(
            agent.resolve_model_context_ex("deepseek-flash", "prov-a", self.catalog)[1],
            "zcode-rules")
        self.assertEqual(
            agent.resolve_model_context_ex("shared-model", "prov-a", self.catalog)[1],
            "zcode-config")
        self.assertEqual(
            agent.resolve_model_context_ex("rule-only", "prov-b", self.catalog)[1],
            "zcode-rules")
        self.assertEqual(
            agent.resolve_model_context_ex("deepseek-flash", "prov-a", self.catalog,
                                           {"deepseek-flash": 1})[1],
            "plugin-config")

    def test_attach_windows_to_snapshot(self):
        snap = {
            "current": {"model": "deepseek-flash", "provider": "prov-a"},
            "sessions": [{"model": "shared-model", "provider": "prov-b"}],
            "sidStats": {"s": {"model": "unknown-model", "provider": "prov-a"}},
        }
        agent.attach_context_windows(snap, self.catalog)
        self.assertEqual(snap["current"]["contextWindow"], 300000)
        self.assertEqual(snap["sessions"][0]["contextWindow"], 1000000)
        self.assertNotIn("contextWindow", snap["sidStats"]["s"])


if __name__ == "__main__":
    unittest.main()
