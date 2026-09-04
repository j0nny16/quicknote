"""Title-only enrichment with no model call.

Used below the word threshold, and as the whole enricher when someone wants
QuickNotes to run without any LLM at all.
"""

from __future__ import annotations

from ..models import Enrichment

MAX_TITLE = 80


def title_from_text(text: str, *, hint: str | None = None) -> str:
    if hint and hint.strip():
        candidate = hint.strip()
    else:
        candidate = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    candidate = " ".join(candidate.split())
    if not candidate:
        return "Quicknote"
    if len(candidate) <= MAX_TITLE:
        return candidate
    return candidate[: MAX_TITLE - 1].rstrip() + "…"


class NoopEnricher:
    name = "noop"

    async def enrich(
        self, text: str, *, lang: str | None = None, title_hint: str | None = None
    ) -> Enrichment:
        return Enrichment(
            title=title_from_text(text, hint=title_hint),
            summary=None,
            tags=[],
            cleaned_text=text,
        )

    async def health(self) -> str:
        return "noop (no model calls)"

    async def aclose(self) -> None:
        return None
