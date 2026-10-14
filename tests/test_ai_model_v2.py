import importlib.util
import json
from pathlib import Path
import sys
import unittest

FILE = Path(__file__).parents[1]/'packages/runneos-ai-usage/files/model_v2.py'
spec = importlib.util.spec_from_file_location('ai_model_v2', FILE)
model = importlib.util.module_from_spec(spec);sys.modules[spec.name] = model;spec.loader.exec_module(model)


class MetricTests(unittest.TestCase):
    def metric(self, **overrides):
        data = dict(provider='openai', product='codex-subscription', account='local-id-1',
                    id='5h', kind='quota_remaining', value=62, unit='%', scope='5h', source='account-api',
                    observed_at='2026-10-10T12:00:00Z', last_success='2026-10-10T12:00:00Z', status='live')
        data.update(overrides);return model.Metric(**data)

    def test_unknown_and_zero_remain_distinct(self):
        self.assertIsNone(self.metric(value=None, status='unsupported').value)
        self.assertEqual(self.metric(value=0).value, 0)

    def test_unknown_is_not_live(self):
        with self.assertRaises(ValueError):self.metric(value=None)

    def test_multiple_accounts_and_products_do_not_merge(self):
        rows=[self.metric(), self.metric(account='local-id-2'), self.metric(product='other-product')]
        self.assertEqual(len(model.snapshot(rows)['metrics']), 3)

    def test_duplicates_rejected(self):
        with self.assertRaises(ValueError):model.snapshot([self.metric(), self.metric()])

    def test_bad_numbers_rejected(self):
        for value in (True, float('nan'), float('inf'), -1, 101):
            with self.subTest(value=value), self.assertRaises(ValueError):self.metric(value=value)

    def test_timestamps_must_have_timezone(self):
        with self.assertRaises(ValueError):self.metric(observed_at='2026-10-10T12:00:00')

    def test_offline_retains_original_success_age(self):
        previous=self.metric()
        failed=self.metric(value=None, status='error', last_success=None)
        retained=model.last_known_good(previous, failed, '2026-10-10T13:00:00Z')
        self.assertEqual(retained.value, 62)
        self.assertEqual(retained.status, 'stale')
        self.assertEqual(retained.last_success, previous.last_success)
        self.assertEqual(retained.observed_at, previous.observed_at)
        self.assertEqual(retained.checked_at, '2026-10-10T13:00:00Z')
        self.assertEqual(retained.failure, 'error')

    def test_another_account_cannot_inherit_balance(self):
        with self.assertRaises(ValueError):model.last_known_good(self.metric(), self.metric(account='other'), '2026-10-10T13:00:00Z')

    def test_changed_scope_cannot_inherit_cached_value(self):
        with self.assertRaises(ValueError):model.last_known_good(self.metric(), self.metric(scope='week'), '2026-10-10T13:00:00Z')

    def test_auth_failure_is_retained_as_typed_reason(self):
        failed=self.metric(value=None, status='auth_required', last_success=None)
        retained=model.last_known_good(self.metric(),failed,'2026-10-10T13:00:00Z')
        self.assertEqual(retained.failure,'auth_required')

    def test_raw_error_text_cannot_enter_failure_field(self):
        with self.assertRaises(ValueError):self.metric(failure='credential echoed by API')

    def test_tokens_cannot_be_a_quota_percentage(self):
        with self.assertRaises(ValueError):self.metric(kind='local_tokens')

    def test_output_has_no_extra_secret_fields(self):
        payload=model.snapshot([self.metric()])
        self.assertEqual(payload['schemaVersion'], 2)
        self.assertNotIn('credentials', json.dumps(payload))
        self.assertNotIn('apiKey', json.dumps(payload))


if __name__ == '__main__':unittest.main()
