"""
Level 3: a lead is created from call_analyzed.custom_analysis_data when (and only when) the receptionist captured
one. The callback number the caller gave wins over the number they called from; bad values must never lose the call.
"""
import pytest

from app.db.base import SessionLocal
from app.db.models.calls import Call
from app.db.models.crm import Lead
from tests.test_inbound_call_resolution import (  # noqa: F401  (_retell_key is an autouse fixture)
    _call, _cid, _event, _number, _org, _phone, _post, _retell_key, _wh,
)

CALLER = "+14155550123"  # the default from_number used by _event


def _analyzed(label, custom, summary="Caller wants a quote.", org_number=None, **over):
    org, number, cid = org_number or (_org(label), _number(), _cid())
    _phone(org, number) if org_number is None else None
    analysis = {"call_summary": summary}
    if custom is not None:
        analysis["custom_analysis_data"] = custom
    _post(_event("call_analyzed", cid, number, org, call_status="ended", call_analysis=analysis, **over))
    return org, number, cid


def _leads(cid):
    db = SessionLocal()
    try:
        call = db.query(Call).filter(Call.retell_call_id == cid).first()
        return [] if call is None else db.query(Lead).filter(Lead.call_id == call.id).all()
    finally:
        db.close()


def test_captured_lead_uses_the_callback_number_the_caller_gave():
    org, _n, cid = _analyzed("cb", {"captured_lead": True, "caller_name": "Dana", "callback_number": "+15551230000", "reason": "Braces"})

    (lead,) = _leads(cid)
    assert (lead.name, lead.phone, lead.reason, lead.summary) == ("Dana", "+15551230000", "Braces", "Caller wants a quote.")
    assert lead.organization_id == org["org_id"] and lead.source == "call"


@pytest.mark.parametrize("missing", [None, "", "   ", "five five five one two three", "12", "1" * 40, {"x": 1}])
def test_missing_or_unusable_callback_falls_back_to_the_caller_number(missing):
    custom = {"captured_lead": True, "caller_name": "Dana"}
    if missing is not None:
        custom["callback_number"] = missing
    _o, _n, cid = _analyzed(f"fb{abs(hash(str(missing)))}", custom)

    (lead,) = _leads(cid)
    assert lead.phone == CALLER


def test_numeric_callback_value_is_accepted():
    _o, _n, cid = _analyzed("intcb", {"captured_lead": True, "callback_number": 5551234567})

    assert _leads(cid)[0].phone == "5551234567"


def test_no_number_anywhere_gives_a_lead_with_no_phone_not_an_error():
    org, cid = _org("web"), _cid()
    _post(_event("call_analyzed", cid, None, org, call_type="web_call", direction=None, from_number=None, to_number=None,
                 call_status="ended", call_analysis={"custom_analysis_data": {"captured_lead": True, "caller_name": "Tess"}}))

    (lead,) = _leads(cid)
    assert lead.phone is None and lead.source == "test_call"
    assert _wh(cid, "call_analyzed").status == "processed"


@pytest.mark.parametrize("value", [False, "false", "False", "no", "", None, 0, "0", {}, []])
def test_uncaptured_values_create_no_lead(value):
    _o, _n, cid = _analyzed(f"nc{abs(hash(str(value)))}", {"captured_lead": value, "caller_name": "X", "callback_number": "+15550001111"})

    assert _leads(cid) == []
    assert _call(cid) is not None  # the call itself is still recorded


@pytest.mark.parametrize("value", [True, "true", "TRUE", " yes ", 1])
def test_captured_values_create_a_lead(value):
    _o, _n, cid = _analyzed(f"c{abs(hash(str(value)))}", {"captured_lead": value, "caller_name": "X"})

    assert len(_leads(cid)) == 1


@pytest.mark.parametrize("analysis_custom", [None, "oops", ["captured_lead"], {}])
def test_missing_or_malformed_analysis_data_creates_no_lead(analysis_custom):
    _o, _n, cid = _analyzed(f"m{abs(hash(str(analysis_custom)))}", analysis_custom)

    assert _leads(cid) == []


def test_oversized_and_blank_text_never_loses_the_call():
    org, number, cid = _analyzed("long", {"captured_lead": True, "caller_name": "N" * 400, "reason": "  "})

    (lead,) = _leads(cid)
    assert len(lead.name) == 255 and lead.reason is None
    assert _wh(cid, "call_analyzed").status == "processed"
    assert _call(cid) is not None


def test_duplicate_analysis_events_create_one_lead():
    org, number, cid = _analyzed("dupe", {"captured_lead": True, "caller_name": "Dana"})
    payload = _event("call_analyzed", cid, number, org, call_status="ended",
                     call_analysis={"call_summary": "again", "custom_analysis_data": {"captured_lead": True, "caller_name": "Other"}})
    _post(payload)
    _post(payload)

    (lead,) = _leads(cid)
    assert lead.name == "Dana"  # the first capture stands


def test_lead_is_created_even_when_analysis_arrives_before_call_ended():
    org, number, cid = _analyzed("early", {"captured_lead": True, "caller_name": "Dana"})
    _post(_event("call_ended", cid, number, org, call_status="ended"))
    _post(_event("call_started", cid, number, org))

    assert len(_leads(cid)) == 1


def test_a_foreign_agents_analysis_event_cannot_attach_a_lead_to_another_tenants_call():
    owner, intruder = _org("owner"), _org("intruder")
    number, cid = _number(), _cid()
    _phone(owner, number)
    _post(_event("call_started", cid, number, owner))

    # An analysis event naming another tenant's agent is rejected (Level 1), so no lead can be created for it.
    _post(_event("call_analyzed", cid, number, intruder, call_status="ended",
                 call_analysis={"custom_analysis_data": {"captured_lead": True, "caller_name": "Eve"}}))

    assert _leads(cid) == []
    assert _wh(cid, "call_analyzed").status == "failed"
