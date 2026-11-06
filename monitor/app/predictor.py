"""Growth analysis and capacity-exhaustion prediction.

Method: ordinary least-squares linear regression of used bytes against time over a
sliding window of recent StorageUsageRecords (default 7 days). The slope is the
growth rate; extrapolating the line to the capacity limit gives the time remaining.
"""
from datetime import timedelta

from sqlalchemy import select

from .config import settings
from .db import PredictionRecord, StorageUsageRecord

METHOD = "linear_regression"


def linear_regression(points):
    """points: [(x, y)] -> (slope, intercept, r_squared)."""
    n = len(points)
    if n < 2:
        return 0.0, (points[0][1] if points else 0.0), None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    if sxx == 0:
        return 0.0, my, None
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((p[1] - my) ** 2 for p in points)
    ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return slope, intercept, r2


def forecast(records, capacity, now):
    """records: [(datetime, used_bytes)] ordered by time. Returns a prediction dict."""
    pts = [((t - now).total_seconds() / 86400, used) for t, used in records]
    slope, _, r2 = linear_regression(pts)
    used = records[-1][1] if records else 0
    days = full_at = None
    if capacity:
        if used >= capacity:
            days, full_at = 0.0, now
        elif slope > 0:
            days = (capacity - used) / slope
            if days < 3650:
                full_at = now + timedelta(days=days)
            else:
                days = None
    return {"growth_bytes_per_day": slope, "r_squared": r2, "days_remaining": days,
            "predicted_full_at": full_at, "used_bytes": used, "capacity_bytes": capacity,
            "sample_count": len(records)}


def window_records(session, scope, user_id, now, window_days=None):
    window_days = window_days or settings.prediction_window_days
    base = select(StorageUsageRecord.recorded_at, StorageUsageRecord.used_bytes).where(
        StorageUsageRecord.scope == scope, StorageUsageRecord.user_id == user_id,
        StorageUsageRecord.recorded_at <= now)
    rows = session.execute(base.where(StorageUsageRecord.recorded_at >= now - timedelta(days=window_days))
                           .order_by(StorageUsageRecord.recorded_at)).all()
    if len(rows) < settings.min_samples:   # not enough history inside the window: use the latest samples
        rows = session.execute(base.order_by(StorageUsageRecord.recorded_at.desc())
                               .limit(settings.min_samples)).all()[::-1]
    return [(r[0], r[1]) for r in rows]


def run_predictions(session, now, targets):
    """targets: [(scope, user_id, capacity_bytes)]. Stores and returns {(scope, user_id): prediction}."""
    out = {}
    for scope, user_id, capacity in targets:
        recs = window_records(session, scope, user_id, now)
        if not recs:
            continue
        p = forecast(recs, capacity, now)
        session.add(PredictionRecord(created_at=now, scope=scope, user_id=user_id, method=METHOD,
                                     window_days=settings.prediction_window_days, **p))
        out[(scope, user_id)] = p
    return out
