"""Upload compressible demo files (server logs, sensor CSV) so the "compress" recommendation has
something to act on. The load generator writes random bytes, which no compressor can shrink.

    python scripts/seed_compressible.py                 # dave: 30 MB log, alice: 20 MB CSV
    python scripts/seed_compressible.py --user bob --mb 40 --kind log
"""
import argparse
import random
import time
from datetime import datetime, timedelta

import httpx

PASSWORD = "Demo@Cloud2026!x"


def log_lines(n_bytes):
    rng = random.Random(7)
    t = datetime(2026, 10, 1)
    paths = ["/index.php/apps/files", "/remote.php/dav/files", "/ocs/v2.php/apps/notifications", "/status.php"]
    out, size = [], 0
    while size < n_bytes:
        t += timedelta(seconds=rng.randint(1, 20))
        line = (f'10.0.{rng.randint(0, 9)}.{rng.randint(2, 254)} - - [{t:%d/%b/%Y:%H:%M:%S} +0000] '
                f'"{rng.choice(["GET", "PROPFIND", "PUT"])} {rng.choice(paths)} HTTP/1.1" '
                f'{rng.choice([200, 200, 200, 207, 304, 404])} {rng.randint(200, 90000)} "-" "Mozilla/5.0"\n')
        out.append(line)
        size += len(line)
    return "".join(out).encode()


def csv_rows(n_bytes):
    rng = random.Random(11)
    out, size, t = ["timestamp,sensor,temperature_c,humidity_pct,status\n"], 0, datetime(2026, 9, 1)
    while size < n_bytes:
        t += timedelta(seconds=30)
        row = f"{t:%Y-%m-%d %H:%M:%S},S{rng.randint(1, 40):02d},{rng.uniform(18, 35):.2f},{rng.uniform(30, 90):.1f},OK\n"
        out.append(row)
        size += len(row)
    return "".join(out).encode()


def upload(url, user, kind, mb):
    data = log_lines(mb * 1024 ** 2) if kind == "log" else csv_rows(mb * 1024 ** 2)
    name = f"server_access_{int(time.time())}.log" if kind == "log" else f"sensor_data_{int(time.time())}.csv"
    r = httpx.put(f"{url}/remote.php/dav/files/{user}/Documents/{name}", content=data,
                  auth=(user, PASSWORD), timeout=120)
    print(f"{user}: Documents/{name} ({len(data) / 1024 ** 2:.1f} MB) -> HTTP {r.status_code}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--user")
    ap.add_argument("--kind", choices=["log", "csv"], default="log")
    ap.add_argument("--mb", type=int, default=30)
    a = ap.parse_args()
    if a.user:
        upload(a.url, a.user, a.kind, a.mb)
    else:
        upload(a.url, "dave", "log", 30)
        upload(a.url, "alice", "csv", 20)


if __name__ == "__main__":
    main()
