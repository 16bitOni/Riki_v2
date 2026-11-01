"""reflections, schedules, nudges, durable approvals, grants

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('threads', sa.Column('lessons_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('runs', sa.Column('effort', sa.String(length=8), nullable=True))
    op.add_column('runs', sa.Column('resume_of', sa.String(length=32), nullable=True))
    op.add_column('approvals', sa.Column('thread_id', sa.String(length=32), nullable=True))
    op.add_column('approvals', sa.Column('args_hash', sa.String(length=64), nullable=True))
    op.add_column('approvals', sa.Column('money', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('approvals', sa.Column('via', sa.String(length=8), server_default='user', nullable=False))
    op.add_column('approvals', sa.Column('result', sa.Text(), nullable=True))
    op.add_column('approvals', sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('approvals', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_approvals_thread_id'), 'approvals', ['thread_id'], unique=False)
    op.create_table(
        'approval_grants',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('thread_id', sa.String(length=32), nullable=False),
        sa.Column('tool', sa.String(length=200), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_approval_grants_thread_id'), 'approval_grants', ['thread_id'], unique=False)
    op.create_table(
        'reflections',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('monologue', sa.Text(), server_default='', nullable=False),
        sa.Column('memory_count', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=8), nullable=False),
        sa.Column('run_id', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_reflections_created_at'), 'reflections', ['created_at'], unique=False)
    op.create_table(
        'schedules',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('next_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('repeat', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('last_run_id', sa.String(length=32), nullable=True),
        sa.Column('last_fired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_schedules_next_at'), 'schedules', ['next_at'], unique=False)
    op.create_table(
        'nudges',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('topic', sa.String(length=120), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(length=12), nullable=False),
        sa.Column('run_id', sa.String(length=32), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reaction', sa.String(length=8), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_nudges_sent_at'), 'nudges', ['sent_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_nudges_sent_at'), table_name='nudges')
    op.drop_table('nudges')
    op.drop_index(op.f('ix_schedules_next_at'), table_name='schedules')
    op.drop_table('schedules')
    op.drop_index(op.f('ix_reflections_created_at'), table_name='reflections')
    op.drop_table('reflections')
    op.drop_index(op.f('ix_approval_grants_thread_id'), table_name='approval_grants')
    op.drop_table('approval_grants')
    op.drop_index(op.f('ix_approvals_thread_id'), table_name='approvals')
    for col in ('expires_at', 'executed_at', 'result', 'via', 'money', 'args_hash', 'thread_id'):
        op.drop_column('approvals', col)
    op.drop_column('runs', 'resume_of')
    op.drop_column('runs', 'effort')
    op.drop_column('threads', 'lessons_at')
