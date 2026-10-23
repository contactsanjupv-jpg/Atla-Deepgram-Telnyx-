from pydantic import BaseModel, Field, HttpUrl


class ImportWebsiteRequest(BaseModel):
    url: HttpUrl


class CreateManualBusinessRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ImportWebsiteResponse(BaseModel):
    business_id: str
    pages_fetched: list[str]
    structured_info: dict


class BusinessReviewOut(BaseModel):
    id: str
    name: str
    website_url: str | None
    industry: str | None
    address: str | None
    description: str | None
    hours: dict | None
    phone: str | None
    status: str
    raw_import_snapshot: dict | None

    model_config = {"from_attributes": True}


class BusinessApproveRequest(BaseModel):
    """
    The edited/confirmed version of everything the importer found —
    spec §7: the customer edits before this becomes authoritative.
    Nothing here is written to knowledge_items until this endpoint is
    called; up to that point it's all still a draft.
    """
    name: str
    industry: str | None = None
    address: str | None = None
    description: str | None = None
    hours: dict | None = None
    phone: str | None = None
    services: list[str] = []
    faqs: list[dict] = []
    policies: list[str] = []

import re
import uuid as _uuid
from urllib.parse import urlparse

from pydantic import field_validator

_PHONE_CHARS = re.compile(r"^[+\d\s().-]+$")


def _clean(v: str | None, maxlen: int) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if len(v) > maxlen:
        raise ValueError(f"must be at most {maxlen} characters")
    return v


class FaqIn(BaseModel):
    id: _uuid.UUID | None = None
    question: str = Field(min_length=1, max_length=255)
    answer: str = Field(min_length=1, max_length=1500)

    @field_validator("question", "answer")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class TextItemIn(BaseModel):
    id: _uuid.UUID | None = None
    text: str = Field(min_length=1, max_length=1000)

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class BusinessProfileUpdate(BaseModel):
    """
    Settings-page edit. Knowledge is UPSERT-ONLY: items listed here are created
    (no id) or updated (id); anything not mentioned is left exactly as it is.
    An item is removed only if its id is in `delete_ids`. `custom` knowledge is
    never touched by this endpoint.
    """
    name: str = Field(min_length=1, max_length=255)
    industry: str | None = None
    phone: str | None = None
    address: str | None = None
    website_url: str | None = None
    description: str | None = None
    hours: dict[str, str] | None = None
    services: list[TextItemIn] = Field(default_factory=list, max_length=50)
    faqs: list[FaqIn] = Field(default_factory=list, max_length=50)
    policies: list[TextItemIn] = Field(default_factory=list, max_length=30)
    delete_ids: list[_uuid.UUID] = Field(default_factory=list, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Business name is required")
        return v

    @field_validator("industry")
    @classmethod
    def _industry(cls, v):
        return _clean(v, 128)

    @field_validator("address")
    @classmethod
    def _address(cls, v):
        return _clean(v, 512)

    @field_validator("description")
    @classmethod
    def _description(cls, v):
        return _clean(v, 4000)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        v = _clean(v, 32)
        if v is None:
            return None
        if not _PHONE_CHARS.match(v) or sum(c.isdigit() for c in v) < 7:
            raise ValueError("Enter a valid phone number")
        return v

    @field_validator("website_url")
    @classmethod
    def _website(cls, v):
        v = _clean(v, 2048)
        if v is None:
            return None
        u = urlparse(v)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            raise ValueError("Website must be a full http(s) address like https://example.com")
        return v

    @field_validator("hours")
    @classmethod
    def _hours(cls, v):
        if v is None:
            return None
        if len(v) > 14:
            raise ValueError("Too many hours entries")
        out = {}
        for k, val in v.items():
            k, val = k.strip(), val.strip()
            if not k or not val or len(k) > 40 or len(val) > 100:
                raise ValueError("Each hours entry needs a label (max 40) and a value (max 100)")
            out[k] = val
        return out or None


class KnowledgeEntryOut(BaseModel):
    id: str
    source: str


class FaqOut(KnowledgeEntryOut):
    question: str
    answer: str


class TextItemOut(KnowledgeEntryOut):
    text: str


class BusinessProfileOut(BaseModel):
    id: str
    name: str
    industry: str | None
    phone: str | None
    address: str | None
    website_url: str | None
    description: str | None
    hours: dict | None
    status: str
    services: list[TextItemOut]
    faqs: list[FaqOut]
    policies: list[TextItemOut]
    # Knowledge this page does not edit (kept untouched), so the UI can say so.
    other_knowledge_count: int
