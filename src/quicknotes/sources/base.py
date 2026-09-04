from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import RawItem


@runtime_checkable
class Source(Protocol):
    """Where captures come from. Owns its own transport and reply channel."""

    name: str

    async def run(self) -> None:
        """Run until cancelled."""
        ...

    async def notify(self, item: RawItem, text: str) -> None:
        """Report the outcome of a job back to wherever it came from."""
        ...

    async def health(self) -> str: ...

    async def aclose(self) -> None: ...
