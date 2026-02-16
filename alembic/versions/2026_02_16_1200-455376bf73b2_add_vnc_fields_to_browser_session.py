"""add vnc fields to browser session

Revision ID: 455376bf73b2
Revises: 43217e31df12
Create Date: 2026-02-16 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "455376bf73b2"
down_revision: Union[str, None] = "43217e31df12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("persistent_browser_sessions", sa.Column("display_number", sa.Integer(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("vnc_port", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "vnc_port")
    op.drop_column("persistent_browser_sessions", "display_number")
