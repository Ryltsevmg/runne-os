"""Product registry and private account references. Secrets live only in Keyring."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid


PRODUCTS = {
 'codex-subscription': {'provider':'codex','name':'Codex · подписка','auth':'cli','scope':'Лимиты ChatGPT/Codex через штатный CLI','url':'https://chatgpt.com/codex/settings/usage'},
 'claude-subscription': {'provider':'claude','name':'Claude · подписка','auth':'cli','scope':'Штатный Claude CLI и доступные квоты','url':'https://claude.ai/settings/usage'},
 'deepseek-api': {'provider':'deepseek','name':'DeepSeek · API','auth':'key','scope':'Баланс API, валюты раздельно','url':'https://platform.deepseek.com/usage'},
 'openrouter-key': {'provider':'openrouter','name':'OpenRouter · API-ключ','auth':'key','scope':'Расход и бюджет ключа, не баланс аккаунта','url':'https://openrouter.ai/settings/credits'},
 'zai-coding': {'provider':'zai','name':'Z.ai · Coding Plan','auth':'key','scope':'Окна Coding Plan, не обычный API-баланс','url':'https://z.ai/manage-apikey/subscription'},
 'kimi-code': {'provider':'kimi','name':'Kimi Code','auth':'key','scope':'Квоты Kimi Code','url':'https://www.kimi.com/code/console'},
 'moonshot-api': {'provider':'kimi','name':'Moonshot · API','auth':'key','scope':'Баланс Moonshot API, не Kimi Code','url':'https://platform.moonshot.ai/console/account'},
 'xai-team': {'provider':'xai','name':'x.ai · команда','auth':'management','scope':'Предоплаченный баланс команды; нужен Management Key и Team ID','url':'https://console.x.ai/'},
 'qwen-alibaba': {'provider':'qwen','name':'Qwen / Alibaba Cloud','auth':'profile','scope':'Баланс всего Alibaba Cloud, не квота Qwen','url':'https://bailian.console.aliyun.com/'},
 'opencode-go': {'provider':'opencode-go','name':'OpenCode Go','auth':'local','scope':'Локальная история; API остатка подписки не подтверждён','url':'https://opencode.ai/console'},
 'openai-api': {'provider':'codex','name':'OpenAI · API','auth':'local','scope':'Локальная история; API-баланс здесь не поддержан','url':'https://platform.openai.com/usage'},
 'claude-api': {'provider':'claude','name':'Anthropic · API','auth':'local','scope':'Локальная история; не лимит подписки Claude','url':'https://console.anthropic.com/'},
}
SCHEMA_NAME = 'org.runneos.AIAccount.v1'


def private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        json.dump(data, output, ensure_ascii=False, indent=2);output.write('\n')
        temporary=Path(output.name)
    temporary.replace(path)


class AccountStore:
    def __init__(self, path=None):
        root=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))
        self.path=Path(path) if path else root/'runneos/ai-accounts.json'

    def read(self):
        if not self.path.exists():return {'schemaVersion':2,'accounts':[],'periodDays':7}
        if self.path.stat().st_mode & 0o077:raise ValueError('Account configuration must be private')
        data=json.loads(self.path.read_text())
        if not isinstance(data,dict) or data.get('schemaVersion')!=2 or not isinstance(data.get('accounts'),list):
            raise ValueError('Unsupported account configuration')
        if set(data)-{'schemaVersion','accounts','periodDays','codexAuto','revision'}:raise ValueError('Unsupported root fields')
        if 'codexAuto' in data and not isinstance(data['codexAuto'],bool):raise ValueError('Invalid auto-discovery flag')
        if len(data['accounts'])>32:raise ValueError('Too many accounts')
        ids=[]
        for account in data['accounts']:
            self.validate(account);ids.append(account['id'])
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate account identity')
        return data

    @staticmethod
    def validate(account):
        if not isinstance(account,dict) or set(account)-{'id','product','label','region','teamId','profile','pinned'}:
            raise ValueError('Account config contains unsupported fields; never store keys here')
        if account.get('product') not in PRODUCTS:raise ValueError('Unknown product')
        if not re.fullmatch(r'[0-9a-f-]{36}',account.get('id','')):raise ValueError('Invalid account id')
        uuid.UUID(account['id'])
        label=account.get('label','')
        if not isinstance(label,str) or not 1<=len(label.strip())<=80 or any(ord(c)<32 for c in label):raise ValueError('Invalid label')
        if account.get('region','international') not in ('international','china'):raise ValueError('Unknown region')
        for key in ('teamId','profile'):
            if account.get(key) and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}',account[key]):raise ValueError('Invalid extra account field')
        if 'pinned' in account and not isinstance(account['pinned'],bool):raise ValueError('Invalid pin flag')
        if account['product']=='xai-team' and not account.get('teamId'):raise ValueError('Team ID required')
        if account['product']=='qwen-alibaba' and not account.get('profile'):raise ValueError('Alibaba CLI profile required')

    def fingerprint(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else 'empty'

    @contextlib.contextmanager
    def edit(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.path.with_suffix('.lock'),os.O_RDWR|os.O_CREAT,0o600)
        with os.fdopen(fd,'w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            data=self.read();yield data
            for row in data['accounts']:self.validate(row)
            private_json(self.path,data)


class SecretStore:
    def __init__(self):
        import gi
        gi.require_version('Secret','1')
        from gi.repository import Secret,Gio
        self.Secret=Secret;self.Gio=Gio
        self.schema=Secret.Schema.new(SCHEMA_NAME,Secret.SchemaFlags.NONE,
            {'account_id':Secret.SchemaAttributeType.STRING})

    @contextlib.contextmanager
    def deadline(self,seconds=10):
        cancel=self.Gio.Cancellable();timer=threading.Timer(seconds,cancel.cancel);timer.start()
        try:yield cancel
        finally:timer.cancel()

    def get(self,account_id):
        with self.deadline() as cancel:
            s=self.Secret.Service.get_sync(self.Secret.ServiceFlags.OPEN_SESSION,cancel)
            rows=s.search_sync(self.schema,{'account_id':account_id},
                self.Secret.SearchFlags.ALL|self.Secret.SearchFlags.LOAD_SECRETS,cancel)
            if not rows:raise ValueError('auth_required')
            if any(item.get_locked() for item in rows):raise ValueError('keyring_locked')
            value=rows[0].get_secret()
            if value is None:raise ValueError('auth_required')
            return value.get_text()

    def put(self,account_id,label,value):
        if not isinstance(value,str) or not value.strip() or len(value)>8192 or any(ord(c)<32 for c in value):
            raise ValueError('Invalid credential')
        with self.deadline(120) as cancel:
            if not self.Secret.password_store_sync(self.schema,{'account_id':account_id},
                self.Secret.COLLECTION_DEFAULT,'RunneOS AI · '+label,value.strip(),cancel):
                raise ValueError('Credential store failed')

    def delete(self,account_id):
        with self.deadline(120) as cancel:
            self.Secret.password_clear_sync(self.schema,{'account_id':account_id},cancel)
