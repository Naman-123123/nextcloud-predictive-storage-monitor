"""Elastic storage: pool resizing (manual or automatic) and per-user quota requests.

  pool       - the provisioned capacity being forecast; the admin can resize it, and with
               autoscaling on it grows by a step whenever it passes a utilization threshold
  quotas     - users ask for more space, the admin approves (quota changed in Nextcloud) or denies
  events     - every change is written to scaling_events as an audit log
"""
from sqlalchemy import func, select

from .config import settings
from .db import PoolConfig, Quota, QuotaRequest, ScalingEvent
from .util import fmt_bytes


def pool(session):
    cfg = session.scalars(select(PoolConfig).limit(1)).first()
    if not cfg:
        cfg = PoolConfig(capacity_bytes=settings.capacity_bytes,
                         max_capacity_bytes=max(settings.pool_max_bytes, settings.capacity_bytes * 4),
                         autoscale=False, autoscale_at_pct=85, autoscale_step_pct=25)
        session.add(cfg)
        session.flush()
    return cfg


def log(session, now, kind, target, old, new, actor, note):
    session.add(ScalingEvent(created_at=now, kind=kind, target=target, old_bytes=old, new_bytes=new,
                             actor=actor, note=note[:500]))


def fmt_gb(n):
    return f"{n / 1024 ** 3:.2f} GB"


def resize_pool(session, now, new_bytes, actor="admin", note=""):
    cfg = pool(session)
    new_bytes = int(min(max(new_bytes, 1), cfg.max_capacity_bytes))
    old = cfg.capacity_bytes
    if abs(new_bytes - old) < 1024 ** 2:        # same size: nothing to do, nothing to log
        return cfg
    cfg.capacity_bytes, cfg.updated_at = new_bytes, now
    log(session, now, "pool", "primary", old, new_bytes, actor,
        note or f"Pool {'grown' if new_bytes > old else 'shrunk'} from {fmt_gb(old)} to {fmt_gb(new_bytes)}.")
    return cfg


def autoscale(session, now, storage, notify=None):
    """Grow the pool one step when it is too full. Returns the event note, or None."""
    cfg = pool(session)
    cap, used = cfg.capacity_bytes, storage["used_bytes"]
    if not cfg.autoscale or not cap or used / cap * 100 < cfg.autoscale_at_pct or cap >= cfg.max_capacity_bytes:
        return None
    new = min(int(cap * (1 + cfg.autoscale_step_pct / 100)), cfg.max_capacity_bytes)
    note = (f"Autoscaler: pool reached {used / cap * 100:.0f}% (threshold {cfg.autoscale_at_pct:.0f}%), "
            f"grown by {cfg.autoscale_step_pct:.0f}% from {fmt_gb(cap)} to {fmt_gb(new)}.")
    resize_pool(session, now, new, actor="autoscaler", note=note)
    storage["capacity_bytes"] = new
    if notify:
        try:
            notify("[SCALING] Storage pool expanded automatically", note)
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            print(f"[scaling] notification failed: {exc}")
    return note


def allocated_bytes(session):
    """Sum of all user quotas, to show over-commitment (thin provisioning) of the pool."""
    return session.scalar(select(func.coalesce(func.sum(Quota.quota_bytes), 0))) or 0


def request_quota(session, now, uid, quota_bytes, used_bytes, requested_bytes, reason):
    req = QuotaRequest(user_id=uid, current_quota_bytes=quota_bytes, used_bytes=used_bytes,
                       requested_quota_bytes=int(requested_bytes), reason=(reason or "")[:500],
                       status="pending", created_at=now)
    session.add(req)
    return req


def set_quota(session, now, source, uid, new_bytes, actor="admin", note=""):
    q = session.scalars(select(Quota).where(Quota.user_id == uid)).first()
    old = q.quota_bytes if q else None
    source.set_quota(uid, new_bytes)
    if q:
        q.quota_bytes = int(new_bytes)
    log(session, now, "quota", uid, old, int(new_bytes), actor,
        note or f"Quota of {uid} changed from {fmt_bytes(old) if old else 'unlimited'} to {fmt_bytes(new_bytes)}.")
