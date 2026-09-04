"""Wiring: build the configured plugins, run the source and the queue worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path

from .config import Config, ConfigError
from .enrichers.anthropic import AnthropicEnricher
from .enrichers.noop import NoopEnricher
from .models import RawItem
from .pipeline import Pipeline
from .queue import JobQueue
from .sinks.anytype import AnytypeSink
from .sources.telegram import TelegramSource
from .transcribers.openai_compatible import OpenAICompatibleTranscriber
from .transcribers.passthrough import PassthroughTranscriber

log = logging.getLogger(__name__)

IDLE_POLL_S = 2.0


# -- factories: config.type -> implementation ---------------------------------


def build_transcriber(cfg: Config):
    tc = cfg.transcriber
    if tc.type == "passthrough":
        return PassthroughTranscriber()
    return OpenAICompatibleTranscriber(
        tc.base_url,
        tc.model,
        api_key=tc.api_key(),
        language=tc.language,
        timeout_s=tc.timeout_s,
    )


def build_enricher(cfg: Config):
    ec = cfg.enricher
    if ec.type == "noop":
        return NoopEnricher()
    return AnthropicEnricher(
        ec.api_key(),
        ec.model,
        max_tokens=ec.max_tokens,
        timeout_s=ec.timeout_s,
        generate_tags=ec.generate_tags,
    )


def build_sinks(cfg: Config) -> list:
    sinks = []
    for sc in cfg.sinks:
        if not sc.space_id:
            raise ConfigError(
                "sink.anytype.space_id is empty. Run "
                "'docker compose exec anytype-cli anytype space list' and put the id "
                "into config.yaml (or set ANYTYPE_SPACE_ID)."
            )
        sinks.append(
            AnytypeSink(
                sc.base_url,
                sc.api_key(),
                sc.space_id,
                type_key=sc.type_key,
                api_version=sc.api_version,
                property_map=sc.property_map,
                icon_text=sc.icon_text,
                icon_voice=sc.icon_voice,
                timeout_s=sc.timeout_s,
            )
        )
    return sinks


# -- the running application --------------------------------------------------


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.queue = JobQueue(
            cfg.db_path, max_attempts=cfg.max_attempts, backoff_s=cfg.retry_backoff_s
        )
        self.transcriber = build_transcriber(cfg)
        self.enricher = build_enricher(cfg)
        self.sinks = build_sinks(cfg)
        self.pipeline = Pipeline(
            transcriber=self.transcriber,
            enricher=self.enricher,
            sinks=self.sinks,
            threshold_words=cfg.threshold_words,
            note_body=cfg.note_body,
        )
        self.source = TelegramSource(
            cfg.source.token(),
            allowed_user_ids=cfg.source.allowed_user_ids,
            audio_dir=cfg.audio_dir,
            on_item=self._enqueue,
            on_undo=self._undo,
            on_status=self._status,
            ack_emoji=cfg.source.ack_emoji,
        )

    # -- source callbacks --

    async def _enqueue(self, item: RawItem) -> None:
        job_id = await self.queue.enqueue(item)
        log.info("queued job %s from %s", job_id, item.source)

    async def _undo(self, chat_key: str) -> str:
        rows = await self.queue.pop_last_objects(chat_key)
        if not rows:
            return "Nothing to undo."
        by_name = {s.name: s for s in self.sinks}
        removed, failed = [], []
        for row in rows:
            sink = by_name.get(row["sink"])
            if sink is None:
                failed.append(f"{row['sink']} (not configured)")
                continue
            try:
                ok = await sink.delete(row["object_id"])
            except Exception as exc:
                log.exception("undo failed")
                failed.append(f"{row['sink']}: {str(exc)[:120]}")
                continue
            (removed if ok else failed).append(row["sink"])
        title = rows[0].get("title") or "note"
        if removed and not failed:
            return f"🗑 Removed “{title}” from {', '.join(removed)}."
        if removed:
            return f"🗑 Removed from {', '.join(removed)}; failed: {', '.join(failed)}."
        return f"Could not remove “{title}”: {', '.join(failed)}"

    async def _status(self) -> str:
        stats = await self.queue.stats()
        lines = [
            f"Queue: {stats.get('pending', 0)} pending, "
            f"{stats.get('done', 0)} done, {stats.get('dead', 0)} failed"
        ]
        for label, component in (
            ("stt", self.transcriber),
            ("llm", self.enricher),
            *((f"sink:{s.name}", s) for s in self.sinks),
        ):
            try:
                lines.append(f"{label}: ✅ {await component.health()}")
            except Exception as exc:
                lines.append(f"{label}: ❌ {str(exc)[:160]}")
        return "\n".join(lines)

    # -- worker --

    async def worker(self) -> None:
        while True:
            job = await self.queue.next_ready()
            if job is None:
                await asyncio.sleep(IDLE_POLL_S)
                continue
            await self._run_job(job)

    async def _run_job(self, job) -> None:
        item = job.item
        chat_key = item.meta.get("chat_key", "unknown")
        try:
            result = await self.pipeline.process(item)
        except Exception as exc:
            dead = await self.queue.fail(job.id, repr(exc))
            attempts = job.attempts + 1
            log.warning("job %s failed (attempt %s): %s", job.id, attempts, exc)
            if dead:
                await self.source.notify(
                    item,
                    f"❌ Could not save this note after {attempts} attempts.\n"
                    f"{str(exc)[:300]}\n\nIt stays in the queue database; "
                    f"fix the cause and restart to retry.",
                )
            else:
                await self.source.notify(
                    item, f"⚠️ Attempt {attempts} failed, retrying…\n{str(exc)[:200]}"
                )
            return

        await self.queue.complete(job.id)
        # One id groups every sink row of this note, so /undo removes the note
        # everywhere it landed rather than from one backend at a time.
        note_id = uuid.uuid4().hex
        for sink_result in result.sinks:
            await self.queue.record_object(
                chat_key, note_id, sink_result.sink, sink_result.object_id, result.note.title
            )
        await self.queue.trim_history(chat_key, self.cfg.undo_history)
        self._cleanup_audio(item)

        note = result.note
        lines = [f"✅ {note.title}"]
        if note.summary:
            lines.append(note.summary)
        lines.append("/undo to remove")
        await self.source.notify(item, "\n\n".join(lines))
        log.info("saved %r (model=%s)", note.title, result.used_model)

    def _cleanup_audio(self, item: RawItem) -> None:
        if not item.audio_path:
            return
        with contextlib.suppress(OSError):
            Path(item.audio_path).unlink(missing_ok=True)

    # -- lifecycle --

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self.source.run(), name="source"),
            asyncio.create_task(self.worker(), name="worker"),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()  # re-raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.aclose()

    async def aclose(self) -> None:
        for component in (self.transcriber, self.enricher, *self.sinks):
            with contextlib.suppress(Exception):
                await component.aclose()
        self.queue.close()
