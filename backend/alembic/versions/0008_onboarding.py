"""onboarding: what you use Riki for, and whether setup is done

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28 13:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('persona', sa.Column('role', sa.String(length=24), server_default='', nullable=False))
    op.add_column('persona', sa.Column('onboarded', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('persona', 'onboarded')
    op.drop_column('persona', 'role')
