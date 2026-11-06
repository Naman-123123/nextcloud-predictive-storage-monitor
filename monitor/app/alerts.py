"""Threshold and predictive alerts.

Each cycle computes the set of conditions that currently hold. An existing open
alert for the same (scope, user, type) is updated; a new one is created (and the
administrator notified); open alerts whose condition no longer holds are resolved.
"""
from sqlalchemy import select

from .config import settings
from .db import Alert
from .util import fmt_bytes, fmt_days


def _severity(value, warn, crit, higher_is_worse=True):
    if value is None:
        return None
    if higher_is_worse:
        return "critical" if value >= crit else "warning" if value >= warn else None
    return "critical" if value <= crit else "warning" if value <= warn else None


def _conditions(storage, users, preds):
    cond = {}
    cap, used = storage["capacity_bytes"], storage["used_bytes"]
    pct = used / cap * 100 if cap else 0
    sev = _severity(pct, settings.util_warn, settings.util_crit)
    if sev:
        cond[("system", None, "utilization")] = (sev, pct,
            f"Storage pool is {pct:.1f}% full ({fmt_bytes(used)} of {fmt_bytes(cap)}).")
    p = preds.get(("system", None))
    if p:
        sev = _severity(p["days_remaining"], settings.days_warn, settings.days_crit, higher_is_worse=False)
        if sev:
            when = p["predicted_full_at"].strftime("%d %b %Y %H:%M") if p["predicted_full_at"] else "now"
            cond[("system", None, "exhaustion")] = (sev, p["days_remaining"],
                f"At {fmt_bytes(p['growth_bytes_per_day'])}/day growth, the storage pool will be full in "
                f"~{fmt_days(p['days_remaining'])} ({when} UTC).")

    for u in users:
        quota = u["quota_bytes"]
        if not quota:
            continue
        pct = u["used_bytes"] / quota * 100
        sev = _severity(pct, settings.util_warn, settings.util_crit)
        if sev:
            cond[("user", u["uid"], "quota_utilization")] = (sev, pct,
                f"User {u['uid']} has used {pct:.1f}% of their {fmt_bytes(quota)} quota.")
        p = preds.get(("user", u["uid"]))
        if p:
            sev = _severity(p["days_remaining"], settings.days_warn, settings.days_crit, higher_is_worse=False)
            if sev:
                cond[("user", u["uid"], "quota_exhaustion")] = (sev, p["days_remaining"],
                    f"User {u['uid']} will exceed their quota in ~{fmt_days(p['days_remaining'])} "
                    f"(growing {fmt_bytes(p['growth_bytes_per_day'])}/day).")
    return cond


def _notify_owner(notify_user, a, users):
    """Tell the user themselves (Nextcloud bell) that their storage is almost full."""
    u = next((u for u in users if u["uid"] == a.user_id), None)
    if not u or not u["quota_bytes"]:
        return
    pct = u["used_bytes"] / u["quota_bytes"] * 100
    notify_user(a.user_id,
                f"Your storage is almost full: {pct:.0f}% of {fmt_bytes(u['quota_bytes'])} used",
                f"Open {settings.monitor_public_url}/me (log in with your Nextcloud account) to see how to free "
                f"space - compress files, empty your trash bin - or to ask the administrator for more storage.")


def evaluate(session, now, storage, users, preds, notify=None, notify_user=None):
    cond = _conditions(storage, users, preds)
    open_alerts = session.scalars(select(Alert).where(Alert.resolved_at.is_(None))).all()
    by_key = {(a.scope, a.user_id, a.alert_type): a for a in open_alerts}
    created = []

    for key, (sev, value, msg) in cond.items():
        a = by_key.pop(key, None)
        if a:
            escalated = a.severity == "warning" and sev == "critical"
            a.severity, a.value, a.message, a.updated_at = sev, value, msg, now
            if escalated:
                created.append(a)
        else:
            a = Alert(scope=key[0], user_id=key[1], alert_type=key[2], severity=sev, value=value,
                      message=msg, created_at=now, updated_at=now)
            session.add(a)
            created.append(a)

    for a in by_key.values():       # condition cleared
        a.resolved_at = now

    if notify:
        for a in created:
            try:
                notify(f"[{a.severity.upper()}] {a.message}", "Raised by the predictive storage monitor.")
                a.notified = True
            except Exception as exc:  # noqa: BLE001 - notification is best-effort
                print(f"[alerts] notification failed: {exc}")
    if notify_user:
        for a in created:
            if a.alert_type == "quota_utilization":
                try:
                    _notify_owner(notify_user, a, users)
                except Exception as exc:  # noqa: BLE001
                    print(f"[alerts] user notification failed: {exc}")
    return created
