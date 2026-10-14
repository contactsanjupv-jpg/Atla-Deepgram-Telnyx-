"""
A refund / chargeback adjustment from Paddle changes a top-up exactly once, however many times it is delivered.
Regression: apply_adjustment() used to add the refund to refunded_minor BEFORE checking whether the adjustment had
already been processed, so every replay inflated refunded_minor and skewed the next legitimate refund's clawback.
"""
import threading
import uuid

from app.db.base import SessionLocal
from app.db.models.billing import TopupPurchase
from tests.billing_helpers import new_org
from tests.test_topups import (  # noqa: F401  (fixtures are used by name)
    _adjust, _adjustment, _balance, _complete, _entries, _paddle, _purchase, paddle,
)


def _row(topup_id):
    db = SessionLocal()
    try:
        r = db.get(TopupPurchase, uuid.UUID(topup_id))
        return r.refunded_minor, r.clawed_back_seconds, r.status
    finally:
        db.close()


def _paid_topup(amount=4000):
    _c, org = new_org("ri")
    topup_id, txn, seconds = _purchase(org, amount)
    _complete(org, topup_id, txn, amount=amount)
    return org, topup_id, txn, seconds


def test_replaying_the_same_partial_refund_changes_nothing_after_the_first_time(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    adj = _adjustment(txn, subtotal=1000)
    assert _adjust(adj) is True
    after_first = (_row(topup_id), _balance(org))
    assert after_first == ((1000, seconds * 1000 // 4000, "partially_refunded"), seconds - seconds * 1000 // 4000)
    for _ in range(5):
        assert _adjust(adj) is False
    assert (_row(topup_id), _balance(org)) == after_first
    assert len(_entries(org, "refund")) == 1


def test_replaying_a_fully_processed_refund_changes_nothing(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    adj = _adjustment(txn, subtotal=4000)
    assert _adjust(adj) is True
    assert _row(topup_id) == (4000, seconds, "refunded") and _balance(org) == 0
    assert _adjust(adj) is False and _adjust(adj) is False
    assert _row(topup_id) == (4000, seconds, "refunded") and _balance(org) == 0
    assert len(_entries(org, "refund")) == 1


def test_a_replay_does_not_skew_the_next_legitimate_refund(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    first = _adjustment(txn, subtotal=1000)
    _adjust(first)
    _adjust(first)  # replay: before the fix this pushed refunded_minor to 2000
    assert _adjust(_adjustment(txn, subtotal=1000)) is True  # a genuinely different adjustment
    assert _row(topup_id) == (2000, seconds * 2000 // 4000, "partially_refunded")
    assert _balance(org) == seconds - seconds * 2000 // 4000
    assert len(_entries(org, "refund")) == 2


def test_distinct_adjustments_with_the_same_amount_are_all_processed(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    for _ in range(4):
        _adjust(_adjustment(txn, subtotal=1000))
    assert _row(topup_id) == (4000, seconds, "refunded") and _balance(org) == 0
    assert len(_entries(org, "refund")) == 4


def test_a_refund_that_removes_no_credits_is_still_counted_once(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    _adjust(_adjustment(txn, action="chargeback"))  # every credit already clawed back
    late_refund = _adjustment(txn, subtotal=1000)
    assert _adjust(late_refund) is False  # nothing left to remove
    assert _row(topup_id)[0] == 1000
    assert _adjust(late_refund) is False and _adjust(late_refund) is False
    assert _row(topup_id)[0] == 1000 and _balance(org) == 0  # not 3000, and no credits invented


def test_a_non_positive_refund_amount_is_ignored(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    _adjust(_adjustment(txn, subtotal=1000))
    before = (_row(topup_id), _balance(org))
    assert _adjust(_adjustment(txn, subtotal=0)) is False
    assert _adjust(_adjustment(txn, subtotal=-500)) is False
    assert (_row(topup_id), _balance(org)) == before


def test_concurrent_duplicate_deliveries_of_one_refund_apply_it_once(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    adj = _adjustment(txn, subtotal=1000)
    results, gate = [], threading.Barrier(8)

    def go():
        gate.wait()
        results.append(_adjust(adj))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1
    assert _row(topup_id) == (1000, seconds * 1000 // 4000, "partially_refunded")
    assert _balance(org) == seconds - seconds * 1000 // 4000
    assert len(_entries(org, "refund")) == 1


def test_concurrent_distinct_refunds_are_each_applied_exactly_once(paddle):
    org, topup_id, txn, seconds = _paid_topup(4000)
    adjs = [_adjustment(txn, subtotal=1000) for _ in range(4)]
    gate = threading.Barrier(8)

    def go(a):
        gate.wait()
        _adjust(a)

    threads = [threading.Thread(target=go, args=(adjs[i % 4],)) for i in range(8)]  # each delivered twice, concurrently
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert _row(topup_id) == (4000, seconds, "refunded") and _balance(org) == 0
    assert len(_entries(org, "refund")) == 4