"""Generate realistic storage activity on a real Nextcloud instance.

  python scripts/generate_load.py --setup                  # users, quotas, folders, shares, initial files
  python scripts/generate_load.py --scenario rapid         # then keep uploading (Ctrl+C to stop)

Scenarios (MB uploaded per tick, split across users):
  steady : constant ~15 MB/tick
  rapid  : starts at 10 MB/tick and grows 4% every tick
  spike  : ~3 MB/tick with a bulk upload of ~120 MB/tick between ticks 15 and 20
  stable : ~0.5 MB/tick

Side effects that the optimizer should detect are produced on purpose:
files are deleted (trash bin), documents are re-uploaded (old versions),
the same file is uploaded by two users (duplicates) and some files are
back-dated by 1-2 years (cold data).
"""
import argparse
import os
import random
import sys
import time

import httpx

PASSWORD = "Demo@Cloud2026!x"
USERS = {  # uid: (display name, quota, share of uploads)
    "alice": ("Alice Sharma", "1 GB", 0.40),
    "bob": ("Bob Mathew", "750 MB", 0.25),
    "carol": ("Carol Dsouza", "none", 0.25),
    "dave": ("Dave Rao", "300 MB", 0.10),
}
FOLDERS = ["Documents", "Photos", "Videos", "Projects", "Backups"]
KINDS = [("Documents", "pdf", 0.3, 6), ("Documents", "docx", 0.1, 2), ("Photos", "jpg", 2, 8),
         ("Videos", "mp4", 20, 120), ("Projects", "zip", 5, 60)]
SHARES = [("alice", "/Projects", 0, "bob", 31), ("alice", "/Documents", 3, None, 1),
          ("bob", "/Photos", 0, "carol", 17), ("dave", "/Documents", 0, "alice", 3)]
MB = 1024 * 1024


class Cloud:
    def __init__(self, url, admin, password):
        self.url = url.rstrip("/")
        self.admin = (admin, password)
        self.uploaded = {u: [] for u in USERS}
        self.counter = 0
        self.uploaded_bytes = 0

    def ocs(self, method, endpoint, data=None, auth=None):
        r = httpx.request(method, f"{self.url}{endpoint}", params={"format": "json"}, data=data,
                          auth=auth or self.admin, headers={"OCS-APIRequest": "true"}, timeout=60)
        try:
            return r.json()["ocs"]["meta"]
        except Exception:  # noqa: BLE001
            return {"statuscode": r.status_code, "message": r.text[:200]}

    def dav(self, method, uid, path, content=None, headers=None):
        return httpx.request(method, f"{self.url}/remote.php/dav/files/{uid}/{path.lstrip('/')}",
                             content=content, auth=(uid, PASSWORD), headers=headers or {}, timeout=300)

    # ------------------------------------------------------------ setup
    def setup(self):
        for uid, (name, quota, _) in USERS.items():
            meta = self.ocs("POST", "/ocs/v1.php/cloud/users", {"userid": uid, "password": PASSWORD,
                            "displayName": name, "email": f"{uid}@cloud.local"})
            status = "created" if meta["statuscode"] == 100 else "exists" if meta["statuscode"] == 102 else meta
            self.ocs("PUT", f"/ocs/v1.php/cloud/users/{uid}", {"key": "quota", "value": quota})
            print(f"user {uid:6} {status}, quota {quota}")
            for folder in FOLDERS:
                self.dav("MKCOL", uid, folder)
        for owner, path, stype, with_, perms in SHARES:
            data = {"path": path, "shareType": stype, "permissions": perms}
            if with_:
                data["shareWith"] = with_
            meta = self.ocs("POST", "/ocs/v2.php/apps/files_sharing/api/v1/shares", data, auth=(owner, PASSWORD))
            print(f"share {owner}:{path} -> {with_ or 'public link'}: {meta.get('status', meta.get('statuscode'))}")
        # initial data, partly back-dated so it shows up as cold data
        for uid in USERS:
            for _ in range(6):
                age_days = random.uniform(200, 700) if random.random() < 0.5 else random.uniform(1, 30)
                self.upload(uid, mtime=time.time() - age_days * 86400)
        self.upload("carol", kind=("Videos", "mp4", 150, 160), mtime=time.time() - 400 * 86400)

    # ------------------------------------------------------------ activity
    def upload(self, uid, size=None, kind=None, mtime=None):
        folder, ext, lo, hi = kind or random.choice(KINDS)
        size = int(size or random.uniform(lo, hi) * MB)
        self.counter += 1
        path = f"{folder}/{folder[:-1] if folder.endswith('s') else folder}_{int(time.time())}_{self.counter}.{ext}"
        headers = {"X-OC-Mtime": str(int(mtime))} if mtime else {}
        r = self.dav("PUT", uid, path, os.urandom(size), headers)
        ok = r.status_code in (201, 204)
        if ok:
            self.uploaded[uid].append((path, size))
            self.uploaded_bytes += size
        print(f"  {'+' if ok else '!'} {uid:6} {path:45} {size / MB:7.1f} MB"
              f"{'' if ok else f'  (HTTP {r.status_code}: quota or disk full)'}")
        return ok

    def side_effects(self, uid):
        files = self.uploaded[uid]
        if files and random.random() < 0.08:                       # delete -> trash bin
            path, size = files.pop(random.randrange(len(files)))
            self.dav("DELETE", uid, path)
            print(f"  - {uid:6} deleted {path} (moves to trash)")
        docs = [f for f in files if f[0].endswith((".pdf", ".docx"))]
        if docs and random.random() < 0.15:                        # overwrite -> old version kept
            path, size = random.choice(docs)
            self.dav("PUT", uid, path, os.urandom(size))
            print(f"  ~ {uid:6} edited {path} (new version)")
        if files and random.random() < 0.04:                       # same file uploaded by someone else
            other = random.choice([u for u in USERS if u != uid])
            path, size = random.choice(files)
            content = self.dav("GET", uid, path).content
            self.dav("PUT", other, f"Documents/{path.split('/')[-1]}", content)
            print(f"  = {other:6} uploaded a duplicate of {path}")


