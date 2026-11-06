"""FastAPI application: REST API + monitoring dashboard."""
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from . import predictor, scaling, selfservice
from .collector import Collector
from .config import settings
from .db import (ENTITY_MODELS, Alert, File, OptimizationRecommendation, PredictionRecord, QuotaRequest,
                 ScalingEvent, SessionLocal, Share, Storage, StorageUsageRecord, User, init_db)
from .optimizer import compress_ratio, is_user_tip, user_tip
from .sources import get_source
from .sources.simulator import SCENARIOS, SimulatedNextcloud
from .util import fmt_bytes, iso

STATIC = Path(__file__).parent / "static"
collector: Collector = None


@asynccontextmanager
async def lifespan(_app):
    global collector
    init_db()
    source = get_source()
    collector = Collector(source)
    if settings.mode == "demo":
        collector.seed_history(source)
    collector.start()
    yield
    collector.stop()


app = FastAPI(title="Nextcloud Predictive Storage Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/me")
def portal():
    return FileResponse(STATIC / "me.html")


def _cost(used_bytes):
    """Chargeback: monthly storage cost at COST_PER_GB_MONTH."""
    return round((used_bytes or 0) / 1024 ** 3 * settings.cost_per_gb_month, 2)


def _pred_json(p):
    if not p:
        return None
    return {"growth_bytes_per_day": p.growth_bytes_per_day, "r_squared": p.r_squared,
            "days_remaining": p.days_remaining, "predicted_full_at": iso(p.predicted_full_at),
            "sample_count": p.sample_count, "window_days": p.window_days, "method": p.method,
            "created_at": iso(p.created_at)}


def _latest_prediction(session, scope, user_id=None):
    return session.scalars(select(PredictionRecord).where(PredictionRecord.scope == scope,
                                                          PredictionRecord.user_id == user_id)
                           .order_by(PredictionRecord.created_at.desc(), PredictionRecord.id.desc())
                           .limit(1)).first()


@app.get("/api/overview")
def overview():
    src = collector.source
    with SessionLocal() as s:
        st = s.query(Storage).filter_by(name="primary").one_or_none()
        active = s.scalars(select(Alert).where(Alert.resolved_at.is_(None))).all()
        savings = s.scalar(select(func.coalesce(func.sum(OptimizationRecommendation.potential_savings_bytes), 0))
                           .where(OptimizationRecommendation.status == "open"))
        counts = {name: s.scalar(select(func.count()).select_from(m)) for name, m in ENTITY_MODELS.items()}
        return {
            "mode": settings.mode,
            "window_days": settings.prediction_window_days,
            "scenario": getattr(src, "scenario", None),
            "scenarios": {k: {"label": v["label"], "description": v["description"]} for k, v in SCENARIOS.items()},
            "now": iso(src.now()),
            "nextcloud_url": settings.nextcloud_public_url if settings.mode == "nextcloud" else None,
            "collector": {"last_run": iso(collector.last_run), "last_error": collector.last_error,
                          "duration_s": collector.last_duration, "interval_s": settings.collect_interval},
            "storage": None if not st else {
                "capacity_bytes": st.capacity_bytes, "used_bytes": st.used_bytes, "files_bytes": st.files_bytes,
                "trash_bytes": st.trash_bytes, "versions_bytes": st.versions_bytes, "file_count": st.file_count,
                "updated_at": iso(st.updated_at)},
            "prediction": _pred_json(_latest_prediction(s, "system")),
            "alerts": {"critical": sum(a.severity == "critical" for a in active),
                       "warning": sum(a.severity == "warning" for a in active)},
            "potential_savings_bytes": savings,
            "billing": {"cost_per_gb_month": settings.cost_per_gb_month, "currency": settings.currency,
                        "total_month": _cost(st.used_bytes) if st else 0},
            "entities": counts,
            "thresholds": {"util_warn": settings.util_warn, "util_crit": settings.util_crit,
                           "days_warn": settings.days_warn, "days_crit": settings.days_crit},
        }


