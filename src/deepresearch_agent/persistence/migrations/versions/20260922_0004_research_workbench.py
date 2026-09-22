"""Durable research revisions, approval, cells and reports.

Revision ID: 20260922_0004
Revises: 20260828_0003
"""
from alembic import op
from deepresearch_agent.research.storage import RESEARCH_TABLES

revision = "20260922_0004"
down_revision = "20260828_0003"
branch_labels = None
depends_on = None


def upgrade():
    for model in RESEARCH_TABLES:
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in reversed(RESEARCH_TABLES):
        model.__table__.drop(op.get_bind(), checkfirst=True)
