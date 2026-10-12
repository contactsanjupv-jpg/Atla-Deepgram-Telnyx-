"""
Growth differentiation enforced on the SERVER: full transcripts, transcript search, analytics.
A Starter customer calling the API directly gets 402 and never receives the data.
"""
import uuid
from datetime import timedelta

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection, CallTranscript
from app.db.models.crm import Lead
from app.services import transcripts
from app.services.plans import FEATURE_LABELS, GROWTH_ONLY_FEATURES, PLANS, STARTER_FEATURES, Feature
from tests.billing_helpers import add_usage, new_org, now, subscribe

SEGMENTS = [
    {"role": "agent", "content": "Thanks for calling Sunrise Dental.", "words": [{"word": "Thanks", "start": 0.4, "end": 0.7}, {"word": "Dental", "start": 2.0, "end": 2.6}]},
    {"role": "user", "content": "Hi, do you do teeth whitening? I'm 100% sure I need it.", "words": [{"word": "Hi", "start": 3.1, "end": 3.3}, {"word": "it", "start": 7.0, "end": 7.4}]},
    {"role": "agent", "content": "Yes, whitening is available.", "words": []},
]


def _call_with_transcript(org, segments=SEGMENTS, text="Agent: ..."):
    db = SessionLocal()
    try:
        call = Call(organization_id=uuid.UUID(org), retell_call_id=f"call_{uuid.uuid4().hex[:12]}", direction=CallDirection.inbound,
                    status="ended", started_at=now(), caller_number="+15550001111", summary="Asked about whitening.")
        db.add(call)
        db.flush()
        db.add(CallTranscript(call_id=call.id, transcript={"text": text, "segments": segments}))
        db.commit()
        return str(call.id)
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _prices(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


# ---------------- the catalog ----------------

def test_growth_has_everything_starter_has_and_starter_has_nothing_growth_only():
    assert PLANS["starter"].features == STARTER_FEATURES
    assert PLANS["growth"].features == STARTER_FEATURES | GROWTH_ONLY_FEATURES
    assert not (PLANS["starter"].features & GROWTH_ONLY_FEATURES)
    assert {Feature.transcripts, Feature.transcript_search, Feature.analytics} <= GROWTH_ONLY_FEATURES
    assert all(f in FEATURE_LABELS for f in Feature)


def test_growth_includes_substantially_more_credits_than_starter():
    assert PLANS["growth"].included_minutes >= 2 * PLANS["starter"].included_minutes


def test_every_advertised_growth_feature_is_gated_by_a_route():
    """A feature listed in the catalog must not be shippable-by-accident: its route depends on require_feature."""
    from app.main import app

    gated = set()
    for route in app.routes:
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []):
            call = dep.call
            cells = getattr(call, "__closure__", None) or []
            for cell in cells:
                if isinstance(cell.cell_contents, Feature):
                    gated.add(cell.cell_contents)
    assert GROWTH_ONLY_FEATURES <= gated, GROWTH_ONLY_FEATURES - gated


# ---------------- transcripts ----------------

def test_starter_cannot_read_a_transcript_even_by_calling_the_api_directly_but_still_gets_the_summary():
    c, org = new_org("gf")
    subscribe(org, "starter")
    call_id = _call_with_transcript(org)
    resp = c.get(f"/api/v1/orgs/{org}/calls/{call_id}/transcript")
    assert resp.status_code == 402 and "Growth" in resp.json()["detail"]
    assert "whitening" not in resp.text and "Sunrise" not in resp.text  # none of the conversation leaks in the error
    summary = c.get(f"/api/v1/orgs/{org}/calls/{call_id}")
    assert summary.status_code == 200 and summary.json()["summary"] == "Asked about whitening."
    assert "transcript" not in summary.json()


def test_a_customer_with_no_subscription_gets_no_transcript():
    c, org = new_org("gf")
    call_id = _call_with_transcript(org)
    assert c.get(f"/api/v1/orgs/{org}/calls/{call_id}/transcript").status_code == 402


def test_a_lapsed_growth_customer_loses_transcripts_until_billing_is_fixed():
    c, org = new_org("gf")
    subscribe(org, "growth", status="past_due")
    call_id = _call_with_transcript(org)
    assert c.get(f"/api/v1/orgs/{org}/calls/{call_id}/transcript").status_code == 402


def test_growth_gets_the_full_conversation_with_speaker_labels_and_timestamps():
    c, org = new_org("gf")
    subscribe(org, "growth")
    call_id = _call_with_transcript(org)
    data = c.get(f"/api/v1/orgs/{org}/calls/{call_id}/transcript").json()
    segs = data["segments"]
    assert [s["speaker"] for s in segs] == ["Receptionist", "Caller", "Receptionist"]
    assert segs[0]["start_seconds"] == 0.4 and segs[0]["end_seconds"] == 2.6
    assert segs[1]["start_seconds"] == 3.1 and "teeth whitening" in segs[1]["text"]
    assert segs[2]["start_seconds"] is None and segs[2]["end_seconds"] is None  # not provided -> null, never invented


def test_transcripts_are_private_to_their_organization_even_for_growth():
    a, org_a = new_org("gf")
    b, org_b = new_org("gf")
    subscribe(org_a, "growth")
    subscribe(org_b, "growth")
    call_id = _call_with_transcript(org_a)
    assert b.get(f"/api/v1/orgs/{org_a}/calls/{call_id}/transcript").status_code == 404  # wrong org path
    assert b.get(f"/api/v1/orgs/{org_b}/calls/{call_id}/transcript").status_code == 404  # right org, someone else's call


