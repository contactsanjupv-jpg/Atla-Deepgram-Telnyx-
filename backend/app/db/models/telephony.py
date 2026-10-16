import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class PhoneNumberStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    released = "released"


class PhoneNumber(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "phone_numbers"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    retell_phone_number_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # E.164
    area_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    country: Mapped[str] = mapped_column(String(4), default="US", nullable=False)
    monthly_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[PhoneNumberStatus] = mapped_column(
        Enum(PhoneNumberStatus, name="phone_number_status"), default=PhoneNumberStatus.active, nullable=False
    )



class PhoneProvisioning(Base, UUIDPKMixin, TimestampMixin):
    """
    Durable record of "this org is getting a number", written BEFORE we call the
    provider. One row per org (unique) => at most one number per org. The
    deterministic `nickname` is stored on the provider resource, so an ambiguous
    purchase can be reconciled by asking the provider whether it exists.
    status: requested | purchasing | ambiguous | purchased | failed | blocked_payment | released
    """

    __tablename__ = "phone_provisionings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    nickname: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="requested", nullable=False)
    country: Mapped[str] = mapped_column(String(4), default="US", nullable=False)
    area_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    phone_number_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phone_numbers.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
