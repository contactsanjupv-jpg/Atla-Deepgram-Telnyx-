import re

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

StrId = Annotated[str, BeforeValidator(str)]

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,4})?$")
# The only behaviours the product genuinely supports. Anything else the
# client sends (e.g. the old "book_appointments") is dropped, not stored.
SUPPORTED_TASKS = ("answer_questions", "capture_leads", "take_messages", "transfer_calls")
PERSONALITIES = ("friendly", "professional", "concise")


class VoiceOut(BaseModel):
    id: StrId
    retell_voice_id: str
    name: str
    provider: str
    gender: str | None
    accent: str | None
    age_style: str | None
    preview_url: str | None

    model_config = {"from_attributes": True}


class PhoneNumberOut(BaseModel):
    id: StrId
    number: str
    area_code: str | None
    country: str
    status: str

    model_config = {"from_attributes": True}


class PurchaseNumberRequest(BaseModel):
    country: str = "US"
    area_code: str | None = None


class AgentConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    greeting: str | None = Field(default=None, max_length=500)
    personality: str = "professional"
    language: str = "en-US"
    voice_id: str  # our internal Voice.id (uuid)
    tasks: dict
    transfer_number: str | None = None
    business_hours: dict | None = None  # reserved; not customer-configurable yet
    after_hours_behavior: str | None = None  # reserved; not customer-configurable yet

    @field_validator("language")
    @classmethod
    def _language_shape(cls, v: str) -> str:
        v = v.strip()
        if not _LANG.match(v):
            raise ValueError("language must look like en-US")
        return v

    @field_validator("personality")
    @classmethod
    def _personality_known(cls, v: str) -> str:
        v = v.lower()
        if v not in PERSONALITIES:
            raise ValueError(f"personality must be one of {', '.join(PERSONALITIES)}")
        return v

    @field_validator("tasks")
    @classmethod
    def _tasks_supported_only(cls, v: dict) -> dict:
        return {key: bool(v.get(key, False)) for key in SUPPORTED_TASKS}

    @field_validator("transfer_number")
    @classmethod
    def _transfer_e164(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = re.sub(r"[\s().-]", "", v.strip())
        if not _E164.match(v):
            raise ValueError("Transfer number must be a full phone number like +14155551234")
        return v

    @model_validator(mode="after")
    def _transfer_needs_number(self):
        if self.tasks.get("transfer_calls") and not self.transfer_number:
            raise ValueError("Enter a transfer number, or turn off call transfer")
        if not self.tasks.get("transfer_calls"):
            self.transfer_number = None
        return self


class AgentOut(BaseModel):
    id: str
    name: str
    status: str
    retell_agent_id: str | None
    # True only when the voice provider has received the latest saved config.
    synced: bool = False

    model_config = {"from_attributes": True}



class AgentConfigOut(BaseModel):
    """The complete saved receptionist configuration, as the customer last saved it.
    Reserved/unsupported settings (business hours, after-hours behaviour) are deliberately absent."""
    id: str
    name: str
    greeting: str | None
    personality: str
    language: str
    voice_id: str | None
    tasks: dict
    transfer_number: str | None
    status: str
    version: int
    synced_version: int
    synced: bool
    supported_tasks: list[str]
    personalities: list[str]


class CallHandlingOut(BaseModel):
    business_number: str | None
    number_state: str  # ready | provisioning | blocked | none
    number_message: str
    # not_configured | not_live | live | unverified — "live" ONLY when the provider confirmed it just now.
    receptionist_state: str
    receptionist_message: str
    # not_configured | never_synced | pending | synced
    sync_state: str
    version: int | None
    synced_version: int | None
