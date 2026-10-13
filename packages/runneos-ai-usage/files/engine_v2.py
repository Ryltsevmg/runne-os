#!/usr/bin/python3
"""Single serialized read-only collector for GTK and DMS consumers."""
import argparse
import concurrent.futures
import datetime
import email.utils
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import uuid

from accounts import AccountStore, SecretStore, PRODUCTS, private_json
from codex_limits import read_codex, SourceError
from collect import parse_remote, remote_data, command_json, parse_local
from model_v2 import Metric, snapshot as metric_snapshot


def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def state_dir():
    p=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'runneos/ai-usage'
    p.mkdir(parents=True,exist_ok=True,mode=0o700)
    return p


MESSAGES={'auth_required':'Нужно подключить аккаунт заново','keyring_locked':'Разблокируйте хранилище ключей',
 'timeout':'Источник не ответил вовремя','rate_limited':'Источник временно ограничил обновления',
 'source_error':'Ошибка источника; повторите проверку','source_unavailable':'Штатный клиент недоступен',
 'unsupported':'Источник не предоставляет эти метрики','invalid_response':'Формат ответа изменился',
 'offline':'Нет сети или источник недоступен'}


def classify(error):
    if isinstance(error,SourceError):return error.code
    if isinstance(error,urllib.error.HTTPError):
        if error.code in (401,403):return 'auth_required'
        if error.code==429:return 'rate_limited'
        return 'source_error'
    if isinstance(error,(TimeoutError,subprocess.TimeoutExpired)):return 'timeout'
    if isinstance(error,urllib.error.URLError):return 'offline'
    if isinstance(error,ValueError) and str(error) in MESSAGES:return str(error)
    return 'source_error'


def base_row(account):
    product=PRODUCTS[account['product']]
    return {'id':account['id'],'product':account['product'],'provider':product['provider'],
        'name':product['name'],'label':account['label'],'scope':product['scope'],'consoleUrl':product['url'],
        'pinned':account.get('pinned',True),'status':'loading','message':'','quotas':[],'balances':[],
        'metrics':[],'local':None,'lastSuccess':None,'checkedAt':now(),'retryAt':0,'failures':0}


def retain(row, previous, reason):
    row.update(status=reason,message=MESSAGES.get(reason,MESSAGES['source_error']))
    if previous and previous.get('product')==row['product'] and previous.get('lastSuccess'):
        for key in ('quotas','balances','metrics','lastSuccess'):row[key]=previous.get(key)
        if any(row.get(k) for k in ('quotas','balances','metrics')):row['status']='stale'
    row['failure']=reason
    row['failures']=min(6,(previous or {}).get('failures',0)+1)
    row['retryAt']=time.time()+min(3600,60*2**row['failures'])
    return row


def remote_account(account, secret_store):
    product=PRODUCTS[account['product']];provider=product['provider']
    if product['auth']=='local':raise SourceError('unsupported')
    if product['auth']=='cli':
        if provider!='claude' or not shutil.which('dankaiusage'):raise SourceError('source_unavailable')
        from collect import collect_snapshot
        helper=command_json(['dankaiusage','summary','--period-days','7'],timeout=25)
        normalized=collect_snapshot({}, {}, upstream=helper,local={},remote=False)
        row=next((r for r in normalized['providers'] if r['id']=='claude'),{})
        if not row.get('quotas'):raise SourceError('unsupported')
        return {'quotas':row['quotas'],'balances':[],'metrics':[]}
    config={'region':account.get('region','international')};keys={}
    if product['auth'] in ('key','management'):
        key=secret_store.get(account['id'])
        if not key:raise SourceError('auth_required')
        if product['auth']=='management':config.update(managementKey=key,teamId=account['teamId'])
        else:keys[provider]=key
    if product['auth']=='profile':config['aliyunProfile']=account['profile']
    if account['product']=='kimi-code':config['mode']='coding'
    payload=remote_data(provider,config,keys)
    adapter='kimi-coding' if account['product']=='kimi-code' else provider
    result=parse_remote(adapter,payload)
    if not any(result.get(k) for k in ('quotas','balances','metrics')):raise SourceError('unsupported')
    return result


def collect_accounts(config, previous, secrets=None, remote=remote_account, codex=read_codex, identity_salt=b'',force=False):
    old={r['id']:r for r in previous.get('accounts',[])}
    accounts=list(config['accounts'])
    if config.get('codexAuto',True) and shutil.which('codex'):
        # Identifier contains no email; fixed local binding never inherits cache after read failure.
        accounts.insert(0,{'id':'codex-cli-active','product':'codex-subscription','label':'Текущий Codex CLI','pinned':True})
    def one(account):
        row=base_row(account);prior=old.get(row['id'])
        if row['id']!='codex-cli-active' and prior and prior.get('retryAt',0)>time.time() and (not force or prior.get('failure')=='rate_limited'):
            return dict(prior,checkedAt=row['checkedAt'],label=row['label'],pinned=row['pinned'])
        try:
            if row['id']=='codex-cli-active':
                result=codex()
                binding=hmac.new(identity_salt,result['identity'].encode(),hashlib.sha256).hexdigest()
                row.update(quotas=result['quotas'],binding=binding)
            else:row.update(remote(account,secrets))
            row.update(status='live',lastSuccess=now(),message='Данные аккаунта',failure=None)
        except Exception as error:
            # A failed active-account read cannot prove identity: never show another login's cache.
            if row['id']=='codex-cli-active':
                identity=getattr(error,'identity',None)
                binding=hmac.new(identity_salt,identity.encode(),hashlib.sha256).hexdigest() if identity else None
                prior=prior if binding and prior and prior.get('binding')==binding else None
                if binding:row['binding']=binding
            retain(row,prior,classify(error))
            if isinstance(error,urllib.error.HTTPError) and error.code==429:
                retry=(error.headers.get('Retry-After','') if error.headers else '').strip()
                try:
                    delay=float(retry) if retry.isdigit() else email.utils.parsedate_to_datetime(retry).timestamp()-time.time()
                    row['retryAt']=max(row['retryAt'],time.time()+max(0,min(86400,delay)))
                except (ValueError,TypeError,OverflowError):pass
        return row
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(one,accounts))
    return {'schemaVersion':2,'generatedAt':now(),'accounts':rows,'history':[],'sourceErrors':[]}


