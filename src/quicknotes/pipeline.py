"""Turn a RawItem into a Note and fan it out to every configured sink."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .enrichers.noop import title_from_text
from .models import Enrichment, Note, RawItem, SinkResult

log = logging.getLogger(__name__)


def word_count(text: str) -> int:
    return len(text.split())


@dataclass(slots=True)
class ProcessResult:
    note: Note
    sinks: list[SinkResult]
    used_model: bool


class Pipeline:
    def __init__(
        self,
        *,
        transcriber,
        enricher,
        sinks: list,
        threshold_words: int = 10,
        note_body: str = "summary",
    ) -> None:
        self.transcriber = transcriber
        self.enricher = enricher
        self.sinks = sinks
        self.threshold_words = threshold_words
        self.note_body = note_body

    def _compose_body(self, enrichment: Enrichment, text: str) -> str:
        """What the reader actually gets. Falls back to the text whenever there
        is no summary to show -- short notes never get one."""
        tidied = (enrichment.cleaned_text or text).strip()
        summary = (enrichment.summary or "").strip()
        if not summary or self.note_body == "full":
            return tidied
        if self.note_body == "both":
            return f"{summary}\n\n---\n\n{tidied}"
        return summary

    async def process(self, item: RawItem) -> ProcessResult:
        text, lang, raw_transcript = await self._resolve_text(item)
        text = text.strip()
        if not text:
            raise ValueError("nothing to save: the note is empty")

        # Short notes don't earn a model call -- the text is already its own title.
        if word_count(text) >= self.threshold_words:
            enrichment = await self.enricher.enrich(text, lang=lang, title_hint=item.caption)
            used_model = True
        else:
            enrichment = Enrichment(
                title=title_from_text(text, hint=item.caption), cleaned_text=text
            )
            used_model = False

        note = Note(
            title=enrichment.title,
            body=self._compose_body(enrichment, text),
            summary=enrichment.summary,
            lang=lang,
            tags=enrichment.tags,
            source=item.source,
            from_voice=item.has_audio(),
            created_at=item.received_at,
            raw_transcript=raw_transcript,
        )

        results: list[SinkResult] = []
        errors: list[str] = []
        for sink in self.sinks:
            try:
                results.append(await sink.create(note))
            except Exception as exc:  # one bad sink must not hide the others
                errors.append(f"{sink.name}: {exc}")
                log.exception("sink %s failed", sink.name)
        if not results:
            raise RuntimeError("; ".join(errors) or "no sink accepted the note")
        if errors:
            log.warning("some sinks failed: %s", "; ".join(errors))
        return ProcessResult(note=note, sinks=results, used_model=used_model)

    async def _resolve_text(self, item: RawItem) -> tuple[str, str | None, str | None]:
        """Returns (text, language, raw_transcript)."""
        if not item.has_audio():
            return (item.text or "", None, None)

        audio_file = Path(item.audio_path)  # type: ignore[arg-type]
        if not audio_file.exists():
            raise FileNotFoundError(f"audio file vanished: {audio_file}")
        transcript = await self.transcriber.transcribe(
            audio_file.read_bytes(), audio_file.name
        )
        # A caption on a voice message is context, not content.
        return (transcript.text, transcript.lang, transcript.text)
