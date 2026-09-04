"""Core data types passed between pipeline stages.

The pipeline is deliberately narrow: every Source produces a RawItem, every
Transcriber produces a Transcript, every Enricher produces an Enrichment, and
every Sink consumes a Note. Adding a backend means implementing one protocol,
not touching the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class RawItem:
    """One inbound capture, before any processing."""

    source: str
    text: str | None = None
    audio_path: str | None = None
    audio_mime: str = "audio/ogg"
    caption: str | None = None
    received_at: datetime = field(default_factory=_utcnow)
    # Source-specific routing info the Source needs to reply to the right place
    # (for Telegram: chat_id + the id of the "queued" ack message it will edit).
    meta: dict[str, Any] = field(default_factory=dict)

    def has_audio(self) -> bool:
        return self.audio_path is not None


@dataclass(slots=True)
class Transcript:
    text: str
    lang: str | None = None


@dataclass(slots=True)
class Enrichment:
    title: str
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    cleaned_text: str | None = None


@dataclass(slots=True)
class Note:
    """What a Sink writes out."""

    title: str
    body: str
    summary: str | None = None
    lang: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str = "unknown"
    from_voice: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    raw_transcript: str | None = None


@dataclass(slots=True)
class SinkResult:
    sink: str
    object_id: str
    url: str | None = None
