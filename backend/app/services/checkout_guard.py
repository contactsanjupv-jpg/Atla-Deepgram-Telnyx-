"""
One open checkout per organization.

Why: a Paddle transaction exists (and can be paid) from the moment we create it.
If a customer opens checkout twice and pays both, Paddle creates two subscriptions
and charges twice. So while a checkout is pending we hand back the SAME checkout
URL for the same plan, and refuse to open a second one for a different plan.
The marker clears when the subscription webhook arrives, when services/checkout_recovery.py
sees Paddle say the transaction is dead, or after the TTL. It stores the Paddle transaction id so
the real state can be read back instead of guessing.
(services/duplicate_subscriptions.py is the second line of defence.)
"""
import json

import redis

from app.config import get_settings

_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
# Long on purpose: the marker is cleared only when Paddle confirms the transaction is dead (or a subscription
# exists), so a payable checkout is never forgotten. The TTL is just garbage collection for abandoned orgs.
PENDING_TTL_SECONDS = 86400


def _key(organization_id) -> str:
    return f"pending-checkout:{organization_id}"


def get_pending(organization_id) -> dict | None:
    raw = _redis.get(_key(organization_id))
    return json.loads(raw) if raw else None


def set_pending(organization_id, plan_id: str, checkout_url: str, transaction_id: str | None = None) -> None:
    payload = {"plan": plan_id, "checkout_url": checkout_url, "transaction_id": transaction_id}
    _redis.set(_key(organization_id), json.dumps(payload), ex=PENDING_TTL_SECONDS)


def pending_ttl_seconds(organization_id) -> int:
    return max(0, int(_redis.ttl(_key(organization_id))))


def clear_pending(organization_id) -> None:
    _redis.delete(_key(organization_id))