def test_malformed_stored_transcripts_never_break_the_response():
    assert transcripts.shape_segments(None) == []
    assert transcripts.shape_segments({"segments": "nope"}) == []
    shaped = transcripts.shape_segments({"segments": [None, 5, {"content": 3}, {"role": "user", "content": "ok", "words": [None, {"start": "x"}]}]})
    assert len(shaped) == 1 and shaped[0]["start_seconds"] is None


# ---------------- search ----------------

def test_starter_cannot_search_conversations():
    c, org = new_org("gf")
    subscribe(org, "starter")
    _call_with_transcript(org)
    resp = c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "whitening"})
    assert resp.status_code == 402 and "whitening" not in resp.text.replace("q=whitening", "")


def test_growth_finds_what_callers_said_with_the_matching_line_and_who_said_it():
    c, org = new_org("gf")
    subscribe(org, "growth")
    hit = _call_with_transcript(org)
    _call_with_transcript(org, segments=[{"role": "user", "content": "What are your opening hours?"}])
    out = c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "WHITENING"}).json()  # case-insensitive
    assert [h["call_id"] for h in out] == [hit]
    assert out[0]["match_count"] == 2 and out[0]["matches"][0]["speaker"] in ("Caller", "Receptionist")
    assert any("teeth whitening" in m["snippet"] for m in out[0]["matches"])
    assert c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "zzzznotthere"}).json() == []


def test_search_never_returns_another_organizations_conversations():
    a, org_a = new_org("gf")
    b, org_b = new_org("gf")
    subscribe(org_a, "growth")
    subscribe(org_b, "growth")
    _call_with_transcript(org_a)
    assert b.get(f"/api/v1/orgs/{org_b}/calls/search", params={"q": "whitening"}).json() == []
    assert b.get(f"/api/v1/orgs/{org_a}/calls/search", params={"q": "whitening"}).status_code == 404


def test_search_treats_percent_and_underscore_literally_and_rejects_too_short_queries():
    c, org = new_org("gf")
    subscribe(org, "growth")
    _call_with_transcript(org)
    assert len(c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "100%"}).json()) == 1
    assert c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "%%"}).json() == []  # not a wildcard
    assert c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "_"}).status_code == 422
    assert c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "a"}).status_code == 422
    assert c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "x" * 300}).status_code == 422


def test_search_route_does_not_shadow_the_call_detail_route():
    c, org = new_org("gf")
    subscribe(org, "growth")
    call_id = _call_with_transcript(org)
    assert c.get(f"/api/v1/orgs/{org}/calls/{call_id}").status_code == 200


# ---------------- analytics ----------------

def test_starter_cannot_use_analytics():
    c, org = new_org("gf")
    subscribe(org, "starter")
    assert c.get(f"/api/v1/orgs/{org}/analytics/overview").status_code == 402


def test_growth_analytics_are_real_counts_with_true_zeros_and_no_invented_metrics():
    c, org = new_org("gf")
    subscribe(org, "growth")
    add_usage(org, 120, at=now() - timedelta(hours=1))
    add_usage(org, 60, at=now() - timedelta(hours=2))
    add_usage(org, 500, kind="free_test")  # not customer activity
    db = SessionLocal()
    db.add(Lead(organization_id=uuid.UUID(org), name="Maria", phone="+15551110000"))
    db.commit()
    db.close()
    data = c.get(f"/api/v1/orgs/{org}/analytics/overview", params={"days": 7, "tz": "UTC"}).json()
    assert data["days"] == 7 and len(data["series"]) == 7
    assert data["total_calls"] == 2 and data["total_credit_seconds"] == 180 and data["total_leads"] == 1
    assert data["leads_awaiting_follow_up"] == 1 and data["leads_by_status"] == {"new": 1}
    assert sum(1 for d in data["series"] if d["calls"] == 0) >= 5
    assert not any(k in data for k in ("revenue", "roi", "conversion_rate", "savings"))
    assert c.get(f"/api/v1/orgs/{org}/analytics/overview", params={"days": 400}).status_code == 422


def test_analytics_only_count_the_callers_own_organization():
    a, org_a = new_org("gf")
    b, org_b = new_org("gf")
    subscribe(org_a, "growth")
    subscribe(org_b, "growth")
    add_usage(org_a, 100)
    assert b.get(f"/api/v1/orgs/{org_b}/analytics/overview").json()["total_calls"] == 0
    assert b.get(f"/api/v1/orgs/{org_a}/analytics/overview").status_code == 404


# ---------------- the public catalog the pricing page renders ----------------

def test_public_plan_catalog_exposes_the_real_allowances_and_the_difference_between_plans():
    from fastapi.testclient import TestClient

    from app.main import app

    plans = {p["id"]: p for p in TestClient(app).get("/api/v1/plans").json()}
    assert plans["starter"]["included_minutes"] == PLANS["starter"].included_minutes
    assert plans["growth"]["included_minutes"] == PLANS["growth"].included_minutes > plans["starter"]["included_minutes"]
    starter_ids = {f["id"] for f in plans["starter"]["features"]}
    growth_ids = {f["id"] for f in plans["growth"]["features"]}
    assert starter_ids < growth_ids and {"transcripts", "transcript_search", "analytics"} <= growth_ids - starter_ids
    assert "$20" in plans["starter"]["when_credits_run_out"] and plans["starter"]["baseline"]
    assert "cost" not in str(plans).lower() and "margin" not in str(plans).lower()  # internal economics never exposed
