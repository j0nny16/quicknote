from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Transcript


@runtime_checkable
class Transcriber(Protocol):
    """Turns audio bytes into text. One implementation per backend."""

    name: str

    async def transcribe(self, audio: bytes, filename: str) -> Transcript: ...

    async def health(self) -> str:
        """Return a short human-readable status, or raise."""
        ...

    async def aclose(self) -> None: ...
