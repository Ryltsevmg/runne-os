"""Read-only Codex app-server adapter. Never creates a thread or model request."""
import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import time
from datetime import datetime, timezone


class SourceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def window_label(minutes):
    if minutes == 300:return 'Окно 5 часов'
    if minutes == 10080:return 'Неделя'
    if minutes and minutes % 1440 == 0:return f'{minutes//1440} дн.'
    if minutes and minutes % 60 == 0:return f'{minutes//60} ч.'
    return f'{minutes} мин.' if minutes else 'Окно лимита'


def parse_limits(data):
    buckets = data.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        buckets = {'codex': data.get('rateLimits', {})}
    result=[]
    for bucket_id, bucket in list(buckets.items())[:16]:
        if not isinstance(bucket,dict):continue
        for slot in ('primary','secondary'):
            row=bucket.get(slot)
            if not isinstance(row,dict):continue
            used=row.get('usedPercent');minutes=row.get('windowDurationMins')
            if isinstance(used,bool) or not isinstance(used,(int,float)) or not math.isfinite(used) or not 0<=used<=100:
                continue
            if not isinstance(minutes,int) or isinstance(minutes,bool) or minutes<=0:minutes=None
            reset=row.get('resetsAt');iso=None
            if isinstance(reset,(int,float)) and not isinstance(reset,bool) and math.isfinite(reset):
                try:iso=datetime.fromtimestamp(reset,timezone.utc).isoformat()
                except (ValueError,OverflowError,OSError):pass
            result.append({'id':hashlib.sha256(str(bucket_id).encode()).hexdigest()[:10]+'-'+slot,
                           'label':window_label(minutes),'usedPercent':used,'remainingPercent':100-used,
                           'resetsAt':iso,'windowMinutes':minutes})
    return result


def read_codex(timeout=40):
    proc=subprocess.Popen(['codex','app-server','--listen','stdio://'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        cwd=os.path.expanduser('~'),start_new_session=True,bufsize=0)
    selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
    buffer=bytearray();deadline=time.monotonic()+timeout;received=0
    def send(message):proc.stdin.write((json.dumps(message)+'\n').encode())
    def response(expected):
        nonlocal buffer,received
        while time.monotonic()<deadline:
            while b'\n' in buffer:
                line,_,rest=buffer.partition(b'\n');buffer=bytearray(rest)
                try:message=json.loads(line)
                except ValueError:continue
                if message.get('id')==expected:
                    if 'error' in message:
                        error=SourceError('source_error');error.rpc_code=message['error'].get('code');error.stage=expected
                        text=str(message['error'].get('message','')).lower()
                        error.flags=[word for word in ('auth','login','keyring','timeout','rate','network','refresh','not initialized','forbidden') if word in text]
                        raise error
                    return message.get('result',{})
            if not selector.select(max(0,deadline-time.monotonic())):break
            chunk=os.read(proc.stdout.fileno(),65536)
            if not chunk:raise SourceError('source_unavailable')
            received+=len(chunk)
            if received>2*1024*1024:raise SourceError('invalid_response')
            buffer.extend(chunk)
        raise SourceError('timeout')
    try:
        send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'runneos_usage','title':'RunneOS AI Usage','version':'0.2.0'},
             'capabilities':{'experimentalApi':True,'explicitGatewayOauth':True}}})
        response(1);send({'method':'initialized','params':{}})
        send({'id':2,'method':'account/read','params':{'refreshToken':False}})
        account=response(2).get('account')
        if not isinstance(account,dict):raise SourceError('auth_required')
        if account.get('type') not in ('chatgpt','chatgptAuthTokens'):raise SourceError('unsupported')
        # This identity is returned only to the in-memory collector; never cached/displayed.
        identity=account.get('email') or account.get('id')
        if not isinstance(identity,str) or not identity:raise SourceError('invalid_response')
        send({'id':3,'method':'account/rateLimits/read','params':{}})
        try:limits=parse_limits(response(3))
        except SourceError as error:
            error.identity=identity
            raise
        if not limits:raise SourceError('unsupported')
        return {'identity':identity,'quotas':limits}
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        proc.stdin.close();proc.stdout.close()
