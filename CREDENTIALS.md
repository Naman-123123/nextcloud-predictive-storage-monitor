# Demo logins

These are the default accounts of the **local demo**. They are public on purpose so anyone can run the
project; change them in `.env` (copy `.env.example`) before exposing the stack to a network.

## Web logins

| Where | URL | Username | Password |
|---|---|---|---|
| Nextcloud (administrator) | http://localhost:8080 | `admin` | `Admin@Cloud2026` |
| Monitoring dashboard | http://localhost:8000 | – (no login) | – |
| User self-service portal | http://localhost:8000/me | any demo user below | their Nextcloud password |

## Demo users

Created by `python scripts/generate_load.py --setup`. All of them share the same password.

| Username | Display name | Quota | Password |
|---|---|---|---|
| `alice` | Alice Sharma | 1 GB | `Demo@Cloud2026!x` |
| `bob` | Bob Mathew | 750 MB | `Demo@Cloud2026!x` |
| `carol` | Carol Dsouza | unlimited | `Demo@Cloud2026!x` |
| `dave` | Dave Rao | 300 MB | `Demo@Cloud2026!x` |

## Database (MariaDB, internal to Docker)

| Account | Database | Password (`.env` variable) |
|---|---|---|
| `root` | all | `rootpass` (`DB_ROOT_PASSWORD`) |
| `nextcloud` | `nextcloud` | `nextcloudpass` (`NEXTCLOUD_DB_PASSWORD`) |
| `monitor` | `monitor` (read-only on `nextcloud`) | `monitorpass` (`MONITOR_DB_PASSWORD`) |

The database port is not published outside Docker.
