import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.db.base import get_db
from app.db.models.business import Business, BusinessStatus, KnowledgeItem, KnowledgeItemSource, KnowledgeItemType
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.schemas.business import (
    BusinessProfileOut,
    BusinessProfileUpdate,
    FaqOut,
    TextItemOut,
    BusinessApproveRequest,
    BusinessReviewOut,
    CreateManualBusinessRequest,
    ImportWebsiteRequest,
    ImportWebsiteResponse,
)
from app.db.models.voice_agent import Agent
from app.services.website_import_service import WebsiteImportError, import_website

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/businesses", tags=["businesses"])
settings = get_settings()


@router.post("/import-website", response_model=ImportWebsiteResponse, status_code=status.HTTP_201_CREATED)
def import_website_endpoint(
    payload: ImportWebsiteRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    try:
        check_rate_limit(
            f"website-import:{membership.organization_id}",
            settings.WEBSITE_IMPORTS_PER_DAY_PER_ORG,
            86400,
        )
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Daily website import limit reached") from e

    try:
        result = import_website(str(payload.url))
    except WebsiteImportError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    info = result.structured_info
    business = Business(
        organization_id=membership.organization_id,
        name=info.get("business_name") or "New business",
        website_url=str(payload.url),
        industry=info.get("industry"),
        address=info.get("address"),
        description=info.get("description"),
        hours=info.get("hours"),
        phone=info.get("phone"),
        status=BusinessStatus.draft,
        raw_import_snapshot={**result.raw_snapshot, "extracted": info},
    )
    db.add(business)
    db.commit()
    db.refresh(business)

    return ImportWebsiteResponse(
        business_id=str(business.id),
        pages_fetched=result.pages_fetched,
        structured_info=info,
    )


@router.post("/create-manual", response_model=BusinessReviewOut, status_code=status.HTTP_201_CREATED)
def create_manual_business(
    payload: CreateManualBusinessRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    """
    The "I don't have a website" path — skips the importer entirely and
    creates a business record the customer fills in by hand on the same
    review/approve screen the import flow uses. No rate limit needed here
    (unlike import-website): this is a single local DB insert, not an
    outbound fetch or an LLM call, so there's no external cost or SSRF
    surface to protect against.
    """
    business = Business(
        organization_id=membership.organization_id,
        name=payload.name.strip() or "New business",
        website_url=None,
        status=BusinessStatus.draft,
        raw_import_snapshot=None,
    )
    db.add(business)
    db.commit()
    db.refresh(business)
    return BusinessReviewOut(**{**business.__dict__, "id": str(business.id), "status": business.status.value})


@router.get("/{business_id}", response_model=BusinessReviewOut)
def get_business_for_review(
    business_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return BusinessReviewOut(**{**business.__dict__, "id": str(business.id), "status": business.status.value})


@router.post("/{business_id}/approve", response_model=BusinessReviewOut)
def approve_business(
    business_id: uuid.UUID,
    payload: BusinessApproveRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    """
    Spec §7: "The customer must approve/edit the information before it
    becomes authoritative business knowledge." This is that moment —
    everything the customer confirmed (edited or not) is written into
    `businesses` + `knowledge_items` here; nothing from the raw import is
    trusted as knowledge before this call.
    """
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    business.name = payload.name
    business.industry = payload.industry
    business.address = payload.address
    business.description = payload.description
    business.hours = payload.hours
    business.phone = payload.phone
    business.status = BusinessStatus.reviewed
    business.approved_at = datetime.now(timezone.utc)

    db.query(KnowledgeItem).filter(
        KnowledgeItem.business_id == business.id, KnowledgeItem.source == KnowledgeItemSource.import_
    ).delete()

    for service in payload.services:
        db.add(
            KnowledgeItem(
                business_id=business.id,
                type=KnowledgeItemType.service,
                title=service[:255],
                content=service,
                source=KnowledgeItemSource.import_,
            )
        )
    for faq in payload.faqs:
        q, a = faq.get("question", ""), faq.get("answer", "")
        db.add(
            KnowledgeItem(
                business_id=business.id,
                type=KnowledgeItemType.faq,
                title=q[:255] or "FAQ",
                content=f"Q: {q}\nA: {a}",
                source=KnowledgeItemSource.import_,
            )
        )
    for policy in payload.policies:
        db.add(
            KnowledgeItem(
                business_id=business.id,
                type=KnowledgeItemType.policy,
                title=policy[:255],
                content=policy,
                source=KnowledgeItemSource.import_,
            )
        )

    db.commit()
    db.refresh(business)
    return BusinessReviewOut(**{**business.__dict__, "id": str(business.id), "status": business.status.value})


# ---------------------------------------------------------------------------
# Settings-page profile editing (knowledge-preserving)
# ---------------------------------------------------------------------------

_PROFILE_FIELDS = ("name", "industry", "phone", "address", "website_url", "description", "hours")


def _parse_faq(item: KnowledgeItem) -> tuple[str, str]:
    c = item.content or ""
    if c.startswith("Q: ") and "\nA: " in c:
        q, a = c[3:].split("\nA: ", 1)
        return q, a
    return item.title, c  # unrecognised shape: show it rather than hide it


def _profile_out(db: Session, business: Business) -> BusinessProfileOut:
    items = (
        db.query(KnowledgeItem)
        .filter(KnowledgeItem.business_id == business.id)
        .order_by(KnowledgeItem.created_at, KnowledgeItem.id)
        .all()
    )
    services, faqs, policies, other = [], [], [], 0
    for i in items:
        if i.type == KnowledgeItemType.service:
            services.append(TextItemOut(id=str(i.id), source=i.source.value, text=i.content))
        elif i.type == KnowledgeItemType.policy:
            policies.append(TextItemOut(id=str(i.id), source=i.source.value, text=i.content))
        elif i.type == KnowledgeItemType.faq:
            q, a = _parse_faq(i)
            faqs.append(FaqOut(id=str(i.id), source=i.source.value, question=q, answer=a))
        else:
            other += 1
    return BusinessProfileOut(
        id=str(business.id),
        name=business.name,
        industry=business.industry,
        phone=business.phone,
        address=business.address,
        website_url=business.website_url,
        description=business.description,
        hours=business.hours,
        status=business.status.value,
        services=services,
        faqs=faqs,
        policies=policies,
        other_knowledge_count=other,
    )


def _tenant_business(db: Session, membership: OrganizationMember, business_id: uuid.UUID) -> Business:
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return business


@router.get("/{business_id}/profile", response_model=BusinessProfileOut)
def get_business_profile(
    business_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    return _profile_out(db, _tenant_business(db, membership, business_id))


@router.patch("/{business_id}/profile", response_model=BusinessProfileOut)
def update_business_profile(
    business_id: uuid.UUID,
    payload: BusinessProfileUpdate,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    """
    Upsert-only knowledge edit: nothing is removed unless its id is in `delete_ids`,
    `custom` knowledge is never touched, status/approval are not changed, and the
    receptionist is re-synced so callers hear the edit (502/409 = saved but not applied).
    """
    from app.api.v1.agents import sync_or_http_error  # local: agents imports nothing from here

    business = _tenant_business(db, membership, business_id)

    existing = {i.id: i for i in db.query(KnowledgeItem).filter(KnowledgeItem.business_id == business.id).all()}

    delete_ids = set(payload.delete_ids)
    submitted = [(KnowledgeItemType.service, x) for x in payload.services] + \
                [(KnowledgeItemType.faq, x) for x in payload.faqs] + \
                [(KnowledgeItemType.policy, x) for x in payload.policies]
    seen: set[uuid.UUID] = set()
    for _t, x in submitted:
        if x.id is not None:
            if x.id in seen:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Duplicate knowledge item in request")
            seen.add(x.id)
    if seen & delete_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An item can't be both edited and deleted")

    for kid in seen | delete_ids:
        item = existing.get(kid)
        if item is None:  # other tenant's or nonexistent: indistinguishable on purpose
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge item not found")
        if item.type == KnowledgeItemType.custom:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This item can't be edited here")
    for t, x in submitted:
        if x.id is not None and existing[x.id].type != t:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Knowledge item type mismatch")

    changed = False
    for f in _PROFILE_FIELDS:
        if f in payload.model_fields_set and getattr(business, f) != getattr(payload, f):
            setattr(business, f, getattr(payload, f))
            changed = True

    def _fields(t, x):
        if t == KnowledgeItemType.faq:
            return x.question[:255], f"Q: {x.question}\nA: {x.answer}"
        return x.text[:255], x.text

    for t, x in submitted:
        title, content = _fields(t, x)
        if x.id is None:
            db.add(KnowledgeItem(business_id=business.id, type=t, title=title, content=content,
                                 source=KnowledgeItemSource.manual))
            changed = True
        else:
            item = existing[x.id]
            if item.content != content or item.title != title:
                item.title, item.content = title, content
                item.source = KnowledgeItemSource.manual  # a human edit must survive a re-import
                changed = True
    for kid in delete_ids:
        db.delete(existing[kid])
        changed = True

    agent = None
    if changed:
        agent = (
            db.query(Agent)
            .filter(Agent.business_id == business.id, Agent.organization_id == membership.organization_id)
            .first()
        )
        if agent is not None:
            agent.version += 1  # the prompt is built from this data: provider copy is now stale
    db.commit()

    if agent is not None and agent.retell_agent_id is not None and agent.voice is not None:
        sync_or_http_error(db, agent, business, agent.voice)

    db.refresh(business)
    return _profile_out(db, business)
