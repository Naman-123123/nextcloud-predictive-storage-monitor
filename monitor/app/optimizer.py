"""Identifies major storage consumers and produces optimization recommendations.

Rules:
  large_file  - individual files above LARGE_FILE_MB (top 5)
  compress    - compressible files (text, logs, CSV, BMP, WAV, tar...) that a ZIP would shrink
  duplicate   - identical files (same name and size) stored more than once
  trash       - users whose trash bin holds more than TRASH_MIN_MB
  versions    - users whose old file versions take more than VERSIONS_MIN_MB
  old_files   - data not modified for OLD_FILE_DAYS (archive candidates)
  quota       - users close to their quota, or heavy users without any quota
  capacity    - pool expansion needed to sustain CAPACITY_PLANNING_DAYS of growth
"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import delete, select

from .config import settings
from .db import File, OptimizationRecommendation, User
from .util import fmt_bytes, fmt_days


# typical ZIP saving for formats that are not already compressed
COMPRESS_RATIO = [("text/", 0.75), ("csv", 0.75), ("json", 0.75), ("xml", 0.7), ("sql", 0.75), ("x-log", 0.8),
                  ("javascript", 0.7), ("x-tar", 0.6), ("bmp", 0.6), ("tiff", 0.4), ("wav", 0.3),
                  ("x-ms-dos-executable", 0.3), ("svg", 0.7)]
COMPRESS_MIN_BYTES = 1024 ** 2


def compress_ratio(mimetype, name=""):
    mime = (mimetype or "").lower()
    if "openxmlformats" in mime or "opendocument" in mime or name.lower().endswith(".zip"):
        return 0                    # office documents are ZIP archives already
    if name.lower().endswith((".log", ".csv", ".txt", ".sql", ".json")):
        return 0.75
    return next((r for key, r in COMPRESS_RATIO if key in mime), 0)


def _recommendations(session, now, storage, users, preds):
    files = session.scalars(select(File)).all()
    live = [f for f in files if f.area == "files"]
    recs = []

    for f in sorted((f for f in live if f.size_bytes >= settings.large_file_bytes),
                    key=lambda f: -f.size_bytes)[:5]:
        recs.append(("large_file", f"large:{f.id}", 2, f.owner_id, f.path, f.size_bytes,
                     f"Large file '{f.name}' ({fmt_bytes(f.size_bytes)}) owned by {f.owner_id}: "
                     f"review, compress or move it to archive storage."))

    candidates = [(f, compress_ratio(f.mimetype, f.name)) for f in live if f.size_bytes >= COMPRESS_MIN_BYTES]
    for f, ratio in sorted((c for c in candidates if c[1]), key=lambda c: -c[0].size_bytes * c[1])[:10]:
        recs.append(("compress", f"compress:{f.id}", 1 if f.size_bytes * ratio >= 10 * 1024 ** 2 else 2,
                     f.owner_id, f.path, int(f.size_bytes * ratio),
                     f"'{f.name}' ({fmt_bytes(f.size_bytes)}, {f.mimetype}) owned by {f.owner_id} is not "
                     f"compressed. Zipping it would free about {fmt_bytes(f.size_bytes * ratio)}."))

    groups = defaultdict(list)
    for f in live:
        groups[(f.name, f.size_bytes)].append(f)
    dups = sorted((g for g in groups.values() if len(g) > 1), key=lambda g: -g[0].size_bytes * (len(g) - 1))
    for g in dups[:5]:
        owners = ", ".join(sorted({f.owner_id for f in g}))
        waste = g[0].size_bytes * (len(g) - 1)
        recs.append(("duplicate", f"dup:{g[0].name}:{g[0].size_bytes}", 2, None, g[0].name, waste,
                     f"'{g[0].name}' is stored {len(g)} times ({owners}). Keep one copy and share it "
                     f"instead to free {fmt_bytes(waste)}."))

    per_user = defaultdict(lambda: defaultdict(int))
    old_cutoff = now - timedelta(days=settings.old_file_days)
    for f in files:
        per_user[f.owner_id][f.area] += f.size_bytes
        if f.area == "files" and f.mtime and f.mtime < old_cutoff:
            per_user[f.owner_id]["old"] += f.size_bytes
    for uid, a in per_user.items():
        if a["trashbin"] >= settings.trash_min_bytes:
            recs.append(("trash", f"trash:{uid}", 1, uid, None, a["trashbin"],
                         f"Trash bin of {uid} holds {fmt_bytes(a['trashbin'])} of deleted files. "
                         f"Empty it or shorten trashbin_retention_obligation."))
        if a["versions"] >= settings.versions_min_bytes:
            recs.append(("versions", f"versions:{uid}", 2, uid, None, a["versions"],
                         f"Old file versions of {uid} use {fmt_bytes(a['versions'])}. "
                         f"Expire old versions (occ versions:cleanup) or lower versions_retention_obligation."))
        if a["old"] >= settings.old_files_min_bytes:
            recs.append(("old_files", f"old:{uid}", 3, uid, None, a["old"],
                         f"{fmt_bytes(a['old'])} of {uid}'s files have not been modified for "
                         f"{settings.old_file_days}+ days. Archive them to cheaper cold storage."))

    total = storage["used_bytes"] or 1
    for u in users:
        if u["quota_bytes"]:
            pct = u["used_bytes"] / u["quota_bytes"] * 100
            if pct >= 85:
                recs.append(("quota", f"quota:{u['uid']}", 1, u["uid"], None, 0,
                             f"{u['uid']} is at {pct:.0f}% of quota. Ask the user to clean up "
                             f"or raise the quota above {fmt_bytes(u['quota_bytes'])}."))
        else:
            share = per_user[u["uid"]]["files"] / total * 100
            if share >= 25:
                recs.append(("quota", f"noquota:{u['uid']}", 2, u["uid"], None, 0,
                             f"{u['uid']} has no quota but holds {share:.0f}% of all stored data. "
                             f"Assign a storage quota to prevent one user exhausting the pool."))

    p = preds.get(("system", None))
    if p and p["days_remaining"] is not None and p["days_remaining"] < settings.capacity_planning_days:
        days = settings.capacity_planning_days
        need = p["growth_bytes_per_day"] * days - (storage["capacity_bytes"] - storage["used_bytes"])
        recs.append(("capacity", "capacity", 1, None, None, 0,
                     f"Capacity runs out in ~{fmt_days(p['days_remaining'])}. Add at least {fmt_bytes(need)} "
                     f"of storage to sustain the current growth rate for the next {days} days."))
    return recs


def generate(session, now, storage, users, preds):
    dismissed = set(session.scalars(select(OptimizationRecommendation.rec_key)
                                    .where(OptimizationRecommendation.status == "dismissed")).all())
    session.execute(delete(OptimizationRecommendation).where(OptimizationRecommendation.status == "open"))
    for cat, key, prio, user, path, savings, msg in _recommendations(session, now, storage, users, preds):
        if key in dismissed:
            continue
        session.add(OptimizationRecommendation(rec_key=key[:255], category=cat, priority=prio, target_user=user,
                                               target_path=path, potential_savings_bytes=int(savings),
                                               message=msg[:1000], created_at=now, status="open"))


def user_tip(category, path, savings):
    """The recommendation phrased for the user who owns the data."""
    name = (path or "").rsplit("/", 1)[-1]
    return {
        "trash": f"Empty your trash bin: {fmt_bytes(savings)} of deleted files still count against your quota.",
        "versions": f"Old versions of files you edited take {fmt_bytes(savings)}. Delete versions you no longer need "
                    f"(Files > file details > Versions).",
        "old_files": f"{fmt_bytes(savings)} of your files have not been changed for {settings.old_file_days}+ days. "
                     f"Delete or archive the ones you no longer need.",
        "compress": f"'{name}' is not compressed. Zipping it would free about {fmt_bytes(savings)}.",
        "large_file": f"'{name}' ({fmt_bytes(savings)}) is one of the largest files in the cloud. "
                      f"Delete it, compress it or move it elsewhere if you can.",
        "quota": "You are close to your quota. Free space with the tips below or ask the administrator for more.",
    }.get(category)


USER_TIP_CATEGORIES = ("trash", "versions", "old_files", "compress", "large_file", "quota")


def is_user_tip(rec_key):
    """'noquota:<uid>' asks the admin to assign a quota; it is not something the user can act on."""
    return not rec_key.startswith("noquota:")


def notify_users(session, now, notify_user):
    """Push each user's new storage tips into their Nextcloud bell: one summary per user, each tip once."""
    from .db import RecommendationNotice
    recs = session.scalars(select(OptimizationRecommendation).where(
        OptimizationRecommendation.status == "open", OptimizationRecommendation.target_user.is_not(None),
        OptimizationRecommendation.category.in_(USER_TIP_CATEGORIES))).all()
    recs = [r for r in recs if is_user_tip(r.rec_key)]
    live_keys = {r.rec_key for r in recs}
    notices = session.scalars(select(RecommendationNotice)).all()
    for n in notices:                       # tip no longer applies: re-arm it for the future
        if n.rec_key not in live_keys:
            session.delete(n)
    sent = {n.rec_key for n in notices if n.rec_key in live_keys}
    fresh = defaultdict(list)
    for r in recs:
        if r.rec_key not in sent:
            fresh[r.target_user].append(r)
    for uid, items in fresh.items():
        items.sort(key=lambda r: -r.potential_savings_bytes)
        total = sum(r.potential_savings_bytes for r in items)
        lines = [f"- {user_tip(r.category, r.target_path, r.potential_savings_bytes)}" for r in items]
        try:
            notify_user(uid, f"Storage tips: {len(items)} way{'s' if len(items) > 1 else ''} to free "
                             f"{'up to ' + fmt_bytes(total) if total else 'space'}",
                        "\n".join(lines) + f"\n\nAct on them in one click at {settings.monitor_public_url}/me")
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            print(f"[optimizer] tip notification for {uid} failed: {exc}")
            continue
        for r in items:
            session.add(RecommendationNotice(rec_key=r.rec_key, user_id=uid, notified_at=now))


def top_consumers(session, limit=10):
    files = session.scalars(select(File).where(File.area == "files")
                            .order_by(File.size_bytes.desc()).limit(limit)).all()
    users = {u.uid: u.display_name for u in session.scalars(select(User)).all()}
    return files, users
