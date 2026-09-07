"""Persistence: tenants, their keys, and a ledger of what each request cost.

SQLite by default so the service runs from a single file; any SQLAlchemy URL
works, and the compose file runs it against PostgreSQL. The schema is owned by
Alembic (`alembic/`); `init_schema()` applies the migrations programmatically
so a fresh container needs no separate step.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # None means unlimited. Stored with enough precision for eight-decimal costs to add up exactly.
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    requests_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    burst: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    keys: Mapped[list[ApiKey]] = relationship(back_populates="tenant")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hint: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="keys")


class UsageRecord(Base):
    __tablename__ = "usage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    route: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))  # ok | error
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


def make_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


def alembic_config(database_url: str) -> AlembicConfig:
    here = Path(__file__).resolve().parent.parent.parent / "alembic"
    config = AlembicConfig()
    config.set_main_option("script_location", str(here))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def init_schema(database_url: str) -> None:
    """Apply every migration. Idempotent; safe to run on each start."""
    command.upgrade(alembic_config(database_url), "head")


def month_start(now: dt.datetime | None = None) -> dt.datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend(session: Session, tenant_id: str, now: dt.datetime | None = None) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(
            UsageRecord.tenant_id == tenant_id, UsageRecord.ts >= month_start(now)
        )
    ).scalar_one()
    return Decimal(str(total))
