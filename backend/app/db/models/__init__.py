"""
Import every model module here so Alembic's autogenerate (and Base.metadata
in general) sees the full schema from a single import of app.db.models.
"""
from app.db.base import Base  # noqa: F401
from app.db.models.tenancy import User, Organization, OrganizationMember, OrgRole  # noqa: F401
from app.db.models.business import Business, KnowledgeItem, BusinessStatus  # noqa: F401
from app.db.models.voice_agent import Voice, Agent, AgentStatus  # noqa: F401
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus, PhoneProvisioning  # noqa: F401
from app.db.models.calls import Call, CallTranscript, CallDirection  # noqa: F401
from app.db.models.crm import Lead, LeadStatus, Appointment, AppointmentStatus  # noqa: F401
from app.db.models.integrations import Integration, IntegrationProvider, IntegrationStatus  # noqa: F401
from app.db.models.billing import (  # noqa: F401
    CreditLedgerEntry,
    FreeTestAllowance,
    Subscription,
    TopupPurchase,
    UsageLedgerEntry,
)
from app.db.models.platform import WebhookEvent, WebhookSource, AuditLog, AdminUser  # noqa: F401