@app.get("/api/history")
def history(scope: str = "system", user: str = None, days: float = 30):
    now = collector.source.now()
    with SessionLocal() as s:
        rows = s.execute(select(StorageUsageRecord.recorded_at, StorageUsageRecord.used_bytes,
                                StorageUsageRecord.capacity_bytes)
                         .where(StorageUsageRecord.scope == scope, StorageUsageRecord.user_id == user,
                                StorageUsageRecord.recorded_at >= now - timedelta(days=days))
                         .order_by(StorageUsageRecord.recorded_at)).all()
        capacity = rows[-1][2] if rows else None
        recs = predictor.window_records(s, scope, user, now)
        forecast = []
        if len(recs) >= 2:
            pts = [((t - now).total_seconds() / 86400, u) for t, u in recs]
            slope, intercept, _ = predictor.linear_regression(pts)
            # fitted trend over the window, then projected until capacity (or one window ahead)
            horizon = settings.prediction_window_days
            start_x = pts[0][0]
            end_x = horizon
            if capacity and slope > 0:
                end_x = min(max((capacity - intercept) / slope, horizon * 0.1), 60)
            for x in (start_x, 0.0, end_x):
                forecast.append({"t": iso(now + timedelta(days=x)), "used": max(slope * x + intercept, 0)})
        return {"scope": scope, "user": user, "capacity_bytes": capacity, "now": iso(now),
                "points": [{"t": iso(t), "used": u} for t, u, _ in rows], "forecast": forecast}


@app.get("/api/users")
def users():
    with SessionLocal() as s:
        out = []
        trash = defaultdict(int)
        for owner, area, size in s.execute(select(File.owner_id, File.area, func.sum(File.size_bytes))
                                           .group_by(File.owner_id, File.area)).all():
            trash[(owner, area)] = size or 0
        for u in s.scalars(select(User).order_by(User.uid)).all():
            p = _latest_prediction(s, "user", u.uid)
            q = u.quota
            out.append({"uid": u.uid, "display_name": u.display_name, "role": u.role, "enabled": u.enabled,
                        "last_login": iso(u.last_login),
                        "quota_bytes": q.quota_bytes if q else None, "used_bytes": q.used_bytes if q else 0,
                        "trash_bytes": trash[(u.uid, "trashbin")], "versions_bytes": trash[(u.uid, "versions")],
                        "cost_month": _cost(q.used_bytes if q else 0),
                        "prediction": _pred_json(p)})
        return out


@app.get("/api/alerts")
def get_alerts(include_resolved: bool = False, limit: int = 50):
    with SessionLocal() as s:
        q = select(Alert)
        if not include_resolved:
            q = q.where(Alert.resolved_at.is_(None))
        rows = s.scalars(q.order_by(Alert.resolved_at.is_not(None), Alert.updated_at.desc()).limit(limit)).all()
        return [{"id": a.id, "scope": a.scope, "user_id": a.user_id, "type": a.alert_type, "severity": a.severity,
                 "message": a.message, "created_at": iso(a.created_at), "updated_at": iso(a.updated_at),
                 "resolved_at": iso(a.resolved_at), "notified": a.notified} for a in rows]


@app.get("/api/recommendations")
def recommendations():
    with SessionLocal() as s:
        rows = s.scalars(select(OptimizationRecommendation).where(OptimizationRecommendation.status == "open")
                         .order_by(OptimizationRecommendation.priority,
                                   OptimizationRecommendation.potential_savings_bytes.desc())).all()
        return [{"id": r.id, "category": r.category, "priority": r.priority, "user": r.target_user,
                 "path": r.target_path, "message": r.message, "savings_bytes": r.potential_savings_bytes}
                for r in rows]


@app.post("/api/recommendations/{rec_id}/dismiss")
def dismiss(rec_id: int):
    with SessionLocal() as s:
        r = s.get(OptimizationRecommendation, rec_id)
        if not r:
            raise HTTPException(404)
        r.status = "dismissed"
        s.commit()
    return {"ok": True}


