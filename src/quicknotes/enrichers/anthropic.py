"""Claude-backed enrichment: title, short summary, tags, tidied transcript."""

from __future__ import annotations

import anthropic
from pydantic import BaseModel, Field

from ..models import Enrichment
from .noop import title_from_text

SYSTEM = """\
You clean up quick voice/text notes and label them. The note is a raw thought \
captured on the go -- often a spoken transcript with filler words, false starts \
and missing punctuation.

Rules:
- Always answer in the SAME language as the note (usually German, sometimes English).
- title: 3-8 words, names the actual subject. No quotes, no trailing period, no \
  generic labels like "Notiz" or "Voice note".
- summary: the note itself, condensed. This is usually all the reader keeps, so \
  carry every point worth remembering -- decisions, todos, names, numbers, dates, \
  open questions -- and drop the rambling, the repetition and the thinking-out-loud. \
  One or two sentences for a brief thought; several sentences, or short "- " bullet \
  lines, for a long ramble with distinct points. Only what the note actually says: \
  never invent detail, never add advice, never pad.
- cleaned_text: the note's full content with filler words removed, punctuation and \
  capitalisation fixed, and paragraph breaks where they help. Preserve every idea \
  and all specifics (names, numbers, dates). This is a tidy-up, NOT a summary -- \
  do not shorten the substance.
"""


class _Schema(BaseModel):
    title: str = Field(description="3-8 words naming the subject of the note")
    summary: str = Field(
        description="the note condensed to what is worth keeping; length follows the input"
    )
    cleaned_text: str = Field(description="full note text, tidied but not shortened")


class _SchemaWithTags(_Schema):
    tags: list[str] = Field(default_factory=list, description="0-4 lowercase keywords")


TAG_RULE = (
    "- tags: 0-4 short lowercase keywords, single words where possible. "
    "An empty list is fine.\n"
)


class AnthropicEnricher:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        *,
        max_tokens: int = 2048,
        timeout_s: float = 120.0,
        generate_tags: bool = False,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.generate_tags = generate_tags
        # Asking for tags nobody reads costs tokens on every note, so the rule
        # only enters the prompt when a sink actually wants them.
        self._schema = _SchemaWithTags if generate_tags else _Schema
        self._system = SYSTEM + (TAG_RULE if generate_tags else "")
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s)

    async def enrich(
        self, text: str, *, lang: str | None = None, title_hint: str | None = None
    ) -> Enrichment:
        prompt = f"<note>\n{text}\n</note>"
        if title_hint and title_hint.strip():
            prompt += (
                f"\n\nThe author added this caption; prefer it as the basis for the "
                f"title if it fits: {title_hint.strip()}"
            )
        if lang:
            prompt += f"\n\n(Detected language: {lang})"

        response = await self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system,
            messages=[{"role": "user", "content": prompt}],
            output_format=self._schema,
        )
        parsed = response.parsed_output
        if parsed is None:
            # Structured output failed; a note is worth more than a perfect title.
            return Enrichment(title=title_from_text(text, hint=title_hint), cleaned_text=text)

        title = parsed.title.strip().strip('"').rstrip(".") or title_from_text(text)
        summary = parsed.summary.strip() or None
        cleaned = parsed.cleaned_text.strip() or text
        raw_tags = getattr(parsed, "tags", [])
        tags = [t.strip().lower() for t in raw_tags if t and t.strip()][:4]
        return Enrichment(title=title, summary=summary, tags=tags, cleaned_text=cleaned)

    async def health(self) -> str:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        _ = response.content
        return f"{self.model} reachable"

    async def aclose(self) -> None:
        await self._client.close()
