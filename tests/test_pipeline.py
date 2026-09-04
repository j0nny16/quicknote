"""Pipeline behaviour: the word threshold, title fallbacks and sink fan-out."""

from __future__ import annotations

import pytest

from quicknotes.models import Enrichment, RawItem, SinkResult, Transcript
from quicknotes.pipeline import Pipeline


class FakeTranscriber:
    name = "fake"

    def __init__(self, text: str = "", lang: str = "de") -> None:
        self.text, self.lang, self.calls = text, lang, 0

    async def transcribe(self, audio: bytes, filename: str) -> Transcript:
        self.calls += 1
        return Transcript(text=self.text, lang=self.lang)

    async def aclose(self) -> None: ...


class FakeEnricher:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def enrich(self, text, *, lang=None, title_hint=None) -> Enrichment:
        self.calls += 1
        return Enrichment(
            title="Model title", summary="A summary.", tags=["x"], cleaned_text="cleaned"
        )

    async def aclose(self) -> None: ...


class FakeSink:
    def __init__(self, name: str = "fake", fail: bool = False) -> None:
        self.name, self.fail, self.notes = name, fail, []

    async def create(self, note) -> SinkResult:
        if self.fail:
            raise RuntimeError("sink down")
        self.notes.append(note)
        return SinkResult(sink=self.name, object_id=f"{self.name}-1")

    async def aclose(self) -> None: ...


def build(enricher=None, sinks=None, transcriber=None, threshold=10) -> tuple:
    enricher = enricher or FakeEnricher()
    sinks = sinks if sinks is not None else [FakeSink()]
    transcriber = transcriber or FakeTranscriber()
    pipe = Pipeline(
        transcriber=transcriber, enricher=enricher, sinks=sinks, threshold_words=threshold
    )
    return pipe, enricher, sinks, transcriber


async def test_short_text_skips_the_model():
    pipe, enricher, sinks, _ = build()
    result = await pipe.process(RawItem(source="t", text="Milch kaufen nicht vergessen"))

    assert enricher.calls == 0
    assert result.used_model is False
    assert result.note.title == "Milch kaufen nicht vergessen"
    assert result.note.summary is None
    assert sinks[0].notes[0].body == "Milch kaufen nicht vergessen"


async def test_long_text_is_enriched():
    pipe, enricher, _, _ = build()
    text = " ".join(f"wort{i}" for i in range(10))
    result = await pipe.process(RawItem(source="t", text=text))

    assert enricher.calls == 1
    assert result.used_model is True
    assert result.note.title == "Model title"
    assert result.note.summary == "A summary."
    assert result.note.body == "cleaned"


async def test_threshold_is_inclusive_at_the_boundary():
    nine = " ".join(f"w{i}" for i in range(9))
    ten = " ".join(f"w{i}" for i in range(10))

    pipe, enricher, _, _ = build()
    await pipe.process(RawItem(source="t", text=nine))
    assert enricher.calls == 0

    await pipe.process(RawItem(source="t", text=ten))
    assert enricher.calls == 1


async def test_caption_becomes_the_title_for_short_notes():
    pipe, _, _, _ = build()
    result = await pipe.process(
        RawItem(source="t", text="kurz", caption="Einkaufsliste")
    )
    assert result.note.title == "Einkaufsliste"


async def test_audio_is_transcribed_and_marks_the_note_as_voice(tmp_path):
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"fake-audio")
    transcriber = FakeTranscriber(text="Das ist eine gesprochene Notiz", lang="de")
    pipe, _, _, _ = build(transcriber=transcriber, threshold=100)

    result = await pipe.process(RawItem(source="t", audio_path=str(audio)))

    assert transcriber.calls == 1
    assert result.note.from_voice is True
    assert result.note.lang == "de"
    assert result.note.raw_transcript == "Das ist eine gesprochene Notiz"


async def test_missing_audio_file_raises():
    pipe, _, _, _ = build()
    with pytest.raises(FileNotFoundError):
        await pipe.process(RawItem(source="t", audio_path="/nope/gone.ogg"))


async def test_empty_note_is_rejected():
    pipe, _, _, _ = build()
    with pytest.raises(ValueError):
        await pipe.process(RawItem(source="t", text="   "))


async def test_fan_out_writes_to_every_sink():
    sinks = [FakeSink("a"), FakeSink("b")]
    pipe, _, _, _ = build(sinks=sinks)
    result = await pipe.process(RawItem(source="t", text="kurz"))

    assert {r.sink for r in result.sinks} == {"a", "b"}
    assert len(sinks[0].notes) == len(sinks[1].notes) == 1


async def test_one_failing_sink_does_not_lose_the_note():
    sinks = [FakeSink("broken", fail=True), FakeSink("good")]
    pipe, _, _, _ = build(sinks=sinks)
    result = await pipe.process(RawItem(source="t", text="kurz"))

    assert [r.sink for r in result.sinks] == ["good"]
    assert len(sinks[1].notes) == 1


async def test_all_sinks_failing_raises_so_the_job_retries():
    pipe, _, _, _ = build(sinks=[FakeSink("broken", fail=True)])
    with pytest.raises(RuntimeError):
        await pipe.process(RawItem(source="t", text="kurz"))