@app.get("/api/consumers")
def consumers(limit: int = 10):
    with SessionLocal() as s:
        files = s.scalars(select(File).where(File.area == "files").order_by(File.size_bytes.desc()).limit(limit)).all()
        by_type = defaultdict(int)
        for mime, size in s.execute(select(File.mimetype, func.sum(File.size_bytes))
                                    .where(File.area == "files").group_by(File.mimetype)).all():
            major = (mime or "other").split("/")[0]
            kind = {"image": "Images", "video": "Videos", "audio": "Audio", "text": "Documents"}.get(major)
            if not kind:
                kind = "Archives" if any(k in mime for k in ("zip", "gzip", "tar", "x-7z", "rar")) else \
                    "Documents" if any(k in mime for k in ("pdf", "officedocument", "msword", "opendocument")) \
                    else "Other"
            by_type[kind] += size or 0
        return {"files": [{"id": f.id, "owner": f.owner_id, "name": f.name, "path": f.path, "size": f.size_bytes,
                           "mimetype": f.mimetype, "mtime": iso(f.mtime)} for f in files],
                "by_type": sorted(({"type": k, "bytes": v} for k, v in by_type.items()), key=lambda x: -x["bytes"])}


@app.get("/api/shares")
def shares():
    with SessionLocal() as s:
        rows = s.scalars(select(Share).order_by(Share.id)).all()
        return [{"id": r.id, "owner": r.owner_id, "path": r.item_path, "type": r.item_type,
                 "share_type": r.share_type, "share_with": r.share_with,
                 "permissions": None if not r.permission else
                 [n for n, ok in (("read", r.permission.can_read), ("update", r.permission.can_update),
                                  ("create", r.permission.can_create), ("delete", r.permission.can_delete),
                                  ("share", r.permission.can_share)) if ok]} for r in rows]


@app.get("/api/predictions")
def predictions(limit: int = 200):
    """How the system-level forecast evolved over time."""
    with SessionLocal() as s:
        rows = s.scalars(select(PredictionRecord).where(PredictionRecord.scope == "system")
                         .order_by(PredictionRecord.created_at.desc()).limit(limit)).all()
        return [dict(_pred_json(p), used_bytes=p.used_bytes) for p in reversed(rows)]


@app.post("/api/collect")
def collect_now():
    ok = collector.collect()
    return {"ok": ok, "error": collector.last_error}


class ResetBody(BaseModel):
    scenario: str = "steady"


class AdvanceBody(BaseModel):
    days: float = 1


def _require_demo():
    if settings.mode != "demo":
        raise HTTPException(400, "Only available in demo mode")


@app.post("/api/demo/reset")
def demo_reset(body: ResetBody):
    _require_demo()
    if body.scenario not in SCENARIOS:
        raise HTTPException(400, f"Unknown scenario {body.scenario}")
    collector.seed_history(SimulatedNextcloud(body.scenario))
    return {"ok": True, "scenario": body.scenario}


@app.post("/api/demo/advance")
def demo_advance(body: AdvanceBody):
    """Move the simulated clock forward, collecting a sample every 6 simulated hours."""
    _require_demo()
    steps = max(1, round(body.days * 4))
    for _ in range(steps):
        collector.source.advance(body.days / steps)
        collector.collect()
    return {"ok": collector.last_error is None, "error": collector.last_error}


# ------------------------------------------------------------------ elastic scaling (admin)

class PoolBody(BaseModel):
    capacity_gb: float = None
    autoscale: bool = None
    autoscale_at_pct: float = None
    autoscale_step_pct: float = None


class QuotaBody(BaseModel):
    quota_mb: float


class DecisionBody(BaseModel):
    quota_mb: float = None


def _event_json(e):
    return {"id": e.id, "at": iso(e.created_at), "kind": e.kind, "target": e.target, "old_bytes": e.old_bytes,
            "new_bytes": e.new_bytes, "actor": e.actor, "note": e.note}


def _request_json(r):
    return {"id": r.id, "user": r.user_id, "current_quota_bytes": r.current_quota_bytes, "used_bytes": r.used_bytes,
            "requested_quota_bytes": r.requested_quota_bytes, "granted_quota_bytes": r.granted_quota_bytes,
            "reason": r.reason, "status": r.status, "created_at": iso(r.created_at), "decided_at": iso(r.decided_at)}


