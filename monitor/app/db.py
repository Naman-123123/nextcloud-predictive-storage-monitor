"""Database schema for the monitoring layer (14 entities) and session helpers."""
import time
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import settings

Base = declarative_base()


# ---------------------------------------------------------------- inventory (mirrored from Nextcloud)

class User(Base):
    __tablename__ = "users"
    uid = Column(String(64), primary_key=True)
    display_name = Column(String(255))
    email = Column(String(255))
    role = Column(String(16), default="user")          # admin | user
    enabled = Column(Boolean, default=True)
    last_login = Column(DateTime, nullable=True)
    synced_at = Column(DateTime)
    quota = relationship("Quota", uselist=False, back_populates="user", cascade="all, delete-orphan")


class Quota(Base):
    __tablename__ = "quotas"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), ForeignKey("users.uid"), unique=True)
    quota_bytes = Column(BigInteger, nullable=True)     # NULL = unlimited
    used_bytes = Column(BigInteger, default=0)
    updated_at = Column(DateTime)
    user = relationship("User", back_populates="quota")


class Storage(Base):
    __tablename__ = "storage"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True)
    capacity_bytes = Column(BigInteger)
    used_bytes = Column(BigInteger, default=0)
    files_bytes = Column(BigInteger, default=0)
    trash_bytes = Column(BigInteger, default=0)
    versions_bytes = Column(BigInteger, default=0)
    file_count = Column(Integer, default=0)
    updated_at = Column(DateTime)


class Folder(Base):
    __tablename__ = "folders"
    id = Column(BigInteger, primary_key=True, autoincrement=False)   # Nextcloud fileid
    owner_id = Column(String(64), index=True)
    parent_id = Column(BigInteger, nullable=True)
    path = Column(String(1000))
    name = Column(String(255))
    size_bytes = Column(BigInteger, default=0)
    mtime = Column(DateTime, nullable=True)


class File(Base):
    __tablename__ = "files"
    id = Column(BigInteger, primary_key=True, autoincrement=False)   # Nextcloud fileid
    owner_id = Column(String(64), index=True)
    folder_id = Column(BigInteger, nullable=True)
    path = Column(String(1000))
    name = Column(String(255))
    mimetype = Column(String(255))
    size_bytes = Column(BigInteger, default=0)
    mtime = Column(DateTime, nullable=True)
    area = Column(String(16), default="files")          # files | trashbin | versions


class Share(Base):
    __tablename__ = "shares"
    id = Column(BigInteger, primary_key=True, autoincrement=False)   # Nextcloud share id
    owner_id = Column(String(64))
    item_id = Column(BigInteger)
    item_type = Column(String(16))                      # file | folder
    item_path = Column(String(1000))
    share_type = Column(String(16))                     # user | group | link | email | federated
    share_with = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=True)
    permission = relationship("Permission", uselist=False, back_populates="share", cascade="all, delete-orphan")


class Permission(Base):
    __tablename__ = "permissions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    share_id = Column(BigInteger, ForeignKey("shares.id"), unique=True)
    bitmask = Column(Integer)
    can_read = Column(Boolean)
    can_update = Column(Boolean)
    can_create = Column(Boolean)
    can_delete = Column(Boolean)
    can_share = Column(Boolean)
    share = relationship("Share", back_populates="permission")


# ---------------------------------------------------------------- monitoring layer

class StorageUsageRecord(Base):
    __tablename__ = "storage_usage_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    recorded_at = Column(DateTime, index=True)
    scope = Column(String(16), index=True)              # system | user
    user_id = Column(String(64), nullable=True, index=True)
    used_bytes = Column(BigInteger)
    capacity_bytes = Column(BigInteger, nullable=True)  # pool capacity or user quota
    file_count = Column(Integer, nullable=True)


