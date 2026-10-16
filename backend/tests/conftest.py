import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import Base, SessionLocal, engine


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """
    Uses the same Postgres instance as the app (DATABASE_URL from .env)
    — these are real integration tests against a real database, not
    sqlite-in-memory approximations. Tables are created once per test
    session and torn down after.

    SAFETY GUARDRAIL: this fixture DROPS every table at teardown. Running
    it against a real dev/prod database is a real, recoverable-only-by-
    rerunning-migrations mistake — it happened twice already. Refuse to
    even start unless the database name makes it obvious this is a
    throwaway test database, rather than trusting every future `pytest`
    invocation to remember a special env var.
    """
    db_url = get_settings().DATABASE_URL
    db_name = db_url.rsplit("/", 1)[-1]
    if "test" not in db_name.lower():
        pytest.exit(
            f"\n\nREFUSING TO RUN: DATABASE_URL points at '{db_name}', which doesn't look like a "
            f"test database (expected something with 'test' in the name, e.g. 'atla_test').\n"
            f"This test suite DROPS ALL TABLES at the end of the run — running it against your "
            f"real dev database will wipe it.\n\n"
            f"Fix: createdb -O atla atla_test   (if you haven't already)\n"
            f"Then run tests as:\n"
            f'  DATABASE_URL="postgresql+psycopg://atla:atla@localhost:5432/atla_test" python -m pytest tests/ -v\n',
            returncode=1,
        )
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from app.config import get_settings
    import redis as redis_lib

    settings = get_settings()
    r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
    for key in r.scan_iter("ratelimit:*"):
        r.delete(key)
    yield
    
class RecordingBillingProvider:
    """Stands in for PaddleBillingProvider inside the duplicate-subscription guard so tests never
    reach the real Paddle API. `cancelled` records (subscription_id, immediately) requests."""

    cancelled: list = []
    fail: bool = False

    def cancel_subscription(self, external_subscription_id, immediately=False):
        if type(self).fail:
            raise RuntimeError("paddle down")
        type(self).cancelled.append((external_subscription_id, immediately))


@pytest.fixture(autouse=True)
def _guard_never_calls_real_paddle(monkeypatch):
    from app.services import duplicate_subscriptions

    RecordingBillingProvider.cancelled = []
    RecordingBillingProvider.fail = False
    monkeypatch.setattr(duplicate_subscriptions, "PaddleBillingProvider", RecordingBillingProvider)
    yield RecordingBillingProvider

@pytest.fixture(autouse=True)
def _auto_provisioning_off_by_default(monkeypatch):
    """Existing tests must never reach the real Retell. test_auto_provisioning.py switches it on with a fake."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AUTO_PROVISION_NUMBERS", False)
    yield
