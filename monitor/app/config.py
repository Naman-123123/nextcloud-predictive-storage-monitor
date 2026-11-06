"""Runtime configuration, read from environment variables."""
import os

GB = 1024 ** 3
MB = 1024 ** 2


def _env(name, default):
    return os.environ.get(name, default)


def _float(name, default):
    return float(_env(name, default))


class Settings:
    # "demo" uses the built-in simulated Nextcloud; "nextcloud" talks to a real instance.
    mode = _env("MONITOR_MODE", "demo").lower()

    database_url = _env("DATABASE_URL", "sqlite:///./monitor.db")

    nextcloud_url = _env("NEXTCLOUD_URL", "http://localhost:8080").rstrip("/")
    nextcloud_admin_user = _env("NEXTCLOUD_ADMIN_USER", "admin")
    nextcloud_admin_password = _env("NEXTCLOUD_ADMIN_PASSWORD", "admin")
    # Optional read-only connection to Nextcloud's own database (file inventory, shares).
    nextcloud_db_url = _env("NEXTCLOUD_DB_URL", "")
    nextcloud_table_prefix = _env("NEXTCLOUD_TABLE_PREFIX", "oc_")
    # Public URL of Nextcloud shown as a link on the dashboard.
    nextcloud_public_url = _env("NEXTCLOUD_PUBLIC_URL", "http://localhost:8080")
    notify_nextcloud = _env("NOTIFY_NEXTCLOUD", "true").lower() == "true"
    # Self-service portal URL put into the users' "storage almost full" notifications.
    monitor_public_url = _env("MONITOR_PUBLIC_URL", "http://localhost:8000").rstrip("/")

    # Total storage provisioned for the private cloud (the pool being forecast).
    capacity_bytes = int(_float("STORAGE_CAPACITY_GB", "10") * GB)
    # Upper bound for pool resizing (the physical disk / cloud volume limit).
    pool_max_bytes = int(_float("POOL_MAX_GB", "5") * GB)
    # Chargeback: price of one GB-month of storage, shown per user.
    cost_per_gb_month = _float("COST_PER_GB_MONTH", "2.0")
    currency = _env("CURRENCY", "Rs.")

    collect_interval = int(_float("COLLECT_INTERVAL_SECONDS", "60"))
    prediction_window_days = _float("PREDICTION_WINDOW_DAYS", "7")
    # A live demo grows over minutes, not days: PREDICTION_WINDOW_MINUTES overrides the window.
    if _env("PREDICTION_WINDOW_MINUTES", ""):
        prediction_window_days = _float("PREDICTION_WINDOW_MINUTES", "0") / 1440
    min_samples = int(_float("PREDICTION_MIN_SAMPLES", "3"))

    util_warn = _float("ALERT_UTIL_WARN", "80")
    util_crit = _float("ALERT_UTIL_CRIT", "90")
    days_warn = _float("ALERT_DAYS_WARN", "14")
    days_crit = _float("ALERT_DAYS_CRIT", "3")

    large_file_bytes = int(_float("LARGE_FILE_MB", "100") * MB)
    old_file_days = int(_float("OLD_FILE_DAYS", "180"))
    old_files_min_bytes = int(_float("OLD_FILES_MIN_MB", "100") * MB)
    trash_min_bytes = int(_float("TRASH_MIN_MB", "50") * MB)
    versions_min_bytes = int(_float("VERSIONS_MIN_MB", "50") * MB)
    capacity_planning_days = int(_float("CAPACITY_PLANNING_DAYS", "30"))

    demo_scenario = _env("DEMO_SCENARIO", "steady")


settings = Settings()
