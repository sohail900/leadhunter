from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '9433afd4aa40'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('leads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('external_id', sa.String(length=255), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('author', sa.String(length=255), nullable=True),
    sa.Column('contact_hint', sa.String(length=255), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='new', nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('score_reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('budget_min', sa.Integer(), nullable=True),
    sa.Column('budget_max', sa.Integer(), nullable=True),
    sa.Column('tech_stack', postgresql.ARRAY(sa.String(length=100)), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_leads')),
    sa.UniqueConstraint('source', 'external_id', name='uq_leads_source_external_id')
    )
    op.create_index('ix_leads_content_hash', 'leads', ['content_hash'], unique=False)
    op.create_index('ix_leads_status', 'leads', ['status'], unique=False)
    op.create_table('drafts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('lead_id', sa.Integer(), nullable=False),
    sa.Column('variant', sa.String(length=20), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('chosen', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('edited_text', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_drafts_lead_id_leads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_drafts'))
    )
    op.create_index(op.f('ix_drafts_lead_id'), 'drafts', ['lead_id'], unique=False)
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('lead_id', sa.Integer(), nullable=True),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_events_lead_id_leads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events'))
    )
    op.create_index(op.f('ix_events_lead_id'), 'events', ['lead_id'], unique=False)
    op.create_index(op.f('ix_events_type'), 'events', ['type'], unique=False)
    op.create_table('followups',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('lead_id', sa.Integer(), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('done', sa.Boolean(), server_default='false', nullable=False),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_followups_lead_id_leads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_followups'))
    )
    op.create_index('ix_followups_due', 'followups', ['done', 'due_at'], unique=False)
    op.create_index(op.f('ix_followups_lead_id'), 'followups', ['lead_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_followups_lead_id'), table_name='followups')
    op.drop_index('ix_followups_due', table_name='followups')
    op.drop_table('followups')
    op.drop_index(op.f('ix_events_type'), table_name='events')
    op.drop_index(op.f('ix_events_lead_id'), table_name='events')
    op.drop_table('events')
    op.drop_index(op.f('ix_drafts_lead_id'), table_name='drafts')
    op.drop_table('drafts')
    op.drop_index('ix_leads_status', table_name='leads')
    op.drop_index('ix_leads_content_hash', table_name='leads')
    op.drop_table('leads')
