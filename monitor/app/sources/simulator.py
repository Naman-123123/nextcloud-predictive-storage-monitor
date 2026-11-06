"""A deterministic, simulated Nextcloud used for demos and evaluation.

It builds a catalogue of files for five users from a growth-rate profile (the
"scenario"). Each file has a creation time and, sometimes, a deletion time (it
then moves to the trash bin) or later versions. A snapshot at time t returns
exactly what a real Nextcloud would show at t, so the collector, predictor,
alerts and recommendations run unchanged against it.

A virtual clock can be advanced (e.g. "+1 day") to watch the forecast evolve.
"""
import math
import random
from datetime import datetime, timedelta

from ..config import MB, settings
from . import Snapshot

HISTORY_DAYS = 30
HORIZON_DAYS = 90
STEP = timedelta(hours=1)

# uid, display name, role, quota (bytes or None), share of growth, share of initial data
USERS = [
    ("admin", "Administrator", "admin", None, 0.03, 0.08),
    ("alice", "Alice Sharma", "user", 4 * 1024 * MB, 0.40, 0.30),
    ("bob", "Bob Mathew", "user", 3 * 1024 * MB, 0.25, 0.23),
    ("carol", "Carol Dsouza", "user", None, 0.24, 0.27),
    ("dave", "Dave Rao", "user", 1024 * MB, 0.08, 0.12),
]

