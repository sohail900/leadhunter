from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '9467d51df653'
down_revision: Union[str, None] = '9433afd4aa40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('app_state',
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_state'))
    )


def downgrade() -> None:
    op.drop_table('app_state')
