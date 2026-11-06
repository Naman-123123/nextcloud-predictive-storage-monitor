"""Data sources the collector can read from.

Every source returns a Snapshot describing the cloud at one point in time:

  users   : [{uid, display_name, email, role, enabled, last_login, quota_bytes, used_bytes}]
  folders : [{id, owner, parent_id, path, name, size, mtime}]
  files   : [{id, owner, parent_id, path, name, mimetype, size, mtime, area}]
  shares  : [{id, owner, item_id, item_type, item_path, share_type, share_with, permissions, created_at}]
  areas   : {uid: {"files": bytes, "trashbin": bytes, "versions": bytes}}
"""
from dataclasses import dataclass, field


@dataclass
class Snapshot:
    taken_at: object
    users: list
    folders: list = field(default_factory=list)
    files: list = field(default_factory=list)
    shares: list = field(default_factory=list)
    areas: dict = field(default_factory=dict)
    inventory_available: bool = True


def get_source():
    from ..config import settings
    if settings.mode == "nextcloud":
        from .nextcloud import NextcloudSource
        return NextcloudSource()
    from .simulator import SimulatedNextcloud
    return SimulatedNextcloud(settings.demo_scenario)
