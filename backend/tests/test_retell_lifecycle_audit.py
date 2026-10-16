"""
Retell lifecycle audit regressions: which organization a call is attributed to, and a worker that survives
a failing job. Payload field names follow the ones the integration already assumes (call_ingestion.py);
nothing here claims a live Retell payload was observed.
"""
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import UsageLedgerEntry
from app.db.models.calls import Call, CallDirection
from app.workers import run_worker
from tests.fakes import FakePhone
from tests.test_call_pipeline import KEY, _call, _count, _event_row, _new_org, _post, _seed


@pytest.fixture(autouse=True)
def _retell_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "RETELL_API_KEY", KEY)


def _cid():
    return f"call_{uuid.uuid4().hex[:10]}"


def _call_org(call_id):
    db = SessionLocal()
    try:
        row = db.query(Call).filter(Call.retell_call_id == call_id).one()
        return str(row.organization_id), row.direction
    finally:
        db.close()


def _usage_rows(org, call_id):
    return _count(UsageLedgerEntry, organization_id=uuid.UUID(org), retell_call_id=call_id)


# ---------------- attribution ----------------

def test_an_outbound_call_to_another_customers_number_belongs_to_the_org_that_placed_it():
    """Org B's agent dials a number that happens to be org A's Atla line. The CALLEE's number must not
    decide the tenant (it used to make the agent/phone orgs disagree, so the call and its usage were lost)."""
    _c1, org_a = _new_org()
    _c2, org_b = _new_org()
    _agent_a, number_a = _seed(org_a)
    agent_b, number_b = _seed(org_b)
    cid = _cid()
    _post("call_ended", _call(cid, agent_b, number_a, direction="outbound", from_number=number_b))
    assert _event_row(cid, "call_ended").status == "processed"
    assert _call_org(cid) == (org_b, CallDirection.outbound)
    assert _usage_rows(org_b, cid) == 1 and _usage_rows(org_a, cid) == 0


def test_an_inbound_call_from_another_customers_number_belongs_to_the_org_that_was_called():
    _c1, org_a = _new_org()
    _c2, org_b = _new_org()
    agent_a, number_a = _seed(org_a)
    _agent_b, number_b = _seed(org_b)
    cid = _cid()
    _post("call_ended", _call(cid, agent_a, number_a, from_number=number_b))
    assert _event_row(cid, "call_ended").status == "processed"
    assert _call_org(cid) == (org_a, CallDirection.inbound)
    assert _usage_rows(org_a, cid) == 1 and _usage_rows(org_b, cid) == 0


def test_a_callers_number_never_attributes_an_inbound_call_to_the_callers_own_org():
    """Unknown agent and a called number we don't know: the only match left is the CALLER's number, which
    belongs to some other customer. Attributing the call to them would bill the wrong tenant, so the event
    stays failed/retryable instead."""
    _c, org_b = _new_org()
    _agent_b, number_b = _seed(org_b)
    cid = _cid()
    _post("call_ended", _call(cid, f"agent_unknown_{uuid.uuid4().hex[:6]}", "+15550000001", from_number=number_b))
    assert _event_row(cid, "call_ended").status == "failed"
    assert _count(Call, retell_call_id=cid) == 0 and _usage_rows(org_b, cid) == 0


# ---------------- worker resilience ----------------

def test_a_failing_event_sweep_does_not_kill_the_worker_or_skip_the_other_jobs(monkeypatch):
    ran = []

    def boom(*a, **k):
        raise RuntimeError("database restarting")

    monkeypatch.setattr(run_worker, "retry_due_events", boom)
    monkeypatch.setattr(run_worker, "reconcile_provisioning", lambda db, provider: ran.append("provisioning") or 0)
    monkeypatch.setattr(run_worker, "enforce_entitlements", lambda db, provider: ran.append("entitlements") or {})
    monkeypatch.setattr(run_worker, "reconcile_topups", lambda db: ran.append("topups") or 0)

    summary = run_worker.run_once(phone_provider=FakePhone())  # must not raise

    assert summary["events"] == "error"
    assert ran == ["provisioning", "entitlements", "topups"]
