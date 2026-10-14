"""Versioned AI metric contract. No network, credentials or Runne private state."""
from dataclasses import dataclass
from datetime import datetime
import math


STATUS = {'live', 'cached', 'stale', 'local_only', 'unsupported', 'auth_required',
          'rate_limited', 'error', 'loading'}
KINDS = {'quota_remaining', 'api_balance', 'key_budget', 'local_tokens', 'reported_cost'}


def time_value(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:raise ValueError('Metric timestamps need an explicit timezone')
    return result


@dataclass(frozen=True)
class Metric:
    provider: str
    product: str
    account: str
    id: str
    kind: str
    value: float | None
    unit: str
    scope: str
    source: str
    observed_at: str | None
    last_success: str | None
    status: str
    reset_at: str | None = None
    checked_at: str | None = None
    failure: str | None = None

    def __post_init__(self):
        if not all(isinstance(s, str) and s.strip() for s in
                   (self.provider, self.product, self.account, self.id, self.unit, self.scope, self.source)):
            raise ValueError('Metric identity, unit, scope and source are required')
        if self.status not in STATUS or self.kind not in KINDS:
            raise ValueError('Unsupported metric contract')
        if self.failure not in (None, 'error', 'rate_limited', 'auth_required'):
            raise ValueError('Failure uses a safe typed code, not raw exception text')
        if self.kind == 'quota_remaining' and self.unit != '%':raise ValueError('Wrong quota unit')
        if self.kind == 'local_tokens' and self.unit != 'tokens':raise ValueError('Wrong local-history unit')
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
                raise ValueError('Metric value must be finite or unknown')
            if self.kind == 'quota_remaining' and (self.unit != '%' or not 0 <= self.value <= 100):
                raise ValueError('Quota must be a remaining percentage in [0,100]')
            if self.kind == 'local_tokens' and (self.unit != 'tokens' or self.value < 0):
                raise ValueError('Local tokens must not be a monetary or quota estimate')
        for value in (self.observed_at, self.last_success, self.reset_at, self.checked_at):
            if value is not None:time_value(value)
        if self.status == 'live' and (self.value is None or self.observed_at is None or self.last_success is None):
            raise ValueError('Live metrics require a known value and successful observation')
        if self.status in {'cached', 'stale'} and (self.value is None or self.last_success is None):
            raise ValueError('Cached values require last-success metadata')
        if self.last_success and self.observed_at and time_value(self.last_success) > time_value(self.observed_at):
            raise ValueError('Last success cannot follow the observation')

    @property
    def key(self):
        return self.provider, self.product, self.account, self.id


def last_known_good(previous, new, now):
    """Failure retains old value + provenance, never calls it a new success."""
    if previous.key != new.key:raise ValueError('Cannot merge different account/metric identities')
    if (previous.kind, previous.unit, previous.scope) != (new.kind, new.unit, new.scope):
        raise ValueError('Cannot retain a value across a changed metric meaning')
    time_value(now)
    if new.status == 'live' or previous.value is None:return new
    if new.status not in {'error', 'rate_limited', 'auth_required'}:return new
    return Metric(previous.provider, previous.product, previous.account, previous.id,
                  previous.kind, previous.value, previous.unit, previous.scope, previous.source,
                  previous.observed_at, previous.last_success, 'stale', previous.reset_at,
                  now, new.status)


def snapshot(metrics):
    metrics = list(metrics)
    keys = [metric.key for metric in metrics]
    if len(set(keys)) != len(keys):raise ValueError('Duplicate account metric')
    # Explicit allowlist: extending an adapter cannot accidentally expose secrets.
    fields = ('provider', 'product', 'account', 'id', 'kind', 'value', 'unit', 'scope',
              'source', 'observed_at', 'last_success', 'status', 'reset_at', 'checked_at', 'failure')
    return {'schemaVersion': 2, 'metrics': [{key: getattr(m, key) for key in fields} for m in metrics]}
