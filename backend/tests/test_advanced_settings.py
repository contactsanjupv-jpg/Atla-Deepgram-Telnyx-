"""
Advanced Settings: full-config read, knowledge-preserving profile edit, honest call-handling
status. Real HTTP API + real providers; only the network boundary (RetellClient.request) is faked.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db.base import SessionLocal
from app.db.models.business import Business, BusinessStatus, KnowledgeItem, KnowledgeItemSource, KnowledgeItemType
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.db.models.voice_agent import Agent, AgentStatus
from app.main import app
from tests.test_agent_config import FakeRetell, _agent_row, _body, _new_org, _put, _seed_business, fake  # noqa: F401
from tests.test_tenant_isolation import _signup


def _cfg(c, org, biz):
    return c.get(f"/api/v1/orgs/{org}/agents/{biz}/config")


def _profile(c, org, biz):
    return c.get(f"/api/v1/orgs/{org}/businesses/{biz}/profile")


def _patch(c, org, biz, body):
    return c.patch(f"/api/v1/orgs/{org}/businesses/{biz}/profile", json=body)


def _knowledge(biz):
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeItem).filter(KnowledgeItem.business_id == uuid.UUID(biz)).all()
        return {str(r.id): (r.type.value, r.content, r.source.value) for r in rows}
    finally:
        db.close()


def _add_knowledge(biz, type_, content, source=KnowledgeItemSource.import_, title=None):
    db = SessionLocal()
    try:
        k = KnowledgeItem(business_id=uuid.UUID(biz), type=type_, title=title or content[:50], content=content, source=source)
        db.add(k)
        db.commit()
        return str(k.id)
    finally:
        db.close()


def _setup(fake):
    c, org = _new_org()
    biz, voice = _seed_business(org)
    assert _put(c, org, biz, _body(voice)).status_code == 200
    return c, org, biz, voice


# ---------------- reload: saved configuration comes back complete ----------------

def test_config_roundtrip_returns_everything_saved(fake):
    c, org, biz, voice = _setup(fake)
    body = _body(voice, personality="concise", tasks={"answer_questions": True, "capture_leads": False,
                                                       "take_messages": True, "transfer_calls": True},
                 transfer_number="+14155551234", language="en-GB")
    assert _put(c, org, biz, body).status_code == 200
    r = _cfg(c, org, biz)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["greeting"] == body["greeting"] and j["personality"] == "concise" and j["language"] == "en-GB"
    assert j["voice_id"] == voice and j["transfer_number"] == "+14155551234"
    assert j["tasks"] == {"answer_questions": True, "capture_leads": False, "take_messages": True, "transfer_calls": True}
    assert j["synced"] is True and j["version"] == j["synced_version"]
    assert "after_hours_behavior" not in j and "business_hours" not in j and "retell_agent_id" not in j


def test_config_404_when_no_receptionist_yet(fake):
    c, org = _new_org()
    biz, _voice = _seed_business(org)
    assert _cfg(c, org, biz).status_code == 404


def test_config_reports_unsynced_after_provider_failure(fake):
    c, org, biz, voice = _setup(fake)
    fake.fail[f"/update-retell-llm/{fake.ids['llm']}"] = 1
    resp = _put(c, org, biz, _body(voice, greeting="Changed"))
    assert resp.status_code == 502 and "saved" in resp.json()["detail"]
    j = _cfg(c, org, biz).json()
    assert j["greeting"] == "Changed"          # saved...
    assert j["synced"] is False                 # ...but honestly NOT applied
    assert j["version"] > j["synced_version"]


# ---------------- authorization / tenant isolation ----------------

def test_endpoints_require_auth_and_isolate_tenants(fake):
    c, org, biz, voice = _setup(fake)
    other, _other_org = _new_org()
    anon = TestClient(app)
    urls = [
        ("get", f"/api/v1/orgs/{org}/agents/{biz}/config", None),
        ("get", f"/api/v1/orgs/{org}/agents/{biz}/call-handling", None),
        ("get", f"/api/v1/orgs/{org}/businesses/{biz}/profile", None),
        ("patch", f"/api/v1/orgs/{org}/businesses/{biz}/profile", {"name": "X"}),
    ]
    for method, url, body in urls:
        assert getattr(anon, method)(url, **({"json": body} if body else {})).status_code == 401
        # another tenant's session addressing OUR org -> 404, and OUR business via THEIR org -> 404
        assert getattr(other, method)(url, **({"json": body} if body else {})).status_code == 404


def test_cannot_reach_other_tenants_business_through_own_org(fake):
    c, org, biz, voice = _setup(fake)
    other, other_org = _new_org()
    assert other.get(f"/api/v1/orgs/{other_org}/agents/{biz}/config").status_code == 404
    assert other.get(f"/api/v1/orgs/{other_org}/businesses/{biz}/profile").status_code == 404
    assert other.patch(f"/api/v1/orgs/{other_org}/businesses/{biz}/profile", json={"name": "Hacked"}).status_code == 404
    assert _profile(c, org, biz).json()["name"] == "Sunrise Dental"


def test_cannot_edit_or_delete_another_tenants_knowledge_item(fake):
    c, org, biz, voice = _setup(fake)
    other, other_org = _new_org()
    other_biz, _v = _seed_business(other_org)
    foreign = next(iter(_knowledge(other_biz)))
    for body in ({"name": "Sunrise Dental", "delete_ids": [foreign]},
                 {"name": "Sunrise Dental", "services": [{"id": foreign, "text": "pwned"}]}):
        assert _patch(c, org, biz, body).status_code == 404
    assert _knowledge(other_biz)[foreign][1] == "Costs $120 and takes 45 minutes."


def test_member_role_can_edit_like_existing_put(fake):
    """Existing permission model: member is the minimum for writes (same as PUT agents)."""
    c, org, biz, voice = _setup(fake)
    tag = uuid.uuid4().hex[:8]
    m, _o = _signup(f"mem-{tag}@adv-test.com", f"Member Home {tag}")
    db = SessionLocal()
    try:
        uid = db.query(User).filter(User.email == f"mem-{tag}@adv-test.com").one().id
        db.add(OrganizationMember(organization_id=uuid.UUID(org), user_id=uid, role=OrgRole.member))
        db.commit()
    finally:
        db.close()
    assert _cfg(m, org, biz).status_code == 200
    assert _patch(m, org, biz, {"name": "Sunrise Dental Care"}).status_code == 200
    assert _profile(c, org, biz).json()["name"] == "Sunrise Dental Care"


# ---------------- profile: save + reload ----------------

def test_profile_save_and_reload(fake):
    c, org, biz, voice = _setup(fake)
    body = {
        "name": "Sunrise Dental Care", "industry": "Dentistry", "phone": "+1 (555) 555-0199",
        "address": "99 Oak Ave", "website_url": "https://sunrise.example/", "description": "We care.",
        "hours": {"Mon-Fri": "8am-6pm"},
        "faqs": [{"question": "Do you take walk-ins?", "answer": "Yes, until 4pm."}],
        "policies": [{"text": "24h cancellation notice."}],
    }
    r = _patch(c, org, biz, body)
    assert r.status_code == 200, r.text
    got = _profile(c, org, biz).json()
    for k in ("name", "industry", "phone", "address", "website_url", "description", "hours"):
        assert got[k] == body[k]
    assert [f["question"] for f in got["faqs"]] == ["Do you take walk-ins?"]
    assert got["faqs"][0]["source"] == "manual"
    assert [p["text"] for p in got["policies"]] == ["24h cancellation notice."]
    assert [s["text"] for s in got["services"]] == ["Costs $120 and takes 45 minutes."]  # untouched


def test_unspecified_profile_fields_are_not_cleared(fake):
    c, org, biz, voice = _setup(fake)
    assert _patch(c, org, biz, {"name": "Only Name Changed"}).status_code == 200
    got = _profile(c, org, biz).json()
    assert got["industry"] == "Dental clinic" and got["address"] == "12 Main Street, Springfield"
    assert got["hours"] == {"Mon-Fri": "9am-5pm", "Sat": "closed"}


def test_explicit_null_clears_a_field(fake):
    c, org, biz, voice = _setup(fake)
    assert _patch(c, org, biz, {"name": "Sunrise Dental", "address": None}).status_code == 200
    assert _profile(c, org, biz).json()["address"] is None


# ---------------- knowledge preservation ----------------

def test_edit_never_deletes_unmentioned_knowledge_including_custom(fake):
    c, org, biz, voice = _setup(fake)
    faq = _add_knowledge(biz, KnowledgeItemType.faq, "Q: Parking?\nA: Free lot.")
    pol = _add_knowledge(biz, KnowledgeItemType.policy, "No refunds after 30 days.")
    cust = _add_knowledge(biz, KnowledgeItemType.custom, "Internal note for the receptionist.")
    before = _knowledge(biz)
    # a stale/partial client that only knows about one new FAQ:
    r = _patch(c, org, biz, {"name": "Sunrise Dental", "faqs": [{"question": "Open Sundays?", "answer": "No."}]})
    assert r.status_code == 200
    after = _knowledge(biz)
    for kid, val in before.items():
        assert after[kid] == val, "existing knowledge was altered by an unrelated edit"
    assert len(after) == len(before) + 1
    assert faq in after and pol in after and cust in after


def test_edit_in_place_keeps_id_and_marks_manual_so_reimport_wont_delete_it(fake):
    c, org, biz, voice = _setup(fake)
    faq = _add_knowledge(biz, KnowledgeItemType.faq, "Q: Parking?\nA: Free lot.")
    r = _patch(c, org, biz, {"name": "Sunrise Dental", "faqs": [{"id": faq, "question": "Parking?", "answer": "Free lot + street."}]})
    assert r.status_code == 200
    assert _knowledge(biz)[faq] == ("faq", "Q: Parking?\nA: Free lot + street.", "manual")


def test_delete_only_by_explicit_id_and_custom_is_protected(fake):
    c, org, biz, voice = _setup(fake)
    pol = _add_knowledge(biz, KnowledgeItemType.policy, "Old policy.")
    cust = _add_knowledge(biz, KnowledgeItemType.custom, "Keep me.")
    assert _patch(c, org, biz, {"name": "Sunrise Dental", "delete_ids": [cust]}).status_code == 400
    assert cust in _knowledge(biz)
    assert _patch(c, org, biz, {"name": "Sunrise Dental", "delete_ids": [pol]}).status_code == 200
    k = _knowledge(biz)
    assert pol not in k and cust in k


def test_type_mismatch_and_contradictory_requests_rejected_without_partial_writes(fake):
    c, org, biz, voice = _setup(fake)
    pol = _add_knowledge(biz, KnowledgeItemType.policy, "P")
    before = _knowledge(biz)
    assert _patch(c, org, biz, {"name": "Changed Name", "services": [{"id": pol, "text": "x"}]}).status_code == 400
    assert _patch(c, org, biz, {"name": "Changed Name", "policies": [{"id": pol, "text": "x"}], "delete_ids": [pol]}).status_code == 400
    assert _knowledge(biz) == before
    assert _profile(c, org, biz).json()["name"] == "Sunrise Dental"


def test_edit_does_not_change_business_status_or_receptionist_activation(fake):
    c, org, biz, voice = _setup(fake)
    db = SessionLocal()
    try:
        b = db.query(Business).filter(Business.id == uuid.UUID(biz)).one()
        b.status = BusinessStatus.active
        a = db.query(Agent).filter(Agent.business_id == b.id).one()
        a.status = AgentStatus.active
        db.commit()
    finally:
        db.close()
    assert _patch(c, org, biz, {"name": "Sunrise 2", "faqs": [{"question": "q", "answer": "a"}]}).status_code == 200
    db = SessionLocal()
    try:
        b = db.query(Business).filter(Business.id == uuid.UUID(biz)).one()
        a = db.query(Agent).filter(Agent.business_id == b.id).one()
        assert b.status == BusinessStatus.active and a.status == AgentStatus.active
    finally:
        db.close()


# ---------------- validation ----------------

@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 256},
    {"name": "A", "phone": "abc"}, {"name": "A", "phone": "12"},
    {"name": "A", "website_url": "javascript:alert(1)"}, {"name": "A", "website_url": "ftp://x.com"},
    {"name": "A", "website_url": "https://user:pw@x.com"},
    {"name": "A", "description": "d" * 4001}, {"name": "A", "industry": "i" * 129},
    {"name": "A", "hours": {"Mon": "x" * 101}}, {"name": "A", "hours": {"": "9-5"}},
    {"name": "A", "faqs": [{"question": "", "answer": "a"}]},
    {"name": "A", "faqs": [{"question": "q", "answer": "a" * 1501}]},
    {"name": "A", "policies": [{"text": " "}]},
    {"name": "A", "services": [{"text": "s"}] * 51},
    {"name": "A", "delete_ids": ["not-a-uuid"]},
])
def test_profile_validation_rejects_bad_input_and_saves_nothing(fake, body):
    c, org, biz, voice = _setup(fake)
    before = _knowledge(biz)
    r = _patch(c, org, biz, body)
    assert r.status_code == 422, r.text
    assert _knowledge(biz) == before
    assert _profile(c, org, biz).json()["name"] == "Sunrise Dental"


@pytest.mark.parametrize("override", [
    {"language": "english please"}, {"language": ""}, {"personality": "rude"},
    {"voice_id": str(uuid.uuid4())}, {"voice_id": "nope"},
    {"tasks": {"transfer_calls": True}, "transfer_number": None},
    {"tasks": {"transfer_calls": True}, "transfer_number": "555-1234"},
    {"name": ""}, {"greeting": "g" * 501},
])
def test_receptionist_validation_rejects_and_does_not_touch_saved_config(fake, override):
    c, org, biz, voice = _setup(fake)
    before = _cfg(c, org, biz).json()
    calls = len(fake.calls)
    body = _body(voice)
    body.update(override)
    resp = _put(c, org, biz, body)
    assert resp.status_code in (400, 422), resp.text
    assert _cfg(c, org, biz).json() == before
    assert len(fake.calls) == calls  # nothing sent to the provider


def test_unsupported_tasks_are_dropped_not_stored(fake):
    c, org, biz, voice = _setup(fake)
    _put(c, org, biz, _body(voice, tasks={"answer_questions": True, "book_appointments": True, "hack": True}))
    t = _cfg(c, org, biz).json()["tasks"]
    assert set(t) == {"answer_questions", "capture_leads", "take_messages", "transfer_calls"}


# ---------------- provider sync ----------------

def test_profile_edit_resyncs_prompt_to_provider(fake):
    c, org, biz, voice = _setup(fake)
    r = _patch(c, org, biz, {"name": "Sunrise Dental", "faqs": [{"question": "Parking?", "answer": "Free garage."}]})
    assert r.status_code == 200
    upd = fake.to(f"/update-retell-llm/{fake.ids['llm']}")[-1][2]
    assert "Free garage." in upd["general_prompt"]
    assert "Teeth cleaning" in upd["general_prompt"] or "Costs $120" in upd["general_prompt"]
    assert _cfg(c, org, biz).json()["synced"] is True


def test_profile_edit_provider_failure_is_saved_but_reported_not_applied(fake):
    c, org, biz, voice = _setup(fake)
    fake.fail[f"/update-retell-llm/{fake.ids['llm']}"] = 1
    r = _patch(c, org, biz, {"name": "Sunrise New", "policies": [{"text": "Be nice."}]})
    assert r.status_code == 502
    assert "saved" in r.json()["detail"].lower()
    assert _profile(c, org, biz).json()["name"] == "Sunrise New"       # persisted
    assert any(p["text"] == "Be nice." for p in _profile(c, org, biz).json()["policies"])
    assert _cfg(c, org, biz).json()["synced"] is False                  # honest
    # retry (provider recovered) applies it
    assert _put(c, org, biz, _body(voice)).status_code == 200
    assert _cfg(c, org, biz).json()["synced"] is True


def test_noop_profile_save_does_not_bump_version_or_call_provider(fake):
    c, org, biz, voice = _setup(fake)
    v = _cfg(c, org, biz).json()["version"]
    n = len(fake.calls)
    assert _patch(c, org, biz, {"name": "Sunrise Dental"}).status_code == 200
    assert _cfg(c, org, biz).json()["version"] == v and len(fake.calls) == n


def test_profile_edit_without_receptionist_makes_no_provider_calls(fake):
    c, org = _new_org()
    biz, _v = _seed_business(org)
    assert _patch(c, org, biz, {"name": "No Agent Yet"}).status_code == 200
    assert fake.calls == []


# ---------------- call handling: never claim live without provider confirmation ----------------

class _Phones:
    def __init__(self, inbound=None, fail=False, missing=False):
        self.inbound, self.fail, self.missing = inbound, fail, missing

    def get_number(self, _id):
        if self.fail:
            raise RuntimeError("provider down")
        return None if self.missing else SimpleNamespace(inbound_agent_id=self.inbound)


def _make_live_in_db(org, biz):
    db = SessionLocal()
    try:
        a = db.query(Agent).filter(Agent.business_id == uuid.UUID(biz)).one()
        a.status = AgentStatus.active
        p = PhoneNumber(organization_id=uuid.UUID(org), agent_id=a.id, retell_phone_number_id=f"pn_{uuid.uuid4().hex[:8]}",
                        number="+15555550123", status=PhoneNumberStatus.active)
        db.add(p)
        db.commit()
        return a.retell_agent_id
    finally:
        db.close()


def _ch(c, org, biz):
    r = c.get(f"/api/v1/orgs/{org}/agents/{biz}/call-handling")
    assert r.status_code == 200, r.text
    return r.json()


def test_call_handling_not_configured_and_not_live(fake):
    c, org = _new_org()
    biz, voice = _seed_business(org)
    j = _ch(c, org, biz)
    assert j["receptionist_state"] == "not_configured" and j["sync_state"] == "not_configured" and j["business_number"] is None
    _put(c, org, biz, _body(voice))
    j = _ch(c, org, biz)
    assert j["receptionist_state"] == "not_live" and j["sync_state"] == "synced"


def test_db_says_active_but_provider_does_not_confirm_is_never_live(fake, monkeypatch):
    c, org, biz, voice = _setup(fake)
    agent_id = _make_live_in_db(org, biz)
    for phones, expected in ((_Phones(inbound=agent_id), "live"), (_Phones(inbound="other"), "not_live"),
                             (_Phones(inbound=None), "not_live"), (_Phones(missing=True), "not_live"),
                             (_Phones(fail=True), "unverified")):
        monkeypatch.setattr("app.services.activation.RetellPhoneProvider", lambda p=phones: p)
        j = _ch(c, org, biz)
        assert j["receptionist_state"] == expected, (expected, j)
        assert j["business_number"] == "+15555550123"
        if expected != "live":
            assert "is live" not in j["receptionist_message"]


def test_live_but_unsynced_says_callers_hear_previous_version(fake, monkeypatch):
    c, org, biz, voice = _setup(fake)
    agent_id = _make_live_in_db(org, biz)
    monkeypatch.setattr("app.services.activation.RetellPhoneProvider", lambda: _Phones(inbound=agent_id))
    fake.fail[f"/update-retell-llm/{fake.ids['llm']}"] = 1
    assert _put(c, org, biz, _body(voice, greeting="Edited")).status_code == 502
    j = _ch(c, org, biz)
    assert j["sync_state"] == "pending"
    assert "haven't been applied" in j["receptionist_message"] and "is live and answering" not in j["receptionist_message"]


def test_saving_settings_never_activates_or_assigns_a_number(fake):
    c, org, biz, voice = _setup(fake)
    _patch(c, org, biz, {"name": "Sunrise Dental", "services": [{"text": "Whitening"}]})
    _put(c, org, biz, _body(voice, greeting="Hi"))
    assert _agent_row(biz).status == AgentStatus.draft
    db = SessionLocal()
    try:
        assert db.query(PhoneNumber).filter(PhoneNumber.organization_id == uuid.UUID(org)).count() == 0
    finally:
        db.close()


# ---------------- finding 1: no provider agent id => never live (None == None trap) ----------------

class _CountingPhones(_Phones):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.lookups = 0

    def get_number(self, _id):
        self.lookups += 1
        return super().get_number(_id)


def test_missing_provider_agent_id_is_never_live_even_if_provider_inbound_is_also_null(fake, monkeypatch):
    c, org = _new_org()
    biz, voice = _seed_business(org)
    fake.fail["/create-retell-llm"] = 1
    assert _put(c, org, biz, _body(voice)).status_code == 502          # agent saved, never created at provider
    assert _agent_row(biz).retell_agent_id is None
    _make_live_in_db(org, biz)                                          # DB claims active + number assigned
    for inbound in (None, "", "agent_something_else"):
        phones = _CountingPhones(inbound=inbound)
        monkeypatch.setattr("app.services.activation.RetellPhoneProvider", lambda p=phones: p)
        j = _ch(c, org, biz)
        assert j["receptionist_state"] == "not_live", (inbound, j)
        assert "live and answering" not in j["receptionist_message"]
        assert phones.lookups == 0   # decided without trusting/asking the provider


def test_provider_number_with_null_inbound_agent_is_not_live_even_with_agent_id(fake, monkeypatch):
    c, org, biz, voice = _setup(fake)
    _make_live_in_db(org, biz)
    for inbound in (None, ""):
        monkeypatch.setattr("app.services.activation.RetellPhoneProvider", lambda i=inbound: _Phones(inbound=i))
        assert _ch(c, org, biz)["receptionist_state"] == "not_live"


def test_verify_receptionist_live_unit_contract():
    from app.services.activation import verify_receptionist_live

    agent = SimpleNamespace(id=uuid.uuid4(), status=AgentStatus.active, retell_agent_id=None, version=1, synced_version=1)
    num = SimpleNamespace(agent_id=agent.id, retell_phone_number_id="pn")
    assert verify_receptionist_live(None, None, agent, num, provider=_Phones(inbound=None))[0] == "not_live"
    assert verify_receptionist_live(None, None, agent, num, provider=_Phones(inbound=""))[0] == "not_live"
    agent.retell_agent_id = "ag_1"
    assert verify_receptionist_live(None, None, agent, num, provider=_Phones(inbound="ag_1"))[0] == "live"


# ---------------- finding 4: one definition of "live" ----------------

def test_live_means_provider_routing_and_is_independent_of_sync(fake, monkeypatch):
    """live == provider routes the number to this agent. Sync state is reported separately."""
    c, org, biz, voice = _setup(fake)
    agent_id = _make_live_in_db(org, biz)
    monkeypatch.setattr("app.services.activation.RetellPhoneProvider", lambda: _Phones(inbound=agent_id))
    j = _ch(c, org, biz)
    assert (j["receptionist_state"], j["sync_state"]) == ("live", "synced")
    assert "is live and answering" in j["receptionist_message"]
    fake.fail[f"/update-retell-llm/{fake.ids['llm']}"] = 1
    assert _put(c, org, biz, _body(voice, greeting="Edit")).status_code == 502
    j = _ch(c, org, biz)
    assert (j["receptionist_state"], j["sync_state"]) == ("live", "pending")   # still routed, but flagged
    assert "previous version" in j["receptionist_message"]
    # and database state alone (no provider confirmation) is never live:
    monkeypatch.setattr("app.services.activation.RetellPhoneProvider", lambda: _Phones(fail=True))
    assert _ch(c, org, biz)["receptionist_state"] == "unverified"