def normalized_metrics(accounts):
    output=[]
    for account in accounts:
        status='live' if account['status']=='live' else 'stale'
        if not account.get('lastSuccess'):continue
        def add(identity,kind,value,unit,scope,reset=None):
            output.append(Metric(account['provider'],account['product'],account['id'],identity,kind,value,
                unit,scope,'Codex account/rateLimits/read' if account['id']=='codex-cli-active' else PRODUCTS[account['product']]['scope'],
                account['lastSuccess'],account['lastSuccess'],status,reset,account['checkedAt'],
                account.get('failure') if account.get('failure') in ('auth_required','rate_limited','error') else ('error' if status=='stale' else None)))
        for index,q in enumerate(account.get('quotas') or []):
            kind='key_budget' if account['product']=='openrouter-key' else 'quota_remaining'
            reset=q.get('resetsAt')
            try:
                from model_v2 import time_value
                if not isinstance(reset,str):reset=None
                elif reset:time_value(reset)
            except (ValueError,TypeError):reset=None
            add(q.get('id',f'quota-{index}'),kind,100-q['usedPercent'],'%',q['label'],reset)
        for balance in account.get('balances') or []:add('balance-'+balance['currency'],'api_balance',balance['amount'],balance['currency'],account['scope'])
        for index,metric in enumerate(account.get('metrics') or []):add(f'cost-{index}','reported_cost',metric['value'],metric['unit'],metric['label'])
    return metric_snapshot(output)['metrics']


def read_snapshot(force=False, offline=False):
    # Desktop services have a smaller PATH than interactive shells. These are
    # the user's established CLI installation directories, not project paths.
    os.environ['PATH']=os.pathsep.join([str(Path.home()/'.local/bin'),str(Path.home()/'.opencode/bin'),
                                      os.environ.get('PATH','/usr/bin:/bin')])
    root=state_dir();cache=root/'accounts-v2.json';store=AccountStore()
    fd=os.open(root/'accounts-v2.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        config=store.read();fingerprint=store.fingerprint()+':v2.2:'+str(bool(shutil.which('codex')))
        try:previous=json.loads(cache.read_text())
        except (OSError,ValueError):previous={}
        if previous.get('configFingerprint')!=fingerprint:previous={}
        if offline:
            # Offline GUI reads are fast; no credentials/helpers/network touched.
            result=previous or {'schemaVersion':2,'accounts':[],'history':[],'sourceErrors':[]}
            result['offline']=True
            for row in result['accounts']:
                if row.get('lastSuccess'):row.update(status='stale',message='Сохранённые данные · офлайн')
            return result
        age=time.time()-cache.stat().st_mtime if cache.exists() else 1e9
        if previous and age<(10 if force else 180):return previous
        salt=root/'identity-salt'
        if not salt.exists():
            descriptor=os.open(salt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as stream:stream.write(os.urandom(32))
        result=collect_accounts(config,previous,SecretStore(),identity_salt=salt.read_bytes(),force=force)
        period=config.get('periodDays',7)
        period=period if period in (7,30,90) else 7
        def local_source():
            if not shutil.which('opencode'):return [],'unavailable'
            try:
                payload=command_json(['opencode','stats','--json','--days',str(period),'--models','--cost'],timeout=25)
                return [{'provider':p,**v} for p,v in parse_local(payload).items()],None
            except Exception:return [],'source_error'
        history,error=local_source();result['history']=history;result['periodDays']=period
        if error:result['sourceErrors'].append({'source':'OpenCode local history','status':error})
        # CLI transcript history is independently attributed, never replaces OpenCode totals.
        if shutil.which('dankaiusage'):
            try:
                helper=command_json(['dankaiusage','summary','--period-days',str(period)],timeout=25)
                for row in helper.get('providers',[]):
                    total=row.get('period',{}).get('total')
                    if row.get('id') in ('codex','claude') and isinstance(total,(int,float)) and (total>0 or row.get('available')):
                        result['history'].append({'provider':row['id'],'tokens':total,'cost':None,
                            'source':row['id']+' CLI','scope':'Только этот компьютер, выбранный период'})
            except Exception:result['sourceErrors'].append({'source':'CLI local history','status':'source_error'})
        result['metricsV2']=normalized_metrics(result['accounts'])
        result['configFingerprint']=fingerprint;private_json(cache,result)
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force',action='store_true');parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    try:result=read_snapshot(args.force,args.offline)
    except Exception:
        print(json.dumps({'schemaVersion':2,'error':'Не удалось прочитать настройки или хранилище','accounts':[],'history':[]}))
        return 1
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