def mb_for_tick(scenario, tick):
    if scenario == "rapid":
        return 10 * 1.04 ** tick
    if scenario == "spike":
        return 120 if 15 <= tick < 20 else 3
    if scenario == "stable":
        return 0.5
    return 15


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--admin", default="admin")
    ap.add_argument("--password", default="Admin@Cloud2026")
    ap.add_argument("--setup", action="store_true", help="create users, quotas, folders, shares and initial files")
    ap.add_argument("--scenario", choices=["steady", "rapid", "spike", "stable"])
    ap.add_argument("--interval", type=float, default=10, help="seconds between ticks")
    ap.add_argument("--ticks", type=int, default=0, help="stop after N ticks (0 = run until Ctrl+C)")
    ap.add_argument("--max-gb", type=float, default=1.2,
                    help="safety stop: total GB this run may upload, protects the local disk (default 1.2)")
    args = ap.parse_args()

    cloud = Cloud(args.url, args.admin, args.password)
    try:
        httpx.get(f"{cloud.url}/status.php", timeout=10).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Nextcloud is not reachable at {cloud.url}: {exc}")

    if args.setup:
        cloud.setup()
    if not args.scenario:
        return
    tick = 0
    try:
        while not args.ticks or tick < args.ticks:
            if cloud.uploaded_bytes >= args.max_gb * 1024 ** 3:
                print(f"reached --max-gb {args.max_gb} GB uploaded in this run, stopping to protect the disk")
                break
            total = mb_for_tick(args.scenario, tick) * random.uniform(0.7, 1.3)
            print(f"tick {tick} [{args.scenario}] ~{total:.1f} MB")
            for uid, (_, _, weight) in USERS.items():
                budget = total * weight
                while budget > 0.05:
                    size = min(budget, random.uniform(0.3, max(budget, 0.5)))
                    cloud.upload(uid, size=size * MB)
                    budget -= size
                cloud.side_effects(uid)
            tick += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
