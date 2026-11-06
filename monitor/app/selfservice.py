"""User self-service actions, performed in Nextcloud with the user's own credentials (WebDAV).

  login        - verify Nextcloud credentials (OCS /cloud/user) and open a portal session
  compress     - zip a file, upload the archive, delete the original and purge it from the trash bin
  delete_file  - delete a file and purge it from the trash bin (cold-data clean-up)
  empty_trash  - permanently delete everything in the user's trash bin
"""
import io
import posixpath
import secrets
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import quote, unquote

import httpx

from .config import settings

SESSIONS = {}                     # token -> (uid, password); in memory, cleared on restart
MIN_GAIN = 0.05                   # keep the original unless compression saves at least 5%
NS = {"d": "DAV:", "nc": "http://nextcloud.org/ns"}
TRASH_PROPFIND = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">
  <d:prop><nc:trashbin-original-location/><nc:trashbin-deletion-time/></d:prop>
</d:propfind>"""


def _client(uid, password):
    return httpx.Client(base_url=settings.nextcloud_url, auth=(uid, password),
                        headers={"OCS-APIRequest": "true"}, timeout=300)


def login(user, password):
    with _client(user, password) as c:
        r = c.get("/ocs/v1.php/cloud/user", params={"format": "json"}, headers={"Accept": "application/json"})
    if r.status_code != 200 or r.json()["ocs"]["meta"]["statuscode"] != 100:
        return None
    uid = r.json()["ocs"]["data"]["id"]
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = (uid, password)
    return token, uid


def session(token):
    return SESSIONS.get(token or "")


def _dav(uid, rel):
    return f"/remote.php/dav/files/{quote(uid)}/{quote(rel)}"


def _rel(path):
    """Nextcloud filecache path 'files/Documents/a.txt' -> 'Documents/a.txt'."""
    return path[len("files/"):] if path.startswith("files/") else path


def _purge_from_trash(c, uid, rel):
    r = c.request("PROPFIND", f"/remote.php/dav/trashbin/{quote(uid)}/trash", content=TRASH_PROPFIND,
                  headers={"Depth": "1", "Content-Type": "application/xml"})
    r.raise_for_status()
    best = None
    for resp in ET.fromstring(r.content).findall("d:response", NS):
        loc = resp.find(".//nc:trashbin-original-location", NS)
        when = resp.find(".//nc:trashbin-deletion-time", NS)
        if loc is not None and unquote(loc.text or "") == rel:
            t = int(when.text or 0) if when is not None else 0
            if not best or t > best[0]:
                best = (t, resp.find("d:href", NS).text)
    if best:
        c.request("DELETE", best[1]).raise_for_status()
    return bool(best)


def compress(uid, password, path):
    """Replace a file by a ZIP archive of it when that actually saves space."""
    rel = _rel(path)
    with _client(uid, password) as c:
        r = c.get(_dav(uid, rel))
        r.raise_for_status()
        raw = r.content
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.writestr(posixpath.basename(rel), raw)
        before, after = len(raw), buf.tell()
        if before == 0 or after > before * (1 - MIN_GAIN):
            return {"compressed": False, "before": before, "after": after,
                    "message": f"'{posixpath.basename(rel)}' is already compressed (JPEG/MP4/ZIP/random data); "
                               f"zipping would save only {max(before - after, 0) * 100 / max(before, 1):.1f}%, "
                               f"so the original was kept."}
        c.put(_dav(uid, rel + ".zip"), content=buf.getvalue()).raise_for_status()
        c.request("DELETE", _dav(uid, rel)).raise_for_status()
        purged = _purge_from_trash(c, uid, rel)
    return {"compressed": True, "before": before, "after": after, "target": rel + ".zip", "purged": purged,
            "message": f"Compressed '{posixpath.basename(rel)}' to {posixpath.basename(rel)}.zip "
                       f"({(before - after) * 100 / before:.0f}% smaller)."}


def delete_file(uid, password, path):
    """Delete a file for good: move it to the trash bin, then purge it from there too."""
    rel = _rel(path)
    with _client(uid, password) as c:
        c.request("DELETE", _dav(uid, rel)).raise_for_status()
        _purge_from_trash(c, uid, rel)


def empty_trash(uid, password):
    with _client(uid, password) as c:
        c.request("DELETE", f"/remote.php/dav/trashbin/{quote(uid)}/trash").raise_for_status()
