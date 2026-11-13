from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '2fb3e31ab7ba'
down_revision: Union[str, None] = '9467d51df653'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('lead_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('address', sa.String(length=255), nullable=True),
    sa.Column('client_name', sa.String(length=255), nullable=True),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('question', sa.Text(), nullable=True),
    sa.Column('brief', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_conversations_lead_id_leads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversations')),
    sa.UniqueConstraint('lead_id', name=op.f('uq_conversations_lead_id'))
    )
    op.create_index('ix_conversations_state', 'conversations', ['state'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('external_id', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_messages_conversation_id_conversations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages')),
    sa.UniqueConstraint('conversation_id', 'external_id', name='uq_messages_conversation_id_external_id')
    )
    op.create_index(op.f('ix_messages_conversation_id'), 'messages', ['conversation_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_messages_conversation_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_conversations_state', table_name='conversations')
    op.drop_table('conversations')
