from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.db.models import Event, EventType, Lead, LeadStatus


def record_event(
    session: AsyncSession, type_: EventType, lead_id: int | None = None, **payload: Any
) -> Event:
    event = Event(lead_id=lead_id, type=type_.value, payload=payload)
    session.add(event)
    return event


def set_status(session: AsyncSession, lead: Lead, status: LeadStatus, **extra: Any) -> None:
    previous = lead.status
    lead.status = status
    record_event(
        session,
        EventType.STATUS_CHANGED,
        lead_id=lead.id,
        **{"from": previous.value if previous else None, "to": status.value},
        **extra,
    )
