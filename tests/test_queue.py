"""Queue durability: retries, backoff, dead-lettering and the /undo history."""

from __future__ import annotations

import pytest

from quicknotes.models import RawItem
from quicknotes.queue import JobQueue


@pytest.fixture
def queue(tmp_path):
    q = JobQueue(tmp_path / "q.db", max_attempts=3, backoff_s=0.0)
    yield q
    q.close()


async def test_enqueue_then_claim_roundtrips_the_item(queue):
    await queue.enqueue(RawItem(source="t", text="hallo", caption="cap"))
    job = await queue.next_ready()

    assert job is not None
    assert job.item.text == "hallo"
    assert job.item.caption == "cap"
    assert job.item.source == "t"


async def test_completed_jobs_are_not_handed_out_again(queue):
    await queue.enqueue(RawItem(source="t", text="x"))
    job = await queue.next_ready()
    await queue.complete(job.id)

    assert await queue.next_ready() is None
    assert (await queue.stats())["done"] == 1


async def test_failure_keeps_the_job_pending_until_max_attempts(queue):
    await queue.enqueue(RawItem(source="t", text="x"))
    job = await queue.next_ready()

    assert await queue.fail(job.id, "boom") is False
    assert await queue.fail(job.id, "boom") is False
    assert await queue.fail(job.id, "boom") is True  # 3rd == max_attempts

    assert await queue.next_ready() is None
    assert (await queue.stats())["dead"] == 1


async def test_attempts_survive_across_claims(queue):
    await queue.enqueue(RawItem(source="t", text="x"))
    first = await queue.next_ready()
    await queue.fail(first.id, "boom")

    second = await queue.next_ready()
    assert second.attempts == 1


async def test_backoff_delays_the_retry(tmp_path):
    q = JobQueue(tmp_path / "q.db", max_attempts=5, backoff_s=30.0)
    try:
        await q.enqueue(RawItem(source="t", text="x"))
        job = await q.next_ready()
        await q.fail(job.id, "boom")
        assert await q.next_ready() is None  # not due yet
    finally:
        q.close()


async def test_pending_job_replays_after_a_restart(tmp_path):
    path = tmp_path / "q.db"
    q1 = JobQueue(path)
    await q1.enqueue(RawItem(source="t", text="survive me"))
    await q1.next_ready()  # claimed but never completed -- simulates a crash
    q1.close()

    q2 = JobQueue(path)
    try:
        job = await q2.next_ready()
        assert job is not None and job.item.text == "survive me"
    finally:
        q2.close()


async def test_undo_pops_every_sink_of_the_most_recent_note(queue):
    await queue.record_object("chat1", "note-old", "anytype", "old-1", "Older")
    await queue.record_object("chat1", "note-new", "anytype", "new-1", "Newest")
    await queue.record_object("chat1", "note-new", "obsidian", "new-2", "Newest")

    popped = await queue.pop_last_objects("chat1")
    assert {r["object_id"] for r in popped} == {"new-1", "new-2"}

    # The previous note is now the most recent one.
    assert [r["object_id"] for r in await queue.pop_last_objects("chat1")] == ["old-1"]
    assert await queue.pop_last_objects("chat1") == []


async def test_undo_history_is_scoped_per_chat(queue):
    await queue.record_object("chat1", "n-a", "anytype", "a", "A")
    await queue.record_object("chat2", "n-b", "anytype", "b", "B")

    assert [r["object_id"] for r in await queue.pop_last_objects("chat2")] == ["b"]
    assert [r["object_id"] for r in await queue.pop_last_objects("chat1")] == ["a"]


async def test_trim_history_keeps_only_the_newest_entries(queue):
    for i in range(6):
        await queue.record_object("chat1", f"note-{i}", "anytype", f"id-{i}", f"N{i}")
    await queue.trim_history("chat1", keep=2)

    remaining = []
    while rows := await queue.pop_last_objects("chat1"):
        remaining.extend(r["object_id"] for r in rows)
    assert remaining == ["id-5", "id-4"]


async def test_trim_counts_notes_not_sink_rows(queue):
    """A note written to three sinks is one note for history purposes."""
    for i in range(3):
        for sink in ("anytype", "obsidian", "notion"):
            await queue.record_object("chat1", f"note-{i}", sink, f"{sink}-{i}", f"N{i}")
    await queue.trim_history("chat1", keep=2)

    first = await queue.pop_last_objects("chat1")
    second = await queue.pop_last_objects("chat1")
    assert len(first) == 3 and len(second) == 3
    assert await queue.pop_last_objects("chat1") == []
