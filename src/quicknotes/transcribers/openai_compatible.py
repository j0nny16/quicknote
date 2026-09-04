"""Transcription over the OpenAI ``/audio/transcriptions`` contract.

Deliberately one class for three deployments: the local whisper container,
Groq and OpenAI all implement the same endpoint, so switching is a base_url
change in config.yaml rather than a new code path.
"""

from __future__ import annotations

import httpx

from ..models import Transcript


class OpenAICompatibleTranscriber:
    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        language: str | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=timeout_s, headers=headers)

    async def transcribe(self, audio: bytes, filename: str) -> Transcript:
        data = {"model": self.model, "response_format": "verbose_json"}
        if self.language:
            data["language"] = self.language
        resp = await self._client.post(
            f"{self.base_url}/audio/transcriptions",
            files={"file": (filename, audio, "application/octet-stream")},
            data=data,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"transcription failed ({resp.status_code}): {resp.text[:400]}"
            )
        payload = resp.json()
        text = (payload.get("text") or "").strip()
        if not text:
            raise RuntimeError("transcription returned empty text")
        return Transcript(text=text, lang=payload.get("language"))

    async def health(self) -> str:
        # A reachable server is not enough: a local whisper server answers
        # /models with an empty list until the model has actually been pulled,
        # and the failure would otherwise only surface on the first real note.
        resp = await self._client.get(f"{self.base_url}/models", timeout=20.0)
        resp.raise_for_status()
        payload = resp.json()
        entries = payload.get("data", payload) if isinstance(payload, dict) else payload
        installed = {
            e.get("id") for e in entries if isinstance(e, dict)
        } if isinstance(entries, list) else set()
        if installed and self.model not in installed:
            raise RuntimeError(
                f"model {self.model!r} is not installed on {self.base_url}. "
                f"Available: {', '.join(sorted(i for i in installed if i)) or 'none'}. "
                f"Pull it with: curl -X POST {self.base_url}/models/{self.model}"
            )
        if not installed:
            raise RuntimeError(
                f"no models installed on {self.base_url}. "
                f"Pull one with: curl -X POST {self.base_url}/models/{self.model}"
            )
        return f"{self.base_url} ready (model {self.model})"

    async def aclose(self) -> None:
        await self._client.aclose()
