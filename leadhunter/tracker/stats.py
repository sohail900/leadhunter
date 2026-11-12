from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.db.models import PROPOSAL_VARIANTS, Draft, DraftVariant, Event, EventType, Lead
from leadhunter.db.models import LeadStatus as S

MIN_CONTACTED_FOR_GUIDANCE = 20


@dataclass
class Counts:
    found: int = 0
    contacted: int = 0
    replied: int = 0
    won: int = 0

    @property
    def reply_rate(self) -> float | None:
        return self.replied / self.contacted if self.contacted else None


@dataclass
class Stats:
    total: Counts = field(default_factory=Counts)
    by_source: dict[str, Counts] = field(default_factory=dict)
    by_variant: dict[str, Counts] = field(default_factory=dict)


def _reached(*statuses: S):
    return func.count(Lead.id.distinct()).filter(
        Event.payload["to"].astext.in_([s.value for s in statuses])
    )


def _columns(column):
    return select(
        column,
        func.count(Lead.id.distinct()),
        _reached(S.CONTACTED),
        _reached(S.REPLIED, S.WON),
        _reached(S.WON),
    )


def _grouped(query, column, since: datetime | None):
    query = query.outerjoin(
        Event, (Event.lead_id == Lead.id) & (Event.type == EventType.STATUS_CHANGED.value)
    )
    if since is not None:
        query = query.where(Lead.fetched_at >= since)
    return query.group_by(column).order_by(column)


async def collect_stats(session: AsyncSession, since: datetime | None = None) -> Stats:
    stats = Stats()

    by_source = _grouped(_columns(Lead.source).select_from(Lead), Lead.source, since)
    for source, found, contacted, replied, won in (await session.execute(by_source)).all():
        stats.by_source[source] = Counts(found, contacted, replied, won)
        stats.total.found += found
        stats.total.contacted += contacted
        stats.total.replied += replied
        stats.total.won += won

    variants = [*PROPOSAL_VARIANTS, DraftVariant.CUSTOM]
    chosen = (
        _columns(Draft.variant)
        .select_from(Draft)
        .join(Lead, Lead.id == Draft.lead_id)
        .where(Draft.chosen, Draft.variant.in_(variants))
    )
    by_variant = _grouped(chosen, Draft.variant, since)
    for variant, used, contacted, replied, won in (await session.execute(by_variant)).all():
        stats.by_variant[str(variant)] = Counts(used, contacted, replied, won)
    return stats


def _best(rows: dict[str, Counts], minimum_contacted: int = 3) -> list[tuple[str, Counts]]:
    eligible = [(name, c) for name, c in rows.items() if c.contacted >= minimum_contacted]
    return sorted(eligible, key=lambda item: item[1].reply_rate or 0, reverse=True)


def build_guidance(stats: Stats) -> tuple[str, str]:
    if stats.total.contacted < MIN_CONTACTED_FOR_GUIDANCE:
        return "", ""

    def describe(rows: list[tuple[str, Counts]]) -> str:
        return "; ".join(
            f"{name}: {c.replied} of {c.contacted} contacted replied" for name, c in rows[:3]
        )

    scorer = drafter = ""
    sources = _best(stats.by_source)
    if sources:
        scorer = (
            "Past results by source, as a mild signal only (the post itself matters more): "
            + describe(sources)
            + "."
        )
    variants = _best({k: v for k, v in stats.by_variant.items() if k != DraftVariant.CUSTOM})
    if variants:
        drafter = (
            "Past results by variant, as a mild signal only (still write all three well): "
            + describe(variants)
            + "."
        )
    return scorer, drafter
