"""tenants, api keys, usage ledger

Revision ID: 0001
Revises:
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("monthly_budget_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("requests_per_minute", sa.Integer(), nullable=True),
        sa.Column("burst", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False, index=True
        ),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("hint", sa.String(32), nullable=False),
        sa.Column("label", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "usage",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False, index=True
        ),
        sa.Column("request_id", sa.String(64), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("route", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("usage")
    op.drop_table("api_keys")
    op.drop_table("tenants")
