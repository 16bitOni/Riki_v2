"""prompt versions and eval judge

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-31 23:17:34.836637
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('prompt_versions',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('templates', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('persona', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column('eval_runs', sa.Column('judge', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('eval_runs', 'judge')
    op.drop_table('prompt_versions')