def _notify(uid, short, long=""):
    if settings.notify_nextcloud and hasattr(collector.source, "notify_user"):
        try:
            collector.source.notify_user(uid, short, long)
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            print(f"[api] notification failed: {exc}")


@app.get("/api/admin/scaling")
def scaling_state():
    with SessionLocal() as s:
        cfg = scaling.pool(s)
        st = s.query(Storage).filter_by(name="primary").one_or_none()
        allocated = scaling.allocated_bytes(s)
        reqs = s.scalars(select(QuotaRequest).order_by(QuotaRequest.status != "pending",
                                                       QuotaRequest.created_at.desc()).limit(20)).all()
        events = s.scalars(select(ScalingEvent).order_by(ScalingEvent.created_at.desc(), ScalingEvent.id.desc())
                           .limit(15)).all()
        s.commit()
        return {"pool": {"capacity_bytes": cfg.capacity_bytes, "max_capacity_bytes": cfg.max_capacity_bytes,
                         "used_bytes": st.used_bytes if st else 0, "autoscale": cfg.autoscale,
                         "autoscale_at_pct": cfg.autoscale_at_pct, "autoscale_step_pct": cfg.autoscale_step_pct,
                         "allocated_quota_bytes": allocated},
                "requests": [_request_json(r) for r in reqs], "events": [_event_json(e) for e in events]}


@app.post("/api/admin/pool")
def update_pool(body: PoolBody):
    now = collector.source.now()
    with SessionLocal() as s:
        cfg = scaling.pool(s)
        if body.capacity_gb is not None:
            if body.capacity_gb * 1024 ** 3 > cfg.max_capacity_bytes:
                raise HTTPException(400, f"The volume limit is {fmt_bytes(cfg.max_capacity_bytes)}")
            scaling.resize_pool(s, now, int(body.capacity_gb * 1024 ** 3))
        changed = False
        for k in ("autoscale", "autoscale_at_pct", "autoscale_step_pct"):
            if getattr(body, k) is not None and getattr(body, k) != getattr(cfg, k):
                setattr(cfg, k, getattr(body, k))
                changed = True
        if changed:
            scaling.log(s, now, "policy", "primary", None, None, "admin",
                        f"Autoscaling {'enabled' if cfg.autoscale else 'disabled'}: grow by "
                        f"{cfg.autoscale_step_pct:.0f}% when the pool is {cfg.autoscale_at_pct:.0f}% full.")
        s.commit()
    collector.collect()
    return {"ok": True}


def _apply_quota(uid, quota_bytes, note=""):
    if not hasattr(collector.source, "set_quota"):
        raise HTTPException(400, "This data source cannot change quotas")
    with SessionLocal() as s:
        try:
            scaling.set_quota(s, collector.source.now(), collector.source, uid, quota_bytes, note=note)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Nextcloud: {exc}") from exc
        s.commit()


@app.post("/api/admin/users/{uid}/quota")
def admin_set_quota(uid: str, body: QuotaBody):
    _apply_quota(uid, body.quota_mb * 1024 ** 2)
    _notify(uid, f"Your storage quota is now {fmt_bytes(body.quota_mb * 1024 ** 2)}",
            "The administrator changed your storage allocation.")
    collector.collect()
    return {"ok": True}


