from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class LeadStatus(StrEnum):
    NEW = "new"
    SCORED = "scored"
    REJECTED = "rejected"
    DRAFTED = "drafted"
    SENT_TO_ME = "sent_to_me"
    APPROVED = "approved"
    SKIPPED = "skipped"
    CONTACTED = "contacted"
    REPLIED = "replied"
    WON = "won"
    LOST = "lost"


class DraftVariant(StrEnum):
    SHORT = "short"
    PORTFOLIO_LED = "portfolio_led"
    PROBLEM_LED = "problem_led"
    CUSTOM = "custom"
    FOLLOWUP = "followup"


PROPOSAL_VARIANTS = (DraftVariant.SHORT, DraftVariant.PORTFOLIO_LED, DraftVariant.PROBLEM_LED)


class EventType(StrEnum):
    STATUS_CHANGED = "status_changed"
    MESSAGE_SENT = "message_sent"
    SETTING_CHANGED = "setting_changed"


def _str_enum(enum_cls: type[StrEnum], length: int) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=length,
        values_callable=lambda e: [member.value for member in e],
    )


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_leads_source_external_id"),
        Index("ix_leads_content_hash", "content_hash"),
        Index("ix_leads_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(255))
    contact_hint: Mapped[str | None] = mapped_column(String(255))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[LeadStatus] = mapped_column(
        _str_enum(LeadStatus, 20), default=LeadStatus.NEW, server_default=LeadStatus.NEW.value
    )
    score: Mapped[float | None] = mapped_column(Float)
    score_reasons: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    budget_min: Mapped[int | None] = mapped_column(Integer)
    budget_max: Mapped[int | None] = mapped_column(Integer)
    tech_stack: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)))

    drafts: Mapped[list["Draft"]] = relationship(back_populates="lead")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    variant: Mapped[DraftVariant] = mapped_column(_str_enum(DraftVariant, 20))
    text: Mapped[str] = mapped_column(Text)
    chosen: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    edited_text: Mapped[str | None] = mapped_column(Text)

    lead: Mapped[Lead] = relationship(back_populates="drafts")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class Followup(Base):
    __tablename__ = "followups"
    __table_args__ = (Index("ix_followups_due", "done", "due_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
