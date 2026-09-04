from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Enrichment


@runtime_checkable
class Enricher(Protocol):
    """Derives a title (and optionally a summary) from note text."""

    name: str

    async def enrich(
        self, text: str, *, lang: str | None = None, title_hint: str | None = None
    ) -> Enrichment: ...

    async def health(self) -> str: ...

    async def aclose(self) -> None: ...
