"""people: several people on one Riki, kept apart by the database

Every table that holds a person's data gets a user_id (everything that exists today becomes the owner's), and Postgres
row-level security shows a session only its own person's rows. Sessions run as the restricted role riki_app.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-01 20:00:00
"""
import os
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

OWNER = os.environ.get("RIKI_USER_ID", "me")  # what config.Settings.user_id is; the owner's rows keep that id
assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", OWNER), "RIKI_USER_ID must be 1-32 letters, digits, - or _"
SCOPED = ["threads", "messages", "artifacts", "runs", "spans", "approvals", "approval_grants", "reflections",
          "schedules", "app_settings", "nudges", "feedback", "persona", "connections"]
SCOPE = "current_setting('riki.user_id', true)"


def upgrade() -> None:
    op.create_table(
        'people',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('email', sa.String(length=254), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('role', sa.String(length=8), server_default='member', nullable=False),
        sa.Column('disabled', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('login', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    # the person who owned everything until now; start-up fills in their real email and name
    op.execute(sa.text("INSERT INTO people (id, email, name, role, created_at) VALUES (:id, :email, 'Owner', 'owner', now())")
               .bindparams(id=OWNER, email=f"{OWNER}@local"))

    for t in SCOPED:
        op.execute(f"ALTER TABLE {t} ADD COLUMN user_id VARCHAR(32) NOT NULL DEFAULT '{OWNER}'")
        op.execute(f"ALTER TABLE {t} ADD CONSTRAINT {t}_user_id_fkey FOREIGN KEY (user_id) REFERENCES people (id) ON DELETE CASCADE")
        op.execute(f"ALTER TABLE {t} ALTER COLUMN user_id SET DEFAULT {SCOPE}")  # new rows: the session's person, or an error
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY own_rows ON {t} USING (user_id = {SCOPE}) WITH CHECK (user_id = {SCOPE})")

    op.execute(f"ALTER TABLE devices ADD COLUMN user_id VARCHAR(32) NOT NULL DEFAULT '{OWNER}'")
    op.execute("ALTER TABLE devices ADD CONSTRAINT devices_user_id_fkey FOREIGN KEY (user_id) REFERENCES people (id) ON DELETE CASCADE")
    op.execute("ALTER TABLE devices ALTER COLUMN user_id DROP DEFAULT")
    op.execute("ALTER TABLE people ENABLE ROW LEVEL SECURITY")  # a person's own session sees only their own row
    op.execute(f"CREATE POLICY own_row ON people USING (id = {SCOPE}) WITH CHECK (id = {SCOPE})")

    # The restricted role every person's session runs as. It owns nothing, so row-level security applies to it
    # (the connecting role owns the tables and is exempt: that is "system" access, used only for sign-in and admin).
    op.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'riki_app') THEN CREATE ROLE riki_app NOLOGIN; END IF; END $$")
    op.execute("GRANT riki_app TO CURRENT_USER")
    # the schema the tables live in: "public", or one named after the database user (this app's usual setup)
    schema = op.get_bind().exec_driver_sql("select current_schema()").scalar()
    op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO riki_app')
    op.execute(f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO riki_app')
    op.execute(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema}" TO riki_app')
    op.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT ALL ON TABLES TO riki_app')
    op.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT ALL ON SEQUENCES TO riki_app')


def downgrade() -> None:
    op.execute("DROP POLICY own_row ON people")
    op.execute("ALTER TABLE people DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE devices DROP COLUMN user_id")
    for t in SCOPED:
        op.execute(f"DROP POLICY own_rows ON {t}")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DROP COLUMN user_id")
    op.drop_table('people')
    # the riki_app role is left in place: dropping it would fail while it still holds privileges, and it is harmless