@app.post("/api/admin/quota-requests/{req_id}/{decision}")
def decide_request(req_id: int, decision: str, body: DecisionBody = None):
    if decision not in ("approve", "deny"):
        raise HTTPException(404)
    with SessionLocal() as s:
        r = s.get(QuotaRequest, req_id)
        if not r or r.status != "pending":
            raise HTTPException(404, "No such pending request")
        uid = r.user_id
        granted = int(body.quota_mb * 1024 ** 2) if body and body.quota_mb else r.requested_quota_bytes
    if decision == "approve":
        _apply_quota(uid, granted, note=f"Quota request #{req_id} of {uid} approved: {fmt_bytes(granted)}.")
    with SessionLocal() as s:
        r = s.get(QuotaRequest, req_id)
        r.status, r.decided_at = ("approved" if decision == "approve" else "denied"), collector.source.now()
        r.granted_quota_bytes = granted if decision == "approve" else None
        if decision == "deny":
            scaling.log(s, r.decided_at, "quota", uid, r.current_quota_bytes, r.current_quota_bytes, "admin",
                        f"Quota request #{req_id} of {uid} denied.")
        s.commit()
    if decision == "approve":
        _notify(uid, f"Storage request approved: your quota is now {fmt_bytes(granted)}",
                "The administrator approved your request for more storage.")
    else:
        _notify(uid, "Storage request denied", f"Please free up space using {settings.monitor_public_url}/me.")
    collector.collect()
    return {"ok": True}


# ------------------------------------------------------------------ user self-service portal

class LoginBody(BaseModel):
    user: str
    password: str


class RequestBody(BaseModel):
    extra_mb: float
    reason: str = ""


class CompressBody(BaseModel):
    file_id: int


def _me(token):
    sess = selfservice.session(token)
    if not sess:
        raise HTTPException(401, "Please log in")
    return sess


@app.post("/api/me/login")
def me_login(body: LoginBody):
    if settings.mode != "nextcloud":
        raise HTTPException(400, "The user portal needs a live Nextcloud (MONITOR_MODE=nextcloud)")
    res = selfservice.login(body.user, body.password)
    if not res:
        raise HTTPException(401, "Wrong Nextcloud username or password")
    return {"token": res[0], "uid": res[1]}


def _file_id(rec_key):
    kind, _, rest = rec_key.partition(":")
    return int(rest) if kind in ("compress", "large") and rest.isdigit() else None


@app.get("/api/me")
def me(x_session: str = Header(None)):
    uid, _ = _me(x_session)
    with SessionLocal() as s:
        u = s.get(User, uid)
        q = u.quota if u else None
        quota, used = (q.quota_bytes if q else None), (q.used_bytes if q else 0)
        areas = defaultdict(int)
        for area, size in s.execute(select(File.area, func.sum(File.size_bytes)).where(File.owner_id == uid)
                                    .group_by(File.area)).all():
            areas[area] = size or 0
        recs = s.scalars(select(OptimizationRecommendation).where(OptimizationRecommendation.status == "open",
                                                                  OptimizationRecommendation.target_user == uid)
                         .order_by(OptimizationRecommendation.priority,
                                   OptimizationRecommendation.potential_savings_bytes.desc())).all()
        files = s.scalars(select(File).where(File.owner_id == uid, File.area == "files")
                          .order_by(File.size_bytes.desc()).limit(10)).all()
        alerts = s.scalars(select(Alert).where(Alert.user_id == uid, Alert.resolved_at.is_(None))).all()
        reqs = s.scalars(select(QuotaRequest).where(QuotaRequest.user_id == uid)
                         .order_by(QuotaRequest.created_at.desc()).limit(5)).all()
        cutoff = collector.source.now() - timedelta(days=settings.old_file_days)
        cold = s.scalars(select(File).where(File.owner_id == uid, File.area == "files", File.mtime < cutoff)
                         .order_by(File.size_bytes.desc()).limit(15)).all()
        return {"uid": uid, "display_name": u.display_name if u else uid, "quota_bytes": quota, "used_bytes": used,
                "trash_bytes": areas["trashbin"], "versions_bytes": areas["versions"],
                "cost_month": _cost(used), "currency": settings.currency,
                "cost_per_gb_month": settings.cost_per_gb_month,
                "prediction": _pred_json(_latest_prediction(s, "user", uid)),
                "alerts": [{"severity": a.severity, "type": a.alert_type, "message": a.message} for a in alerts],
                "old_file_days": settings.old_file_days,
                "cold_files": [{"id": f.id, "name": f.name, "path": f.path, "size": f.size_bytes,
                                "mimetype": f.mimetype, "mtime": iso(f.mtime),
                                "compressible": compress_ratio(f.mimetype, f.name) > 0} for f in cold],
                "recommendations": [{"id": r.id, "category": r.category, "priority": r.priority,
                                     "path": r.target_path,
                                     "message": user_tip(r.category, r.target_path, r.potential_savings_bytes)
                                     or r.message,
                                     "savings_bytes": r.potential_savings_bytes, "file_id": _file_id(r.rec_key)}
                                    for r in recs if is_user_tip(r.rec_key)],
                "files": [{"id": f.id, "name": f.name, "path": f.path, "size": f.size_bytes, "mimetype": f.mimetype,
                           "mtime": iso(f.mtime), "compressible": compress_ratio(f.mimetype, f.name) > 0}
                          for f in files],
                "requests": [_request_json(r) for r in reqs]}


