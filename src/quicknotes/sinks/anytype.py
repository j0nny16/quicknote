"""Write notes into Anytype through the local anytype-cli HTTP API.

The note's substance rides on native object fields -- name, description and a
Markdown body -- so the sink works even with an empty ``property_map``. Custom
properties of the target type are opt-in extras; run ``quicknotes introspect``
to discover their keys.
"""

from __future__ import annotations

import httpx

from ..models import Note, SinkResult


class AnytypeSink:
    name = "anytype"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        space_id: str,
        *,
        type_key: str = "quicknote",
        api_version: str = "2025-11-08",
        property_map: dict[str, str] | None = None,
        icon_text: str = "\U0001f4dd",
        icon_voice: str = "\U0001f3a4",
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.space_id = space_id
        self.type_key = type_key
        self.property_map = property_map or {}
        self.icon_text = icon_text
        self.icon_voice = icon_voice
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Anytype-Version": api_version,
                "Content-Type": "application/json",
            },
        )

    # -- helpers --

    def _properties(self, note: Note) -> list[dict[str, object]]:
        """Map Note fields onto the target type's properties, if configured."""
        values: dict[str, object] = {
            "summary": note.summary,
            "source": note.source,
            "lang": note.lang,
            "raw_transcript": note.raw_transcript,
            "recorded_at": note.created_at.isoformat(),
            "tags": note.tags,
        }
        out: list[dict[str, object]] = []
        for field, key in self.property_map.items():
            if not key:
                continue
            value = values.get(field)
            if value in (None, "", []):
                continue
            if field == "tags":
                out.append({"key": key, "multi_select": value})
            elif field == "recorded_at":
                out.append({"key": key, "date": value})
            else:
                out.append({"key": key, "text": str(value)})
        return out

    @staticmethod
    def _extract_id(payload: dict) -> str:
        obj = payload.get("object", payload)
        for candidate in ("id", "object_id"):
            if isinstance(obj, dict) and obj.get(candidate):
                return str(obj[candidate])
        raise RuntimeError(f"could not find object id in Anytype response: {payload!r:.300}")

    # -- Sink protocol --

    async def create(self, note: Note) -> SinkResult:
        payload: dict[str, object] = {
            "name": note.title,
            "type_key": self.type_key,
            "body": note.body,
            "icon": {
                "format": "emoji",
                "emoji": self.icon_voice if note.from_voice else self.icon_text,
            },
        }
        # Anytype renders the description right under the title, so setting it
        # to text the body already opens with just shows it twice.
        if note.summary and note.summary.strip() != note.body.strip():
            payload["description"] = note.summary
        props = self._properties(note)
        if props:
            payload["properties"] = props

        resp = await self._client.post(
            f"{self.base_url}/v1/spaces/{self.space_id}/objects", json=payload
        )
        if resp.status_code >= 400:
            # Properties are the fragile part of the payload; a type without the
            # mapped keys must not cost the note. Retry once without them.
            if props and resp.status_code in (400, 422):
                payload.pop("properties", None)
                resp = await self._client.post(
                    f"{self.base_url}/v1/spaces/{self.space_id}/objects", json=payload
                )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Anytype create failed ({resp.status_code}): {resp.text[:400]}"
                )
        object_id = self._extract_id(resp.json())
        return SinkResult(sink=self.name, object_id=object_id)

    async def delete(self, object_id: str) -> bool:
        resp = await self._client.delete(
            f"{self.base_url}/v1/spaces/{self.space_id}/objects/{object_id}"
        )
        return resp.status_code < 400

    async def list_types(self) -> list[dict]:
        resp = await self._client.get(f"{self.base_url}/v1/spaces/{self.space_id}/types")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data if isinstance(data, list) else [])

    async def list_spaces(self) -> list[dict]:
        resp = await self._client.get(f"{self.base_url}/v1/spaces")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data if isinstance(data, list) else [])

    async def health(self) -> str:
        spaces = await self.list_spaces()
        known = {s.get("id") for s in spaces}
        if self.space_id not in known:
            names = ", ".join(f"{s.get('name')} ({s.get('id')})" for s in spaces) or "none"
            raise RuntimeError(
                f"space_id {self.space_id!r} not accessible to the bot. Joined spaces: {names}"
            )
        types = await self.list_types()
        keys = {t.get("key") for t in types}
        if self.type_key not in keys:
            raise RuntimeError(
                f"type_key {self.type_key!r} not found in the space. "
                f"Available: {', '.join(sorted(k for k in keys if k))}"
            )
        return f"space {self.space_id} reachable, type {self.type_key!r} present"

    async def aclose(self) -> None:
        await self._client.aclose()
