import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "collect", Path(__file__).resolve().parents[1] / "files" / "collect.py"
)
collect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect)


class ProviderTests(unittest.TestCase):
    def test_every_requested_provider_is_visible_without_credentials(self):
        rows = collect.collect_snapshot({}, {}, upstream={}, local={"models": []}, remote=False)["providers"]
        self.assertEqual({r["id"] for r in rows}, {
            "zai", "xai", "kimi", "deepseek", "qwen", "codex", "claude", "opencode-go", "openrouter"
        })
        self.assertTrue(all(r["balance"] is None for r in rows))
        self.assertTrue(all(r["status"] == "not_connected" for r in rows))

    def test_deepseek_keeps_currencies_separate_and_zero_is_valid(self):
        result = collect.parse_remote("deepseek", {"is_available": False, "balance_infos": [
            {"currency": "USD", "total_balance": "0.00"},
            {"currency": "CNY", "total_balance": "12.50"}
        ]})
        self.assertEqual(result["balances"], [{"currency": "USD", "amount": 0.0}, {"currency": "CNY", "amount": 12.5}])

    def test_openrouter_key_budget_is_not_account_balance(self):
        result = collect.parse_remote("openrouter", {"data": {
            "limit": None, "limit_remaining": None, "usage_monthly": 7.5,
            "label": "SECRET", "usage": 12
        }})
        self.assertIsNone(result.get("balance"))
        self.assertEqual(result["metrics"], [{"label": "Расход ключа за месяц", "value": 7.5, "unit": "USD"}])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_openrouter_remaining_key_limit(self):
        result = collect.parse_remote("openrouter", {"data": {"limit": 20, "limit_remaining": 15, "usage_monthly": 5}})
        self.assertEqual(result["quotas"][0]["usedPercent"], 25)
        self.assertEqual(result["quotas"][0]["remaining"], 15)

    def test_zai_percent_and_missing_reset_not_invented(self):
        result = collect.parse_remote("zai", {"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": 42}]}})
        self.assertEqual(result["quotas"][0]["usedPercent"], 42)
        self.assertIsNone(result["quotas"][0]["resetsAt"])

    def test_kimi_and_xai_balances(self):
        self.assertEqual(collect.parse_remote("kimi", {"data": {"available_balance": 3.25}})["balances"][0]["amount"], 3.25)
        self.assertEqual(collect.parse_remote("xai", {"total": {"val": "2500"}})["balances"][0]["amount"], 25)

    def test_kimi_code_used_and_reset_alias(self):
        result = collect.parse_remote("kimi-coding", {"usage": {"limit": 100, "used": 20, "reset_at": "2026-10-11T00:00:00Z"}, "limits": []})
        self.assertEqual(result["quotas"][0]["usedPercent"], 20)
        self.assertEqual(result["quotas"][0]["resetsAt"], "2026-10-11T00:00:00Z")

    def test_zen_is_not_opencode_go(self):
        self.assertEqual(collect.parse_local({"models": [{"providerID": "opencode", "tokens": 10}]}), {})

    def test_missing_local_cost_is_unknown_including_mixed_rows(self):
        data = {"models": [{"providerID": "deepseek", "tokens": 10, "cost": 0.1}, {"providerID": "deepseek", "tokens": 5}]}
        self.assertIsNone(collect.parse_local(data)["deepseek"]["cost"])

    def test_api_key_does_not_replace_cli_quota(self):
        summary = {"providers": [{"id": "codex", "quotaBuckets": [{"label": "5h", "allowance": {"known": True, "percentUsed": 20}}]}]}
        row = collect.collect_snapshot({}, {"codex": "API_KEY"}, upstream=summary)["providers"][0]
        self.assertEqual(row["status"], "ok")

    def test_bad_key_file_does_not_hide_other_providers(self):
        config = {"deepseek": {"apiKeyFile": "/nonexistent/runneos/test-key"}}
        collect.credentials(config)
        rows = collect.collect_snapshot(config, {}, remote=False)["providers"]
        self.assertEqual(len(rows), 9)
        self.assertEqual(next(r for r in rows if r["id"] == "deepseek")["status"], "error")

    def test_qwen_cloud_balance_has_own_scope(self):
        result = collect.parse_remote("qwen", {"Success": True, "Data": {"AvailableCashAmount": "12.00", "Currency": "CNY"}})
        self.assertEqual(result["balances"][0]["amount"], 12)
        self.assertIn("Alibaba Cloud", result["scope"])

    def test_invalid_numeric_value_does_not_become_zero(self):
        result = collect.parse_remote("deepseek", {"balance_infos": [{"currency": "USD", "total_balance": "not-a-number"}]})
        self.assertEqual(result["balances"], [])
        self.assertIsNone(collect.number(float("nan")))

    def test_gateway_usage_not_attributed_to_model_vendor(self):
        local = collect.parse_local({"models": [
            {"providerID": "openrouter", "modelID": "qwen/qwen3", "tokens": {"input": 20, "output": 5}, "cost": 0.05, "requests": 2},
            {"providerID": "opencode-go", "modelID": "kimi-k2", "tokens": {"input": 10, "output": 1}, "cost": 0, "requests": 1}
        ]})
        self.assertEqual(local["openrouter"]["tokens"], 25)
        self.assertEqual(local["opencode-go"]["tokens"], 11)
        self.assertNotIn("qwen", local)
        self.assertNotIn("kimi", local)

    def test_opencode_2_nested_model_shape(self):
        local = collect.parse_local({"models": [{"model": {"providerID": "openrouter", "id": "qwen/qwen3"}, "tokens": {"input": 20, "output": 5, "cache": {"read": 3, "write": 2}}, "cost": 0.05, "steps": 2}]})
        self.assertEqual(local["openrouter"]["tokens"], 30)
        self.assertEqual(local["openrouter"]["cost"], 0.05)
        self.assertEqual(local["openrouter"]["requests"], 2)

    def test_local_only_is_explicit_and_no_subscription_percentage(self):
        local = {"models": [{"providerID": "qwen", "tokens": {"input": 10, "output": 5}, "cost": 0}]}
        row = next(r for r in collect.collect_snapshot({}, {}, upstream={}, local=local, remote=False)["providers"] if r["id"] == "qwen")
        self.assertEqual(row["status"], "local_only")
        self.assertEqual(row["local"]["tokens"], 15)
        self.assertEqual(row["quotas"], [])
        self.assertIsNone(row["balance"])

    def test_credentials_never_in_snapshot_or_errors(self):
        def failing_fetch(*args, **kwargs):
            raise RuntimeError("SECRET credential echoed by upstream")
        result = collect.collect_snapshot({}, {"deepseek": "SECRET"}, upstream={}, local={}, fetch=failing_fetch)
        self.assertNotIn("SECRET", json.dumps(result))
        row = next(r for r in result["providers"] if r["id"] == "deepseek")
        self.assertEqual(row["status"], "error")

    def test_read_api_credentials_ignores_oauth_and_maps_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"auth.json"
            path.write_text(json.dumps({"zai-coding-plan": {"type": "api", "key": "secret"}, "openai": {"type": "oauth", "access": "no"}}))
            self.assertEqual(collect.read_opencode_auth(path), {"zai": "secret"})

    def test_http_rejects_redirects_and_unknown_hosts(self):
        with self.assertRaises(ValueError):
            collect.http_json("https://example.com/steal", "SECRET")
        handler = collect.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_upstream_codex_normalized_without_private_metadata(self):
        summary = {"providers": [{"id": "codex", "available": True, "quotaBuckets": [{"label": "5h", "allowance": {"known": True, "percentUsed": 20}}], "period": {"total": 100}, "meta": {"secret": "SECRET"}}]}
        rows = collect.collect_snapshot({}, {}, upstream=summary, local={}, remote=False)["providers"]
        row = next(r for r in rows if r["id"] == "codex")
        self.assertEqual(row["quotas"][0]["usedPercent"], 20)
        self.assertNotIn("SECRET", json.dumps(row))


if __name__ == "__main__":
    unittest.main()
