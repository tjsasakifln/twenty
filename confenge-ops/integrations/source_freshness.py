"""One definition of freshness for the read-only Extra source."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SAO_PAULO = ZoneInfo("America/Sao_Paulo")

def _next_business_slot(last_success: datetime) -> datetime:
    """First Mon–Fri 08:00…19:00 BRT slot strictly after ``last_success``."""
    local = last_success.astimezone(SAO_PAULO)
    for offset in range(9):
        day = (local + timedelta(days=offset)).date()
        if day.weekday() >= 5: continue
        for hour in range(8, 20):
            slot = datetime(day.year, day.month, day.day, hour, tzinfo=SAO_PAULO)
            if slot > local: return slot
    raise RuntimeError("no_future_business_slot")

def effective_source_state(status: dict, now: datetime | None = None) -> tuple[str, str]:
    """Return ``(FRESH|STALE, reason)`` using the business-hours grace window.

    A status file is trustworthy only when it explicitly says FRESH and carries
    a parseable, non-future ``last_success``. It expires two hours after the
    next Mon–Fri 08:00…19:00 BRT timer slot, including over a weekend.
    """
    if str(status.get("status") or status.get("state") or "").upper() != "FRESH":
        return "STALE", "reader_not_fresh"
    try:
        seen = datetime.fromisoformat(str(status["last_success"]).replace("Z", "+00:00"))
        if seen.tzinfo is None: raise ValueError("naive_timestamp")
    except (KeyError, TypeError, ValueError):
        return "STALE", "missing_or_invalid_last_success"
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - seen.astimezone(timezone.utc)).total_seconds()
    if age < 0: return "STALE", "future_last_success"
    # The installed timer spreads the top-of-hour call slightly; retain that
    # observed three-minute scheduling allowance before its two-hour grace.
    deadline = _next_business_slot(seen) + timedelta(hours=2, minutes=3)
    if current.astimezone(SAO_PAULO) > deadline: return "STALE", "next_business_slot_missed"
    return "FRESH", "recent_reader_success"
