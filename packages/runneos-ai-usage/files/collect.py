#!/usr/bin/env python3
"""Read-only provider usage collector. Only normalized metrics leave this process."""
import argparse
import concurrent.futures
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

PROVIDERS = {
    "codex": ("Codex", "https://chatgpt.com/codex/settings/usage"),
    "claude": ("Claude", "https://claude.ai/settings/usage"),
    "zai": ("Z.ai", "https://z.ai/manage-apikey/subscription"),
    "xai": ("x.ai", "https://console.x.ai/"),
    "kimi": ("Kimi", "https://platform.moonshot.ai/console/account"),
    "deepseek": ("DeepSeek", "https://platform.deepseek.com/usage"),
    "qwen": ("Qwen", "https://bailian.console.aliyun.com/"),
    "opencode-go": ("OpenCode Go", "https://opencode.ai/console"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/settings/credits"),
}
ALIASES = {
    "openai": "codex", "anthropic": "claude", "zai-coding-plan": "zai",
    "z-ai": "zai", "z.ai": "zai", "zhipuai": "zai", "x.ai": "xai",
    "moonshotai": "kimi", "moonshot": "kimi", "kimi-for-coding": "kimi",
    "kimi-coding": "kimi", "alibaba": "qwen", "dashscope": "qwen",
    "alibaba-coding-plan": "qwen", "alibaba-cn": "qwen", "zhipuai-coding-plan": "zai",
}
ENV_KEYS = {
    "zai": "ZAI_API_KEY", "xai": "XAI_API_KEY", "kimi": "MOONSHOT_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY", "qwen": "DASHSCOPE_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY", "openrouter": "OPENROUTER_API_KEY",
}
HOSTS = {"api.deepseek.com", "openrouter.ai", "api.z.ai", "open.bigmodel.cn",
         "api.moonshot.ai", "api.moonshot.cn", "api.kimi.com", "api.kimi.ai",
         "management-api.x.ai"}


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def quota(label, used, remaining=None, reset=None):
    percent = number(used)
    if percent is None:
        return None
    return {"label": str(label)[:80], "usedPercent": max(0, min(100, percent)),
            "remaining": number(remaining), "resetsAt": reset if isinstance(reset, str) else None}


def parse_remote(provider, payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid response")
    data = payload.get("data", payload)
    if not isinstance(data, dict) or payload.get("success") is False or payload.get("Success") is False:
        raise ValueError("Provider returned an error")
    result = {"balances": [], "quotas": [], "metrics": []}
    def balance(amount, currency):
        value = number(amount)
        if value is not None and currency in {"USD", "CNY", "JPY", "EUR"}:
            result["balances"].append({"amount": value, "currency": currency})
    if provider == "deepseek":
        for item in data.get("balance_infos", []):
            balance(item.get("total_balance"), item.get("currency"))
        result["scope"] = "Баланс API DeepSeek"
    elif provider == "openrouter":
        limit, remaining = number(data.get("limit")), number(data.get("limit_remaining"))
        if limit is not None and limit > 0 and remaining is not None:
            result["quotas"].append(quota("Бюджет API-ключа", 100*(limit-remaining)/limit, remaining))
        cost = number(data.get("usage_monthly"))
        if cost is not None:
            result["metrics"].append({"label": "Расход ключа за месяц", "value": cost, "unit": "USD"})
        result["scope"] = "API-ключ OpenRouter; бюджет ключа не равен балансу аккаунта"
    elif provider == "zai":
        for item in data.get("limits", []):
            label = {"TOKENS_LIMIT": "Окно 5 часов", "TIME_LIMIT": "MCP за месяц"}.get(item.get("type"), "Лимит плана")
            q = quota(label, item.get("percentage"), reset=item.get("resetAt"))
            if q:
                result["quotas"].append(q)
        result["scope"] = "Z.ai Coding Plan"
    elif provider == "kimi":
        balance(data.get("available_balance"), data.get("currency", "USD"))
        result["scope"] = "Баланс Moonshot API; отдельный от Kimi Code"
    elif provider == "kimi-coding":
        details = [data.get("usage", {})] + data.get("limits", [])
        for item in details:
            detail = item.get("detail", item)
            limit, remaining = number(detail.get("limit")), number(detail.get("remaining"))
            if remaining is None and limit is not None and number(detail.get("used")) is not None:
                remaining = limit-number(detail["used"])
            if limit and remaining is not None:
                reset = next((detail[k] for k in ["reset_at", "resetAt", "reset_time", "resetTime"] if detail.get(k)), None)
                label = detail.get("name", detail.get("title", "Лимит Kimi Code"))
                result["quotas"].append(quota(label, 100*(limit-remaining)/limit, remaining, reset))
        result["scope"] = "Kimi Code"
    elif provider == "xai":
        total = data.get("total", {})
        cents = number(total.get("val", total.get("value")))
        balance(cents/100 if cents is not None else None, "USD")
        result["scope"] = "Предоплаченный баланс команды x.ai"
    elif provider == "qwen":
        cloud = payload.get("Data", {})
        balance(cloud.get("AvailableCashAmount"), cloud.get("Currency"))
        result["scope"] = "Баланс аккаунта Alibaba Cloud; не отдельный лимит Qwen"
    else:
        raise ValueError("Unknown provider")
    return result


def parse_local(payload):
    """Group by billed provider, never infer it from the model vendor."""
    result = {}
    models = payload.get("models", []) if isinstance(payload, dict) else []
    if isinstance(models, dict):
        models = [dict(v, model=k) for k,v in models.items() if isinstance(v, dict)]
    for row in models:
        if not isinstance(row, dict):
            continue
        provider = row.get("providerID", row.get("providerId", row.get("provider", "")))
        if isinstance(provider, dict):
            provider = provider.get("id", "")
        model = row.get("modelID", row.get("model", row.get("id", "")))
        if isinstance(model, dict):
            provider = provider or model.get("providerID", "")
            model = model.get("id", "")
        if not provider and isinstance(model, str) and "/" in model:
            provider = model.split("/", 1)[0]
        provider = ALIASES.get(provider, provider)
        if provider not in PROVIDERS:
            continue
        totals = result.setdefault(provider, {"tokens": 0, "cost": 0, "requests": 0, "source": "Локальный OpenCode", "scope": "Только этот компьютер, выбранный период"})
        tokens = row.get("tokens", {})
        if isinstance(tokens, dict):
            cache = tokens.get("cache", {})
            total = sum(number(tokens.get(k)) or 0 for k in ["input", "output", "reasoning"])
            total += sum(number(cache.get(k)) or 0 for k in ["read", "write"]) if isinstance(cache, dict) else 0
        else:
            total = number(tokens) or 0
        totals["tokens"] += int(total)
        cost = number(row.get("cost"))
        totals["cost"] = totals["cost"] + cost if totals["cost"] is not None and cost is not None else None
        totals["requests"] += int(number(row.get("requests", row.get("steps"))) or 0)
    return result


def read_opencode_auth(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    result = {}
    for key, item in data.items():
        provider = ALIASES.get(key, key)
        if provider in PROVIDERS and isinstance(item, dict) and item.get("type") == "api":
            value = item.get("key")
            if isinstance(value, str) and value:
                result[provider] = value
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(url, token, *, raw_auth=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.port not in {None, 443} or parsed.username or parsed.password:
        raise ValueError("Unapproved provider endpoint")
    headers = {"Authorization": token if raw_auth else "Bearer "+token, "Accept": "application/json", "User-Agent": "RunneOS-AI-Usage/1.0"}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        content = response.read(1024*1024+1)
        if len(content) > 1024*1024:
            raise ValueError("Response too large")
        return json.loads(content)


def command_json(args, timeout=25):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("Collector unavailable")
    if len(result.stdout) > 8*1024*1024:
        raise ValueError("Response too large")
    return json.loads(result.stdout)


def remote_data(provider, cfg, keys):
    token = keys.get(provider, "")
    if provider == "deepseek":
        return http_json("https://api.deepseek.com/user/balance", token)
    if provider == "openrouter":
        return http_json("https://openrouter.ai/api/v1/key", token)
    if provider == "zai":
        host = "open.bigmodel.cn" if cfg.get("region") == "china" else "api.z.ai"
        return http_json("https://"+host+"/api/monitor/usage/quota/limit", token, raw_auth=True)
    if provider == "kimi":
        if cfg.get("mode") == "coding":
            host = "api.kimi.ai" if cfg.get("region") == "international" else "api.kimi.com"
            return http_json("https://"+host+"/coding/v1/usages", token)
        host = "api.moonshot.cn" if cfg.get("region") == "china" else "api.moonshot.ai"
        return http_json("https://"+host+"/v1/users/me/balance", token)
    if provider == "xai":
        management = cfg.get("managementKey", "")
        team = cfg.get("teamId", "")
        if not management or not team:
            raise ValueError("Management key and team ID required")
        team = urllib.parse.quote(str(team), safe="")
        return http_json("https://management-api.x.ai/v1/billing/teams/"+team+"/prepaid/balance", management)
    if provider == "qwen":
        return command_json(["aliyun", "bssopenapi", "QueryAccountBalance", "--profile", cfg["aliyunProfile"]])
    raise ValueError("No billing API")


def collect_snapshot(config, keys, *, upstream=None, local=None, remote=True, fetch=remote_data):
    rows = {key: {"id": key, "name": name, "consoleUrl": url, "status": "not_connected", "balance": None, "balances": [], "quotas": [], "metrics": [], "local": None, "scope": "", "message": "Не подключён"} for key,(name,url) in PROVIDERS.items()}
    local_usage = parse_local(local or {})
    for key, totals in local_usage.items():
        rows[key].update(local=totals, status="local_only", message="Локальный расход; лимиты аккаунта недоступны")
    for p in (upstream or {}).get("providers", []):
        key = p.get("id")
        if key not in {"codex", "claude"}:
            continue
        row = rows[key]
        totals = p.get("period", {})
        if p.get("available") or totals.get("total"):
            row["local"] = {"tokens": int(number(totals.get("total")) or 0), "cost": None, "requests": int(number(totals.get("requests")) or 0), "source": key+" CLI", "scope": "Локальная история CLI; выбранный период"}
            row.update(status="local_only", message="Локальная история CLI")
        buckets = p.get("quotaBuckets", [])
        if not buckets:
            buckets = [{"label": label, "allowance": p.get(field, {})} for field,label in [("sessionLeft", "5 часов"), ("weeklyLeft", "Неделя")]]
        for bucket in buckets:
            a = bucket.get("allowance", {})
            if a.get("known"):
                q = quota(bucket.get("label", "Лимит"), a.get("percentUsed"), a.get("remaining"), a.get("resetAt"))
                if q:
                    row["quotas"].append(q)
        if row["quotas"]:
            row.update(status="ok", scope="Лимиты аккаунта", message="Данные провайдера")
    def collect_one(key):
        row = rows[key]
        cfg = config.get(key, {})
        if cfg.get("_credentialError"):
            row.update(status="error", message="Проверьте файл ключа и права доступа (0600)")
            return
        if key in {"codex", "claude"}:
            return
        connected = bool(keys.get(key)) or (key == "qwen" and cfg.get("aliyunProfile")) or (key == "xai" and cfg.get("managementKey"))
        if not connected:
            return
        row.update(status="connected", message="Подключён; данные биллинга недоступны")
        if not remote or key in {"codex", "claude", "opencode-go"}:
            if row["local"]:
                row.update(status="local_only", message="Локальный расход; лимиты — в кабинете")
            return
        if key == "xai" and (not cfg.get("managementKey") or not cfg.get("teamId")):
            row["message"] = "Для баланса нужны Management Key и Team ID"
            return
        if key == "qwen" and not cfg.get("aliyunProfile"):
            row["message"] = "Баланс — через профиль Alibaba Cloud CLI"
            return
        try:
            adapter = "kimi-coding" if key == "kimi" and cfg.get("mode") == "coding" else key
            payload = fetch(key, cfg, keys)
            if key == "kimi" and adapter == "kimi" and cfg.get("region") == "china" and isinstance(payload, dict):
                payload = dict(payload)
                payload["data"] = dict(payload.get("data", payload), currency="CNY")
            parsed = parse_remote(adapter, payload)
            row.update(parsed)
            if parsed["balances"] or parsed["quotas"] or parsed["metrics"]:
                row.update(status="ok", message="Данные провайдера")
            else:
                row.update(status="error", message="Провайдер не вернул показатели")
        except urllib.error.HTTPError as error:
            row.update(status="error", message="HTTP "+str(error.code)+"; проверьте подключение")
        except Exception:
            row.update(status="error", message="Не удалось обновить; проверьте подключение")
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        list(pool.map(collect_one, rows))
    return {"version": 1, "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "periodDays": config.get("periodDays", 7), "providers": list(rows.values())}


def load_config(path):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Configuration must be an object")
        return data
    except FileNotFoundError:
        return {}


def credentials(config):
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share")))
    keys = read_opencode_auth(data_home/"opencode/auth.json")
    dbpath = data_home/"opencode/opencode.db"
    if dbpath.exists():
        try:
            with sqlite3.connect(dbpath.as_uri()+"?mode=ro", uri=True, timeout=1) as db:
                for integration, value in db.execute("select integration_id,value from credential where active=1"):
                    provider = ALIASES.get(integration, integration)
                    item = json.loads(value)
                    if provider in PROVIDERS and item.get("type") == "api":
                        token = item.get("key", item.get("apiKey"))
                        if isinstance(token, str) and token:
                            keys[provider] = token
                            if integration in {"kimi-for-coding", "kimi-coding"}:
                                config.setdefault("kimi", {}).setdefault("mode", "coding")
        except (sqlite3.Error, ValueError, TypeError):
            pass
    for provider, env in ENV_KEYS.items():
        if os.environ.get(env):
            keys[provider] = os.environ[env]
    if os.environ.get("KIMI_API_KEY"):
        keys["kimi"] = os.environ["KIMI_API_KEY"]
        config.setdefault("kimi", {}).setdefault("mode", "coding")
    for provider in PROVIDERS:
        cfg = config.setdefault(provider, {})
        for field in ["apiKeyFile", "managementKeyFile"]:
            if cfg.get(field):
                try:
                    path = Path(cfg[field]).expanduser()
                    if path.stat().st_mode & 0o077:
                        raise ValueError("Credential file must be private")
                    token = path.read_text().strip()
                    if not token:
                        raise ValueError("Empty credential")
                    if field == "apiKeyFile":
                        keys[provider] = token
                    else:
                        cfg["managementKey"] = token
                except (OSError, ValueError, TypeError):
                    cfg["_credentialError"] = True
    if os.environ.get("XAI_MANAGEMENT_API_KEY"):
        config["xai"]["managementKey"] = os.environ["XAI_MANAGEMENT_API_KEY"]
    return keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home()/".config")))/"runneos/ai-usage.json")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--period-days", type=int)
    args = parser.parse_args()
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home()/".local/state")))/"runneos/ai-usage"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = state/"snapshot.json"
    try:
        config = load_config(args.config)
        config["periodDays"] = max(1, min(90, args.period_days or int(config.get("periodDays", 7))))
        interval = max(180, int(config.get("refreshInterval", 300)))
        if not args.force and not args.offline and cache.exists() and time.time()-cache.stat().st_mtime < interval:
            saved = json.loads(cache.read_text())
            if saved.get("periodDays") == config["periodDays"]:
                print(json.dumps(saved, ensure_ascii=False)); return
        with (state/"collect.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not args.force and not args.offline and cache.exists() and time.time()-cache.stat().st_mtime < interval:
                saved = json.loads(cache.read_text())
                if saved.get("periodDays") == config["periodDays"]:
                    print(json.dumps(saved, ensure_ascii=False)); return
            keys = credentials(config)
            def optional(args):
                try:
                    return command_json(args, timeout=40)
                except Exception:
                    return {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                upstream = pool.submit(optional, ["dankaiusage", "summary", "--period-days", str(config["periodDays"])]) if not args.offline and shutil.which("dankaiusage") else None
                local = pool.submit(optional, ["opencode", "stats", "--json", "--days", str(config["periodDays"]), "--models", "--cost"]) if shutil.which("opencode") else None
                result = collect_snapshot(config, keys, upstream=upstream.result() if upstream else {}, local=local.result() if local else {}, remote=not args.offline)
            if not args.offline:
                temp = cache.with_suffix(".tmp")
                temp.write_text(json.dumps(result, ensure_ascii=False));temp.chmod(0o600);os.replace(temp, cache)
            print(json.dumps(result, ensure_ascii=False))
    except Exception:
        # Never expose credential paths, response bodies, or exceptions containing keys.
        print(json.dumps({"version": 1, "error": "Проверьте конфигурацию и права файлов подключения", "providers": []}, ensure_ascii=False))


if __name__ == "__main__":
    main()
