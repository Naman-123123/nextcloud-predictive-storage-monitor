def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_days(d):
    if d is None:
        return "n/a"
    if d < 1 / 24:
        return f"{max(d * 1440, 1):.0f} minutes"
    if d < 1:
        return f"{d * 24:.1f} hours"
    return f"{d:.1f} days"


def iso(dt):
    return dt.isoformat() + "Z" if dt else None
