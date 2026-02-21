"""add interactor to browser session

Revision ID: 12a4e89c54d3
Revises: 455376bf73b2
Create Date: 2026-02-16 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "12a4e89c54d3"
down_revision: Union[str, None] = "455376bf73b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "persistent_browser_sessions", sa.Column("interactor", sa.String(), nullable=True, server_default="agent")
    )


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "interactor")
