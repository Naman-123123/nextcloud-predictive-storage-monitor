"""The monitoring pipeline:

  Nextcloud -> collect usage -> store history -> analyse growth -> predict capacity
            -> generate alerts -> optimization recommendations
"""
import threading
import time
from datetime import timedelta

from sqlalchemy import delete, insert

from . import alerts, optimizer, predictor, scaling
from .config import settings
from .db import (File, Folder, Permission, Quota, SessionLocal, Share, Storage, StorageUsageRecord, User,
                 reset_db, utcnow)

SEED_STEP = timedelta(hours=6)


def _storage_totals(snap, capacity):
    if snap.areas:
        files = sum(a["files"] for a in snap.areas.values())
        trash = sum(a["trashbin"] for a in snap.areas.values())
        versions = sum(a["versions"] for a in snap.areas.values())
    else:                                   # no database inventory: fall back to OCS quota usage
        files, trash, versions = sum(u["used_bytes"] for u in snap.users), 0, 0
    return {"capacity_bytes": capacity, "used_bytes": files + trash + versions, "files_bytes": files,
            "trash_bytes": trash, "versions_bytes": versions, "file_count": len(snap.files)}


def _record_usage(session, now, snap, storage):
    session.add(StorageUsageRecord(recorded_at=now, scope="system", used_bytes=storage["used_bytes"],
                                   capacity_bytes=storage["capacity_bytes"], file_count=storage["file_count"]))
    counts = {}
    for f in snap.files:
        if f["area"] == "files":
            counts[f["owner"]] = counts.get(f["owner"], 0) + 1
    for u in snap.users:
        session.add(StorageUsageRecord(recorded_at=now, scope="user", user_id=u["uid"], used_bytes=u["used_bytes"],
                                       capacity_bytes=u["quota_bytes"], file_count=counts.get(u["uid"])))


def _sync_inventory(session, now, snap, storage):
    seen = set()
    for u in snap.users:
        seen.add(u["uid"])
        user = session.get(User, u["uid"]) or User(uid=u["uid"])
        user.display_name, user.email, user.role = u["display_name"], u["email"], u["role"]
        user.enabled, user.last_login, user.synced_at = u["enabled"], u["last_login"], now
        session.add(user)
        if user.quota is None:
            user.quota = Quota()
        user.quota.quota_bytes, user.quota.used_bytes, user.quota.updated_at = u["quota_bytes"], u["used_bytes"], now
    for user in session.query(User).filter(User.uid.notin_(seen)).all():
        user.enabled = False

    st = session.query(Storage).filter_by(name="primary").one_or_none() or Storage(name="primary")
    for k, v in storage.items():
        setattr(st, k, v)
    st.updated_at = now
    session.add(st)

    if not snap.inventory_available:
        return
    for model in (Permission, Share, File, Folder):
        session.execute(delete(model))
    if snap.folders:
        session.execute(insert(Folder), [
            {"id": f["id"], "owner_id": f["owner"], "parent_id": f["parent_id"], "path": f["path"][:1000],
             "name": (f["name"] or "")[:255], "size_bytes": f["size"], "mtime": f["mtime"]} for f in snap.folders])
    if snap.files:
        session.execute(insert(File), [
            {"id": f["id"], "owner_id": f["owner"], "folder_id": f["parent_id"], "path": f["path"][:1000],
             "name": (f["name"] or "")[:255], "mimetype": (f["mimetype"] or "")[:255], "size_bytes": f["size"],
             "mtime": f["mtime"], "area": f["area"]} for f in snap.files])
    for s in snap.shares:
        b = s["permissions"]
        session.add(Share(id=s["id"], owner_id=s["owner"], item_id=s["item_id"], item_type=s["item_type"],
                          item_path=(s["item_path"] or "")[:1000], share_type=s["share_type"],
                          share_with=s["share_with"], created_at=s["created_at"],
                          permission=Permission(bitmask=b, can_read=bool(b & 1), can_update=bool(b & 2),
                                                can_create=bool(b & 4), can_delete=bool(b & 8),
                                                can_share=bool(b & 16))))


def _capacity(source):
    return getattr(source, "capacity", None) or settings.capacity_bytes


def _analyse(session, now, snap, storage, notify, notify_user=None):
    targets = [("system", None, storage["capacity_bytes"])] + \
              [("user", u["uid"], u["quota_bytes"]) for u in snap.users]
    preds = predictor.run_predictions(session, now, targets)
    alerts.evaluate(session, now, storage, snap.users, preds, notify, notify_user)
    return preds


class Collector:
    def __init__(self, source):
        self.source = source
        self.lock = threading.Lock()
        self.last_run = None
        self.last_error = None
        self.last_duration = None
        self._stop = threading.Event()

    def _notify(self):
        if settings.notify_nextcloud and hasattr(self.source, "notify_admin"):
            return self.source.notify_admin
        return None

    def _notify_user(self):
        if settings.notify_nextcloud and hasattr(self.source, "notify_user"):
            return self.source.notify_user
        return None

    def collect(self):
        """One full monitoring cycle against the current state of the source."""
        with self.lock:
            started = time.time()
            try:
                snap = self.source.snapshot()
                now = snap.taken_at
                with SessionLocal() as session:
                    storage = _storage_totals(snap, scaling.pool(session).capacity_bytes)
                    scaling.autoscale(session, now, storage, self._notify())
                    _sync_inventory(session, now, snap, storage)
                    _record_usage(session, now, snap, storage)
                    session.flush()
                    preds = _analyse(session, now, snap, storage, self._notify(), self._notify_user())
                    session.flush()
                    optimizer.generate(session, now, storage, snap.users, preds)
                    if self._notify_user():
                        session.flush()
                        optimizer.notify_users(session, now, self._notify_user())
                    session.commit()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive, surface the error
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                print(f"[collector] cycle failed: {self.last_error}")
            self.last_run = utcnow()
            self.last_duration = round(time.time() - started, 2)
            return self.last_error is None

    def seed_history(self, source):
        """Demo only: rebuild the database and replay the simulator's past 30 days."""
        with self.lock:
            self.source = source
            reset_db()
            with SessionLocal() as session:
                t = source.history_start
                end = source.now()
                while t < end:
                    snap = source.snapshot(t)
                    storage = _storage_totals(snap, _capacity(source))
                    _record_usage(session, t, snap, storage)
                    session.flush()
                    _analyse(session, t, snap, storage, notify=None)
                    session.flush()
                    t += SEED_STEP
                session.commit()
        self.collect()

    # ------------------------------------------------------------ background scheduler
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="collector").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        if self.last_run is None:
            self.collect()
        while not self._stop.wait(settings.collect_interval):
            self.collect()
