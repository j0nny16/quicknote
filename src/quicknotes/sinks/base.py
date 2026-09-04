from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Note, SinkResult


@runtime_checkable
class Sink(Protocol):
    """Somewhere a finished Note gets written. Sinks are a fan-out list."""

    name: str

    async def create(self, note: Note) -> SinkResult: ...

    async def delete(self, object_id: str) -> bool:
        """Best-effort removal for /undo. Returns True if the object is gone."""
        ...

    async def health(self) -> str: ...

    async def aclose(self) -> None: ...
