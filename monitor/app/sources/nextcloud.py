"""Reads storage state from a real Nextcloud instance.

* Users, roles and quotas come from the OCS Provisioning API (admin credentials).
* The file/folder inventory, trash bin, versions and shares are read (SELECT only)
  from Nextcloud's own database tables: oc_filecache, oc_storages, oc_mimetypes, oc_share.
"""
from datetime import datetime

import httpx
from sqlalchemy import create_engine, text

from ..config import settings
from . import Snapshot

SHARE_TYPES = {0: "user", 1: "group", 3: "link", 4: "email", 6: "federated", 10: "room"}
DIR_MIME = "httpd/unix-directory"


def _owner_from_storage(storage_id):
    for prefix in ("home::", "object::user:"):
        if storage_id.startswith(prefix):
            return storage_id[len(prefix):]
    return None


def _area(path):
    if path.startswith("files/") or path == "files":
        return "files"
    if path == "files_trashbin" or path.startswith("files_trashbin/files"):
        return "trashbin"
    if path.startswith("files_versions"):
        return "versions"
    return None


def _ts(value):
    return datetime.utcfromtimestamp(int(value)) if value else None


class NextcloudSource:
    mode = "nextcloud"

    def __init__(self):
        self.client = httpx.Client(
            base_url=settings.nextcloud_url,
            auth=(settings.nextcloud_admin_user, settings.nextcloud_admin_password),
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            timeout=30,
        )
        self.db = create_engine(settings.nextcloud_db_url, pool_pre_ping=True) if settings.nextcloud_db_url else None
        self.p = settings.nextcloud_table_prefix

    def now(self):
        return datetime.utcnow().replace(microsecond=0)

    # ------------------------------------------------------------ OCS API
    def _ocs(self, path):
        r = self.client.get(path, params={"format": "json"})
        r.raise_for_status()
        return r.json()["ocs"]["data"]

    def _users(self):
        users = []
        for uid in self._ocs("/ocs/v1.php/cloud/users")["users"]:
            d = self._ocs(f"/ocs/v1.php/cloud/users/{uid}")
            quota = d.get("quota") or {}
            q = quota.get("quota")
            last_login = d.get("lastLogin") or 0
            users.append({
                "uid": uid,
                "display_name": d.get("displayname") or d.get("display-name") or uid,
                "email": d.get("email"),
                "role": "admin" if "admin" in (d.get("groups") or []) else "user",
                "enabled": bool(d.get("enabled", True)),
                "last_login": datetime.utcfromtimestamp(last_login / 1000) if last_login else None,
                "quota_bytes": int(q) if isinstance(q, (int, float)) and q >= 0 else None,
                "used_bytes": int(quota.get("used") or 0),
            })
        return users

    def notify_user(self, uid, short, long=""):
        """Push a message into the Nextcloud notification bell of any user."""
        r = self.client.post(
            f"/ocs/v2.php/apps/notifications/api/v2/admin_notifications/{uid}",
            params={"format": "json"},
            data={"shortMessage": short[:255], "longMessage": long[:4000]},
        )
        r.raise_for_status()

    def notify_admin(self, short, long=""):
        self.notify_user(settings.nextcloud_admin_user, short, long)

    def set_quota(self, uid, quota_bytes):
        """Change a user's storage quota in Nextcloud (OCS Provisioning API).

        Uses a fresh login without the long-lived session cookie: Nextcloud demands a password
        confirmation younger than 30 minutes for user administration, and a new login counts as one.
        """
        r = httpx.put(f"{settings.nextcloud_url}/ocs/v1.php/cloud/users/{uid}", params={"format": "json"},
                      data={"key": "quota", "value": str(int(quota_bytes))},
                      auth=(settings.nextcloud_admin_user, settings.nextcloud_admin_password),
                      headers={"OCS-APIRequest": "true", "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        meta = r.json()["ocs"]["meta"]
        if meta.get("statuscode") not in (100, 200):
            raise RuntimeError(f"Nextcloud refused the quota change: {meta.get('message')}")

    # ------------------------------------------------------------ database inventory
    def _inventory(self):
        p = self.p
        rows = self.db.connect()
        try:
            entries = rows.execute(text(f"""
                SELECT fc.fileid, fc.parent, fc.path, fc.name, fc.size, fc.mtime, mt.mimetype, s.id AS storage_id
                FROM {p}filecache fc
                JOIN {p}storages s ON fc.storage = s.numeric_id
                JOIN {p}mimetypes mt ON fc.mimetype = mt.id
                WHERE (s.id LIKE 'home::%' OR s.id LIKE 'object::user:%')
                  AND (fc.path = 'files' OR fc.path LIKE 'files/%'
                       OR fc.path = 'files_trashbin' OR fc.path LIKE 'files_trashbin/files/%'
                       OR fc.path = 'files_versions' OR fc.path LIKE 'files_versions/%')
            """)).mappings().all()
            share_rows = rows.execute(text(f"""
                SELECT sh.id, sh.share_type, sh.share_with, sh.uid_owner, sh.item_type, sh.file_source,
                       sh.permissions, sh.stime, sh.file_target
                FROM {p}share sh
            """)).mappings().all()
        finally:
            rows.close()

        folders, files, areas = [], [], {}
        for e in entries:
            owner = _owner_from_storage(e["storage_id"])
            area = _area(e["path"])
            if not owner or not area:
                continue
            size = max(int(e["size"] or 0), 0)
            if e["path"] in ("files", "files_trashbin", "files_versions"):
                areas.setdefault(owner, {"files": 0, "trashbin": 0, "versions": 0})[area] = size
            if e["mimetype"] == DIR_MIME:
                if area == "files":
                    folders.append({"id": e["fileid"], "owner": owner, "parent_id": e["parent"],
                                    "path": e["path"], "name": e["name"] or owner, "size": size,
                                    "mtime": _ts(e["mtime"])})
                continue
            files.append({"id": e["fileid"], "owner": owner, "parent_id": e["parent"], "path": e["path"],
                          "name": e["name"], "mimetype": e["mimetype"], "size": size,
                          "mtime": _ts(e["mtime"]), "area": area})

        shares = [{
            "id": s["id"], "owner": s["uid_owner"], "item_id": s["file_source"], "item_type": s["item_type"],
            "item_path": s["file_target"], "share_type": SHARE_TYPES.get(s["share_type"], str(s["share_type"])),
            "share_with": s["share_with"], "permissions": int(s["permissions"] or 0), "created_at": _ts(s["stime"]),
        } for s in share_rows]
        return folders, files, shares, areas

    def snapshot(self):
        users = self._users()
        if not self.db:
            return Snapshot(self.now(), users, inventory_available=False)
        folders, files, shares, areas = self._inventory()
        return Snapshot(self.now(), users, folders, files, shares, areas)