# folder, extension, mimetype, min MB, max MB, relative frequency
FILE_TYPES = [
    ("Documents", "pdf", "application/pdf", 0.2, 8, 22),
    ("Documents", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 0.05, 3, 14),
    ("Documents", "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 0.05, 4, 10),
    ("Photos", "jpg", "image/jpeg", 2, 9, 30),
    ("Photos", "png", "image/png", 0.5, 5, 8),
    ("Videos", "mp4", "video/mp4", 60, 900, 6),
    ("Projects", "zip", "application/zip", 10, 250, 7),
    ("Backups", "tar.gz", "application/gzip", 200, 1500, 2),
]
FOLDERS = ["Documents", "Photos", "Videos", "Projects", "Backups", "Shared"]
NAME_PREFIX = {"pdf": "Report", "docx": "Notes", "xlsx": "Budget", "jpg": "IMG", "png": "Screenshot",
               "mp4": "Recording", "zip": "Project", "tar.gz": "Backup"}
VERSIONED = {"pdf", "docx", "xlsx"}

# owner, folder, share type, share with, permission bitmask
SHARES = [
    ("alice", "Projects", "user", "bob", 31),
    ("alice", "Documents", "link", None, 1),
    ("bob", "Photos", "user", "carol", 17),
    ("carol", "Videos", "group", "staff", 1),
    ("admin", "Backups", "user", "alice", 1),
    ("dave", "Documents", "user", "alice", 3),
]

SCENARIOS = {
    "steady": {
        "label": "Steady growth",
        "description": "Usage grows at a constant ~150 MB/day.",
        "base_gb": 3.0,
        "rate_mb": lambda d: 150,
    },
    "rapid": {
        "label": "Rapid (accelerating) growth",
        "description": "Uploads accelerate ~4%/day, so capacity runs out within days.",
        "base_gb": 4.0,
        "rate_mb": lambda d: 80 * math.exp(0.04 * (d + HISTORY_DAYS)),
    },
    "spike": {
        "label": "Sudden spike",
        "description": "Slow growth, then a 2.4 GB bulk upload three days ago.",
        "base_gb": 3.5,
        "rate_mb": lambda d: 40 + (1200 if -3 <= d < -1 else 0),
    },
    "stable": {
        "label": "Stable usage",
        "description": "Almost no growth (~6 MB/day); no exhaustion expected.",
        "base_gb": 5.0,
        "rate_mb": lambda d: 6,
    },
}


class SimulatedNextcloud:
    mode = "demo"

    def __init__(self, scenario="steady", capacity_bytes=None, anchor=None, seed=42):
        if scenario not in SCENARIOS:
            scenario = "steady"
        self.scenario = scenario
        self.capacity = capacity_bytes or settings.capacity_bytes
        self.anchor = anchor or datetime.utcnow().replace(microsecond=0)
        self.real_anchor = datetime.utcnow()
        self.offset = timedelta(0)
        self.rng = random.Random(seed)
        self.exhausted_at = None
        self._ids = iter(range(1, 10 ** 9))
        self.quota_overrides = {}
        self._build()

    # ------------------------------------------------------------ clock
    def now(self):
        return (self.anchor + (datetime.utcnow() - self.real_anchor) + self.offset).replace(microsecond=0)

    def set_quota(self, uid, quota_bytes):
        self.quota_overrides[uid] = int(quota_bytes)

    def advance(self, days):
        self.offset += timedelta(days=days)

    @property
    def history_start(self):
        return self.anchor - timedelta(days=HISTORY_DAYS)

    # ------------------------------------------------------------ catalogue generation
    def _build(self):
        rng = self.rng
        prof = SCENARIOS[self.scenario]
        start = self.history_start
        self.folder_ids = {}
        for uid, *_ in USERS:
            self.folder_ids[(uid, "")] = next(self._ids)
            for f in FOLDERS:
                self.folder_ids[(uid, f)] = next(self._ids)
        self.catalog = []
        self.system_bytes = 0
        user_bytes = {u[0]: 0 for u in USERS}
        counters = {}

        def new_file(uid, t, max_mb, mtime=None):
            folder, ext, mime, lo, hi, _ = rng.choices(FILE_TYPES, weights=[ft[5] for ft in FILE_TYPES])[0]
            hi = max(min(hi, max_mb), lo)
            size = int(rng.uniform(lo, hi) * MB)
            # occasionally the same file is uploaded again by someone else (duplicate)
            if self.catalog and rng.random() < 0.05:
                src = rng.choice(self.catalog)
                if src["owner"] != uid and src["kind"] == "file":
                    return dict(src, id=next(self._ids), owner=uid, folder="Shared", created=t, mtime=t,
                                deleted_at=None)
            counters[(uid, ext)] = counters.get((uid, ext), 0) + 1
            name = f"{NAME_PREFIX[ext]}_{uid}_{counters[(uid, ext)]:04d}.{ext}"
            return {"id": next(self._ids), "owner": uid, "folder": folder, "name": name, "ext": ext,
                    "mimetype": mime, "size": size, "created": t, "mtime": mtime or t,
                    "deleted_at": None, "kind": "file"}

        def add(f, t):
            self.catalog.append(f)
            self.system_bytes += f["size"]
            user_bytes[f["owner"]] += f["size"]
            if rng.random() < 0.06:                          # later deleted -> trash bin
                f["deleted_at"] = t + timedelta(days=rng.uniform(0.5, 8))
            if f["ext"] in VERSIONED and rng.random() < 0.25:  # edited later -> old versions kept
                vt = t
                for _ in range(rng.randint(1, 3)):
                    vt += timedelta(days=rng.uniform(0.3, 3))
                    v = dict(f, id=next(self._ids), kind="version", created=vt,
                             size=int(f["size"] * rng.uniform(0.8, 1.1)), deleted_at=None)
                    self.catalog.append(v)
                    self.system_bytes += v["size"]

        # initial data present before the history window; some of it is very old
        base_total = prof["base_gb"] * 1024 * MB
        for uid, _, _, quota, _, base_share in USERS:
            target = base_total * base_share
            while user_bytes[uid] < target:
                age = rng.uniform(200, 900) if rng.random() < 0.35 else rng.uniform(1, 60)
                mtime = start - timedelta(days=age)
                add(new_file(uid, start - timedelta(days=1), 400, mtime), start - timedelta(days=1))

        # growth over the history window and into the future
        acc = {u[0]: 0.0 for u in USERS}
        pending = {}
        t = start
        end = self.anchor + timedelta(days=HORIZON_DAYS)
        while t < end:
            d = (t - self.anchor).total_seconds() / 86400
            rate = prof["rate_mb"](d)
            for uid, _, _, quota, weight, _ in USERS:
                daily = rate * weight
                acc[uid] += daily * MB / 24 * rng.uniform(0.5, 1.5)
                if uid not in pending:
                    pending[uid] = new_file(uid, t, max(3, daily * 2))
                f = pending[uid]
                if acc[uid] < f["size"]:
                    continue
                acc[uid] -= f["size"]
                del pending[uid]
                if self.system_bytes + f["size"] > self.capacity:
                    self.exhausted_at = self.exhausted_at or t      # upload rejected: disk full
                    continue
                if quota and user_bytes[uid] + f["size"] > quota:
                    continue                                         # upload rejected: quota exceeded
                f["created"] = f["mtime"] = t
                add(f, t)
            t += STEP

    # ------------------------------------------------------------ snapshot
    def snapshot(self, at=None):
        t = at or self.now()
        users, folders, files, areas = [], [], [], {}
        folder_size = {}
        for f in self.catalog:
            if f["created"] > t:
                continue
            owner = f["owner"]
            stamp = int(f["created"].timestamp())
            if f["kind"] == "version":
                area, path, parent = "versions", f"files_versions/{f['folder']}/{f['name']}.v{stamp}", None
            elif f["deleted_at"] and f["deleted_at"] <= t:
                area, path, parent = "trashbin", f"files_trashbin/files/{f['name']}.d{int(f['deleted_at'].timestamp())}", None
            else:
                area, path, parent = "files", f"files/{f['folder']}/{f['name']}", self.folder_ids[(owner, f["folder"])]
                folder_size[(owner, f["folder"])] = folder_size.get((owner, f["folder"]), 0) + f["size"]
            a = areas.setdefault(owner, {"files": 0, "trashbin": 0, "versions": 0})
            a[area] += f["size"]
            files.append({"id": f["id"], "owner": owner, "parent_id": parent, "path": path, "name": f["name"],
                          "mimetype": f["mimetype"], "size": f["size"], "mtime": f["mtime"], "area": area})

        for i, (uid, name, role, quota, _, _) in enumerate(USERS):
            used = areas.get(uid, {}).get("files", 0)
            users.append({"uid": uid, "display_name": name, "email": f"{uid}@cloud.local", "role": role,
                          "enabled": True, "last_login": t - timedelta(hours=3 + 7 * i),
                          "quota_bytes": self.quota_overrides.get(uid, quota), "used_bytes": used})
            root = self.folder_ids[(uid, "")]
            folders.append({"id": root, "owner": uid, "parent_id": None, "path": "files", "name": uid,
                            "size": used, "mtime": t})
            for fo in FOLDERS:
                folders.append({"id": self.folder_ids[(uid, fo)], "owner": uid, "parent_id": root,
                                "path": f"files/{fo}", "name": fo, "size": folder_size.get((uid, fo), 0),
                                "mtime": t})

        shares = []
        for i, (owner, folder, stype, with_, perms) in enumerate(SHARES, start=1):
            shares.append({"id": i, "owner": owner, "item_id": self.folder_ids[(owner, folder)],
                           "item_type": "folder", "item_path": f"/{folder}", "share_type": stype,
                           "share_with": with_, "permissions": perms, "created_at": self.history_start})
        return Snapshot(t, users, folders, files, shares, areas)

    def notify_admin(self, short, long=""):
        print(f"[notify] {short}")
