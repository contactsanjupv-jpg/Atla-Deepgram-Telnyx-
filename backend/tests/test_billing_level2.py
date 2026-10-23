"""Level 2: the customer-facing subscription view the billing screen renders."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.services.entitlement import suspended_since
from tests.test_billing import SECRET, _new_org, _post
from tests.test_entitlements import _ev

VIEW_KEYS = {
    "plan_id", "plan_name", "status", "entitled", "features",
    "current_period_start", "current_period_end", "cancel_effective_at", "cancel_requested_at", "service_ends_at",
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")
    monkeypatch.setattr(s, "PAST_DUE_GRACE_DAYS", 3)


def _view(c, org):
    resp = c.get(f"/api/v1/orgs/{org}/billing/subscription")
    assert resp.status_code == 200
    return resp.json()


def test_no_subscription_returns_null():
    c, org = _new_org()
    assert _view(c, org) is None


@pytest.mark.parametrize("price,plan_id,name", [("pri_starter", "starter", "Starter"), ("pri_growth", "growth", "Growth")])
def test_active_plan_view_names_the_plan_and_lists_what_it_includes(price, plan_id, name):
    c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, price))
    view = _view(c, org)
    assert view["plan_id"] == plan_id and view["plan_name"] == name
    assert view["status"] == "active" and view["entitled"] is True
    assert {f["id"] for f in view["features"]} >= {"phone_number", "go_live"}
    assert all(f["label"] for f in view["features"])
    assert view["current_period_start"] and view["current_period_end"]
    assert view["service_ends_at"] is None


def test_view_exposes_no_provider_or_internal_ids():
    c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    view = _view(c, org)
    assert set(view) == VIEW_KEYS
    assert sid not in str(view) and "pri_" not in str(view) and org not in str(view)


def test_scheduled_cancellation_keeps_access_and_reports_the_end_date():
    c, org = _new_org()
    event = _ev("subscription.updated", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_starter")
    event["data"]["scheduled_change"] = {"action": "cancel", "effective_at": "2026-11-01T00:00:00Z"}
    _post(event)
    view = _view(c, org)
    assert view["entitled"] is True and view["status"] == "active"
    assert view["cancel_effective_at"].startswith("2026-11-01")


@pytest.mark.parametrize("status", ["paused", "canceled"])
def test_paused_or_canceled_is_not_entitled_and_lists_no_features(status):
    c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_growth", status=status))
    view = _view(c, org)
    assert view["entitled"] is False and view["features"] == [] and view["service_ends_at"] is None


def test_past_due_is_not_entitled_but_reports_when_service_stops(db):
    c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.updated", sid, org, "pri_starter", status="past_due", at="2026-10-05T00:00:00Z"))
    view = _view(c, org)
    assert view["entitled"] is False and view["features"] == []
    ends = datetime.fromisoformat(view["service_ends_at"])
    assert ends == datetime(2026, 10, 8, tzinfo=timezone.utc)

    # The date shown to the customer is the date enforcement really suspends service.
    db.rollback()
    org_id = uuid.UUID(org)
    assert suspended_since(db, org_id, ends - timedelta(seconds=1)) is None
    assert suspended_since(db, org_id, ends + timedelta(seconds=1)) is not None


def test_unrecognised_plan_is_never_entitled_and_has_no_name():
    c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_not_ours"))
    view = _view(c, org)
    assert view["plan_name"] is None and view["entitled"] is False and view["features"] == []


def test_another_organizations_subscription_is_not_readable():
    c_a, _a = _new_org()
    _c_b, org_b = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_b, "pri_growth"))
    assert c_a.get(f"/api/v1/orgs/{org_b}/billing/subscription").status_code == 404