@app.post("/api/me/quota-request")
def me_request(body: RequestBody, x_session: str = Header(None)):
    uid, _ = _me(x_session)
    if body.extra_mb <= 0:
        raise HTTPException(400, "Ask for a positive amount")
    with SessionLocal() as s:
        if s.scalar(select(func.count()).select_from(QuotaRequest)
                    .where(QuotaRequest.user_id == uid, QuotaRequest.status == "pending")):
            raise HTTPException(400, "You already have a pending request")
        q = s.get(User, uid).quota
        base = q.quota_bytes or q.used_bytes
        req = scaling.request_quota(s, collector.source.now(), uid, q.quota_bytes, q.used_bytes,
                                    base + body.extra_mb * 1024 ** 2, body.reason)
        s.commit()
        current = fmt_bytes(q.quota_bytes) if q.quota_bytes else "unlimited"
        msg = f"{uid} asks for +{fmt_bytes(body.extra_mb * 1024 ** 2)} ({current} -> {fmt_bytes(req.requested_quota_bytes)})"
    _notify(settings.nextcloud_admin_user, f"[QUOTA REQUEST] {msg}",
            f"Reason: {body.reason or '-'}. Approve or deny it on {settings.monitor_public_url}.")
    return {"ok": True}


@app.post("/api/me/compress")
def me_compress(body: CompressBody, x_session: str = Header(None)):
    uid, password = _me(x_session)
    with SessionLocal() as s:
        f = s.get(File, body.file_id)
        if not f or f.owner_id != uid or f.area != "files":
            raise HTTPException(404, "File not found")
        path = f.path
    try:
        res = selfservice.compress(uid, password, path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Nextcloud: {exc}") from exc
    if res["compressed"]:
        with SessionLocal() as s:
            scaling.log(s, collector.source.now(), "compress", uid, res["before"], res["after"], uid, res["message"])
            s.commit()
    collector.collect()
    return res


@app.post("/api/me/delete")
def me_delete(body: CompressBody, x_session: str = Header(None)):
    uid, password = _me(x_session)
    with SessionLocal() as s:
        f = s.get(File, body.file_id)
        if not f or f.owner_id != uid or f.area != "files":
            raise HTTPException(404, "File not found")
        path, name, size = f.path, f.name, f.size_bytes
    try:
        selfservice.delete_file(uid, password, path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Nextcloud: {exc}") from exc
    with SessionLocal() as s:
        scaling.log(s, collector.source.now(), "cleanup", uid, size, 0, uid,
                    f"{uid} deleted '{name}' ({fmt_bytes(size)} freed).")
        s.commit()
    collector.collect()
    return {"ok": True, "freed": size, "name": name}


@app.post("/api/me/empty-trash")
def me_empty_trash(x_session: str = Header(None)):
    uid, password = _me(x_session)
    with SessionLocal() as s:
        size = s.scalar(select(func.coalesce(func.sum(File.size_bytes), 0))
                        .where(File.owner_id == uid, File.area == "trashbin"))
    try:
        selfservice.empty_trash(uid, password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Nextcloud: {exc}") from exc
    with SessionLocal() as s:
        scaling.log(s, collector.source.now(), "trash", uid, size, 0, uid,
                    f"{uid} emptied the trash bin ({fmt_bytes(size)} freed).")
        s.commit()
    collector.collect()
    return {"ok": True, "freed": size}
