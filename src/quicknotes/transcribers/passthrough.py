"""Used when a source only ever delivers text, or to disable STT entirely."""

from __future__ import annotations

from ..models import Transcript


class PassthroughTranscriber:
    name = "passthrough"

    async def transcribe(self, audio: bytes, filename: str) -> Transcript:
        raise RuntimeError(
            "received audio but the transcriber is 'passthrough'; "
            "set transcriber.type to openai_compatible in config.yaml"
        )

    async def health(self) -> str:
        return "passthrough (audio will be rejected)"

    async def aclose(self) -> None:
        return None
