import importlib.util
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import time

sys.path.insert(0,str(Path(__file__).parents[1]/'files'))
from accounts import AccountStore,PRODUCTS
from codex_limits import parse_limits,SourceError
from engine_v2 import collect_accounts,retain,base_row,normalized_metrics


class AccountTests(unittest.TestCase):
    def account(self,**overrides):
        data={'id':str(uuid.uuid4()),'product':'deepseek-api','label':'Test','pinned':True};data.update(overrides);return data

    def test_secret_fields_rejected(self):
        with self.assertRaises(ValueError):AccountStore.validate(self.account(apiKey='SECRET'))

    def test_store_private_and_repeated_edits_preserve_rows(self):
        with tempfile.TemporaryDirectory() as root:
            store=AccountStore(Path(root)/'accounts.json')
            for _ in range(2):
                with store.edit() as data:data['accounts'].append(self.account())
            self.assertEqual(len(store.read()['accounts']),2)
            self.assertEqual(store.path.stat().st_mode & 0o777,0o600)

    def test_changed_product_does_not_inherit_cache(self):
        old=base_row(self.account());old.update(lastSuccess='2026-10-10T00:00:00Z',balances=[{'amount':12,'currency':'USD'}])
        current=dict(old,product='moonshot-api',lastSuccess=None,balances=[])
        self.assertEqual(retain(current,old,'offline')['balances'],[])

    def test_stale_values_keep_original_success(self):
        row=base_row(self.account());old=dict(row,lastSuccess='2026-10-10T00:00:00Z',balances=[{'amount':0,'currency':'USD'}])
        result=retain(row,old,'offline')
        self.assertEqual(result['status'],'stale');self.assertEqual(result['balances'][0]['amount'],0)
        self.assertEqual(result['lastSuccess'],old['lastSuccess'])

    def test_remote_errors_never_expose_raw_key(self):
        def remote(*args):raise RuntimeError('SECRET echoed upstream')
        result=collect_accounts({'accounts':[self.account()],'codexAuto':False},{},remote=remote)
        self.assertNotIn('SECRET',json.dumps(result));self.assertEqual(result['accounts'][0]['status'],'source_error')

    def test_independent_accounts_are_preserved(self):
        def remote(*args):return {'balances':[{'amount':0,'currency':'USD'}]}
        result=collect_accounts({'accounts':[self.account(),self.account()],'codexAuto':False},{},remote=remote)
        self.assertEqual(len(result['accounts']),2)
        self.assertTrue(all(r['status']=='live' for r in result['accounts']))

    def test_codex_error_never_reuses_unproven_login_cache(self):
        prior={'id':'codex-cli-active','product':'codex-subscription','lastSuccess':'2026-10-10T00:00:00Z','quotas':[{'usedPercent':20}]}
        def codex():raise SourceError('auth_required')
        with patch('engine_v2.shutil.which',return_value='/bin/codex'):
            result=collect_accounts({'accounts':[]},{'accounts':[prior]},codex=codex)
        self.assertEqual(result['accounts'][0]['quotas'],[])

    def test_codex_identity_not_returned(self):
        with patch('engine_v2.shutil.which',return_value='/bin/codex'):
            result=collect_accounts({'accounts':[]},{},codex=lambda:{'identity':'private@example.invalid','quotas':[{'usedPercent':18}]},identity_salt=b'test')
        self.assertNotIn('private@example',json.dumps(result));self.assertEqual(result['accounts'][0]['status'],'live')

    def test_manual_retry_bypasses_error_backoff_not_rate_limit(self):
        account=self.account();old=base_row(account)
        old.update(retryAt=time.time()+500,failure='offline',status='offline')
        calls=[]
        def remote(*args):calls.append(True);return {'balances':[{'amount':1,'currency':'USD'}]}
        config={'accounts':[account],'codexAuto':False}
        collect_accounts(config,{'accounts':[old]},remote=remote,force=True)
        self.assertEqual(len(calls),1)
        old['failure']='rate_limited'
        collect_accounts(config,{'accounts':[old]},remote=remote,force=True)
        self.assertEqual(len(calls),1)

    def test_openrouter_budget_remains_distinct_from_subscription(self):
        row=base_row(self.account(product='openrouter-key'))
        row.update(status='live',lastSuccess='2026-10-10T00:00:00Z',
                   quotas=[{'label':'Бюджет API-ключа','usedPercent':25}])
        metric=normalized_metrics([row])[0]
        self.assertEqual(metric['kind'],'key_budget')
        self.assertEqual(metric['value'],75)

    def test_bad_reset_does_not_break_other_account_metrics(self):
        row=base_row(self.account())
        row.update(status='live',lastSuccess='2026-10-10T00:00:00Z',
                   quotas=[{'label':'Окно','usedPercent':25,'resetsAt':'not-a-timestamp'}])
        self.assertIsNone(normalized_metrics([row])[0]['reset_at'])


class CodexTests(unittest.TestCase):
    def test_weekly_not_mislabeled_as_five_hours(self):
        rows=parse_limits({'rateLimits':{'secondary':{'usedPercent':18,'windowDurationMins':10080,'resetsAt':1792166070}}})
        self.assertEqual(rows[0]['label'],'Неделя');self.assertEqual(rows[0]['remainingPercent'],82)

    def test_invalid_numbers_not_converted_to_zero(self):
        for value in (None,True,float('nan'),101,-1):
            self.assertEqual(parse_limits({'rateLimits':{'primary':{'usedPercent':value}}}),[])

    def test_multiple_buckets_and_no_duplicate_legacy(self):
        bucket={'primary':{'usedPercent':0,'windowDurationMins':300}}
        rows=parse_limits({'rateLimits':bucket,'rateLimitsByLimitId':{'a':bucket,'b':bucket}})
        self.assertEqual(len(rows),2);self.assertNotEqual(rows[0]['id'],rows[1]['id'])


if __name__=='__main__':unittest.main()
