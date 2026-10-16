"""
Platform-admin-only refunds. There is intentionally no customer-facing refund route: cancelling is a separate
action (billing.py /cancel) and never refunds.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import require_platform_admin
from app.db.base import get_db
from app.db.models.tenancy import User
from app.providers.billing.paddle_billing_provider import PaddleAPIError, PaddleBillingProvider
from app.services.refunds import RefundRejected, request_refund

logger = logging.getLogger("atla.admin_refunds")
router = APIRouter(prefix="/api/v1/admin/refunds", tags=["admin-refunds"])


class RefundIn(BaseModel):
    transaction_id: str = Field(min_length=3, max_length=64)
    basis: str
    reason: str = Field(min_length=1, max_length=1000)


class RefundOut(BaseModel):
    id: str
    transaction_id: str
    status: str  # requested | submitted | approved | rejected | failed
    provider_status: str | None


@router.post("", response_model=RefundOut, status_code=status.HTTP_202_ACCEPTED)
def create_refund(body: RefundIn, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    try:
        row = request_refund(db, admin.id, body.transaction_id.strip(), body.basis, body.reason, PaddleBillingProvider())
    except RefundRejected as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except (PaddleAPIError, RuntimeError) as e:
        logger.error("Refund request failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Paddle did not accept the refund request.") from e
    return RefundOut(id=str(row.id), transaction_id=row.transaction_id, status=row.status, provider_status=row.provider_status)
