"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog

FULL_REFUND_NOTICE_HOURS = 48
PARTIAL_REFUND_NOTICE_HOURS = 24
FULL_REFUND_PERCENT = 100
PARTIAL_REFUND_PERCENT = 50
NO_REFUND_PERCENT = 0


def calculate_refund_percent(notice: timedelta) -> int:
    """Map notice (start_time - cancellation_time) to a refund percentage."""
    if notice >= timedelta(hours=FULL_REFUND_NOTICE_HOURS):
        return FULL_REFUND_PERCENT
    if notice >= timedelta(hours=PARTIAL_REFUND_NOTICE_HOURS):
        return PARTIAL_REFUND_PERCENT
    return NO_REFUND_PERCENT


def calculate_refund_amount_cents(price_cents: int, percent: int) -> int:
    """Round to the nearest cent, with half-cents rounding up."""
    exact = price_cents * percent / 100.0
    return math.floor(exact + 0.5)


def log_refund(db: Session, booking: Booking, percent: int, amount_cents: int) -> RefundLog:
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