class PredictionRecord(Base):
    __tablename__ = "prediction_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, index=True)
    scope = Column(String(16))
    user_id = Column(String(64), nullable=True)
    method = Column(String(32))
    window_days = Column(Float)
    sample_count = Column(Integer)
    used_bytes = Column(BigInteger)
    capacity_bytes = Column(BigInteger, nullable=True)
    growth_bytes_per_day = Column(Float)
    r_squared = Column(Float, nullable=True)
    days_remaining = Column(Float, nullable=True)       # NULL = not growing / no limit
    predicted_full_at = Column(DateTime, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(16))
    user_id = Column(String(64), nullable=True)
    alert_type = Column(String(32))     # utilization | exhaustion | quota_utilization | quota_exhaustion
    severity = Column(String(16))       # warning | critical
    message = Column(String(500))
    value = Column(Float, nullable=True)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    resolved_at = Column(DateTime, nullable=True)
    notified = Column(Boolean, default=False)


class OptimizationRecommendation(Base):
    __tablename__ = "optimization_recommendations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    rec_key = Column(String(255), index=True)
    category = Column(String(32))       # large_file | duplicate | trash | versions | old_files | quota | capacity
    priority = Column(Integer)          # 1 = high
    target_user = Column(String(64), nullable=True)
    target_path = Column(String(1000), nullable=True)
    message = Column(String(1000))
    potential_savings_bytes = Column(BigInteger, default=0)
    created_at = Column(DateTime)
    status = Column(String(16), default="open")          # open | dismissed


# ---------------------------------------------------------------- elastic scaling & self-service

class PoolConfig(Base):
    """Provisioned size of the storage pool, resizable at runtime (manually or by the autoscaler)."""
    __tablename__ = "pool_config"
    id = Column(Integer, primary_key=True, autoincrement=True)
    capacity_bytes = Column(BigInteger)
    max_capacity_bytes = Column(BigInteger)
    autoscale = Column(Boolean, default=False)
    autoscale_at_pct = Column(Float, default=85)        # grow when the pool is this full
    autoscale_step_pct = Column(Float, default=25)      # by this much of the current size
    updated_at = Column(DateTime)


class QuotaRequest(Base):
    __tablename__ = "quota_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), index=True)
    current_quota_bytes = Column(BigInteger, nullable=True)
    used_bytes = Column(BigInteger)
    requested_quota_bytes = Column(BigInteger)
    granted_quota_bytes = Column(BigInteger, nullable=True)
    reason = Column(String(500))
    status = Column(String(16), default="pending")      # pending | approved | denied
    created_at = Column(DateTime)
    decided_at = Column(DateTime, nullable=True)


class ScalingEvent(Base):
    __tablename__ = "scaling_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, index=True)
    kind = Column(String(16))           # pool | quota | compress | trash
    target = Column(String(255))        # "primary" or a uid
    old_bytes = Column(BigInteger, nullable=True)
    new_bytes = Column(BigInteger, nullable=True)
    actor = Column(String(64))          # admin | autoscaler | <uid>
    note = Column(String(500))


class RecommendationNotice(Base):
    """A recommendation already pushed to its user's Nextcloud bell (sent once, re-armed when it clears)."""
    __tablename__ = "recommendation_notices"
    id = Column(Integer, primary_key=True, autoincrement=True)
    rec_key = Column(String(255), unique=True)
    user_id = Column(String(64), index=True)
    notified_at = Column(DateTime)


ENTITY_MODELS = {
    "User": User, "File": File, "Folder": Folder, "Share": Share, "Permission": Permission,
    "Storage": Storage, "Quota": Quota, "StorageUsageRecord": StorageUsageRecord,
    "PredictionRecord": PredictionRecord, "Alert": Alert,
    "OptimizationRecommendation": OptimizationRecommendation,
    "PoolConfig": PoolConfig, "QuotaRequest": QuotaRequest, "ScalingEvent": ScalingEvent,
    "RecommendationNotice": RecommendationNotice,
}

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(retries=30, delay=3):
    """Create tables, retrying while the database container is still starting."""
    for attempt in range(retries):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"[db] database not ready ({exc.__class__.__name__}), retrying in {delay}s")
            time.sleep(delay)


def reset_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def utcnow():
    return datetime.utcnow().replace(microsecond=0)
