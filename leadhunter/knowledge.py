import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from leadhunter.db.models import Draft, DraftVariant, Lead, LeadStatus

MAX_CHUNK_CHARS = 900
MIN_SHARE_OF_BEST = 0.4
K1, B = 1.5, 0.75
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.+#-][a-z0-9]+)*[+#]*")
_STOPWORDS = frozenset(
    re.findall(
        r"\w+",
        "a an and any are as at be but by can do for from has have how i if in is it its me my "
        "of on or our so that the their them they this to was we were what when which who will "
        "with you your",
    )
)
_cache: dict[Path, tuple[tuple, "KnowledgeBase"]] = {}


@dataclass(frozen=True)
class Chunk:
    source: str
    title: str
    text: str


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in _TOKEN.findall(text.lower()):
        parts = re.split(r"[.+#-]", token)
        variants = {token, "".join(parts), *parts} if len(parts) > 1 else {token}
        tokens += [v for v in variants if len(v) > 1 and v not in _STOPWORDS]
    return tokens


def _split_long(body: str) -> list[str]:
    parts, current = [], ""
    for paragraph in re.split(r"\n\s*\n", body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 > MAX_CHUNK_CHARS:
            parts.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        parts.append(current)
    return parts


def split_markdown(source: str, text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    title, lines = source, []

    def flush() -> None:
        chunks.extend(Chunk(source, title, part) for part in _split_long("\n".join(lines)))
        lines.clear()

    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            flush()
            title = heading.group(1).strip()
        else:
            lines.append(line)
    flush()
    return chunks


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._terms = [Counter(tokenize(f"{chunk.title} {chunk.text}")) for chunk in chunks]
        self._lengths = [sum(terms.values()) for terms in self._terms]
        self._average = sum(self._lengths) / len(chunks) if chunks else 0.0
        self._containing = Counter(term for terms in self._terms for term in terms)

    def __len__(self) -> int:
        return len(self.chunks)

    def extended(self, extra: list[Chunk]) -> "KnowledgeBase":
        return KnowledgeBase([*self.chunks, *extra]) if extra else self

    def _score(self, index: int, words: set[str]) -> float:
        terms, total = self._terms[index], 0.0
        length_factor = 1 - B + B * self._lengths[index] / self._average
        for word in words & terms.keys():
            containing = self._containing[word]
            rarity = math.log(1 + (len(self.chunks) - containing + 0.5) / (containing + 0.5))
            count = terms[word]
            total += rarity * count * (K1 + 1) / (count + K1 * length_factor)
        return total

    def search(self, query: str, limit: int = 4) -> list[Chunk]:
        words = set(tokenize(query))
        if not words or not self.chunks:
            return []
        scored = [(self._score(i, words), i) for i in range(len(self.chunks))]
        scored = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
        if not scored:
            return []
        floor = scored[0][0] * MIN_SHARE_OF_BEST
        return [self.chunks[i] for score, i in scored[:limit] if score >= floor]


def load_knowledge(folder: Path) -> KnowledgeBase:
    files = []
    if folder.is_dir():
        files = sorted(
            path
            for path in folder.iterdir()
            if path.suffix.lower() in (".md", ".txt") and not path.name.startswith("_")
        )
    signature = tuple((path.name, path.stat().st_mtime_ns) for path in files)
    cached = _cache.get(folder)
    if cached and cached[0] == signature:
        return cached[1]
    chunks = [
        chunk
        for path in files
        for chunk in split_markdown(path.stem, path.read_text(encoding="utf-8"))
    ]
    base = KnowledgeBase(chunks)
    _cache[folder] = (signature, base)
    return base


async def won_examples(session: AsyncSession, limit: int = 20) -> list[Chunk]:
    rows = await session.execute(
        select(Lead, Draft)
        .join(Draft, Draft.lead_id == Lead.id)
        .where(Lead.status == LeadStatus.WON, Draft.chosen, Draft.variant != DraftVariant.FOLLOWUP)
        .order_by(Lead.id.desc())
        .limit(limit)
    )
    return [
        Chunk(
            "past win",
            lead.title[:120],
            f"Job post: {lead.body[:400]}\n\n"
            f"Proposal that won it:\n{draft.edited_text or draft.text}",
        )
        for lead, draft in rows
    ]


def render(chunks: list[Chunk]) -> str:
    return "\n\n".join(f"[{chunk.source}: {chunk.title}]\n{chunk.text}" for chunk in chunks)


async def reference_for(session: AsyncSession, folder: Path, query: str, limit: int = 4) -> str:
    base = load_knowledge(folder).extended(await won_examples(session))
    return render(base.search(query, limit))
