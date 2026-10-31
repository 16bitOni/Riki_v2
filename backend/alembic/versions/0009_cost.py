"""cost per run

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-31 16:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('cost_usd', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('runs', 'cost_usd')
