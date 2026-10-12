"""
The promised free test call. A prospect gets exactly FREE_TEST_CALLS_PER_ORG of them, ever.

The allowance is a ROW, not a counter: `free_test_allowances` is unique on (organization, slot) and on
(user, slot). Claiming a slot is an INSERT that either succeeds or violates a constraint, so two
simultaneous requests can never both get it, a page refresh / second receptionist / new sign-in cannot
reset it, and the same person cannot collect one per organization. Nothing here trusts the browser.

Only the customer's access is free: the real Retell cost of the call is still recorded in usage_ledger
(kind "free_test") so it can be measured (python -m app.workers.pricing_report).

Known limit: the call's length is capped by the browser (TEST_CALL_MAX_SECONDS) and, server-side, by the
agent's AGENT_MAX_CALL_SECONDS — so the worst case for one free call is that many seconds of provider cost.
"""
import logging
import uuid

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.billing import FreeTestAllowance

logger = logging.getLogger("atla.free_test")


def reserve(db: Session, organization_id: uuid.UUID, user_id: uuid.UUID) -> FreeTestAllowance | None:
    """Claims the next free slot, or returns None when none is left for this org / user."""
    for slot in range(get_settings().FREE_TEST_CALLS_PER_ORG):
        row = FreeTestAllowance(organization_id=organization_id, user_id=user_id, slot=slot)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            return row
        except IntegrityError:
            continue
    return None


def release(db: Session, allowance: FreeTestAllowance) -> None:
    """Gives the slot back — only when the call provably never happened (provider refused it)."""
    db.delete(allowance)
    db.flush()


def release_if_never_connected(db: Session, retell_call_id: str) -> bool:
    """A free call that ended with zero seconds never reached the receptionist: the customer did not get to try it."""
    row = db.query(FreeTestAllowance).filter(FreeTestAllowance.retell_call_id == retell_call_id).first()
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def remaining(db: Session, organization_id: uuid.UUID, user_id: uuid.UUID | None) -> int:
    total = get_settings().FREE_TEST_CALLS_PER_ORG
    used_org = db.query(func.count(FreeTestAllowance.id)).filter(FreeTestAllowance.organization_id == organization_id).scalar()
    used_user = (
        db.query(func.count(FreeTestAllowance.id)).filter(FreeTestAllowance.user_id == user_id).scalar() if user_id else 0
    )
    return max(0, total - max(used_org, used_user))
