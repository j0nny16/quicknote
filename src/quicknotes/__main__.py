"""CLI: run the bot, check the setup, or inspect the Anytype target type."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys

from .config import Config, ConfigError, load_config

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def _cmd_run(cfg: Config) -> int:
    from .app import App

    app = App(cfg)
    log = logging.getLogger("quicknotes")
    log.info(
        "starting: threshold=%s words, stt=%s, llm=%s, sinks=%s",
        cfg.threshold_words,
        cfg.transcriber.type,
        cfg.enricher.type,
        [s.name for s in app.sinks],
    )
    try:
        await app.run()
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    return 0


async def _cmd_doctor(cfg: Config) -> int:
    """Check every moving part independently and report what is not ready yet."""
    from .app import build_enricher, build_sinks, build_transcriber
    from .sources.telegram import TelegramSource

    checks: list[tuple[str, str, bool]] = []

    async def check(label: str, make, probe) -> None:
        component = None
        try:
            component = make()
            checks.append((label, await probe(component), True))
        except Exception as exc:
            checks.append((label, f"{type(exc).__name__}: {exc}", False))
        finally:
            if component is not None:
                with contextlib.suppress(Exception):
                    await component.aclose()

    await check(
        "telegram",
        lambda: TelegramSource(
            cfg.source.token(),
            allowed_user_ids=cfg.source.allowed_user_ids,
            audio_dir=cfg.audio_dir,
            on_item=None,  # type: ignore[arg-type]
            on_undo=None,  # type: ignore[arg-type]
            on_status=None,  # type: ignore[arg-type]
        ),
        lambda c: c.health(),
    )
    await check("whisper", lambda: build_transcriber(cfg), lambda c: c.health())
    await check("claude", lambda: build_enricher(cfg), lambda c: c.health())

    try:
        sinks = build_sinks(cfg)
    except Exception as exc:
        checks.append(("anytype", f"{type(exc).__name__}: {exc}", False))
        sinks = []
    for sink in sinks:
        try:
            checks.append((f"sink:{sink.name}", await sink.health(), True))
        except Exception as exc:
            checks.append((f"sink:{sink.name}", f"{type(exc).__name__}: {exc}", False))
        finally:
            with contextlib.suppress(Exception):
                await sink.aclose()

    width = max(len(label) for label, _, _ in checks)
    print()
    for label, message, ok in checks:
        print(f"  {'✅' if ok else '❌'}  {label.ljust(width)}  {message}")
    failed = [label for label, _, ok in checks if not ok]
    print()
    if failed:
        print(f"Not ready: {', '.join(failed)}")
        return 1
    print("All checks passed — QuickNotes is ready.")
    return 0


async def _cmd_introspect(cfg: Config) -> int:
    """Print the spaces the bot can see and the properties of the target type."""

    sc = cfg.sinks[0]
    from .sinks.anytype import AnytypeSink

    sink = AnytypeSink(
        sc.base_url,
        sc.api_key(),
        sc.space_id or "",
        type_key=sc.type_key,
        api_version=sc.api_version,
        timeout_s=sc.timeout_s,
    )
    try:
        print("\n== spaces the bot has joined ==")
        spaces = await sink.list_spaces()
        if not spaces:
            print("  (none — run 'anytype space join <invite-link>' and approve it)")
        for space in spaces:
            marker = " <- configured" if space.get("id") == sc.space_id else ""
            print(f"  {space.get('id')}  {space.get('name')!r}{marker}")

        if not sc.space_id:
            print("\nSet sink.space_id in config.yaml, then run introspect again.")
            return 1

        print(f"\n== types in space {sc.space_id} ==")
        types = await sink.list_types()
        for t in types:
            marker = " <- configured" if t.get("key") == sc.type_key else ""
            print(f"  key={t.get('key')!r:28} name={t.get('name')!r}{marker}")

        target = next((t for t in types if t.get("key") == sc.type_key), None)
        if target is None:
            print(f"\n❌ type_key {sc.type_key!r} not found in this space.")
            return 1

        print(f"\n== properties of type {sc.type_key!r} ==")
        props = target.get("properties") or []
        if not props:
            print("  (none reported — the note still works via name/description/body)")
        for prop in props:
            print(f"  key={prop.get('key')!r:28} format={prop.get('format')!r:16} name={prop.get('name')!r}")
        print(
            "\nMap these into sink.property_map in config.yaml, e.g.\n"
            "    property_map:\n"
            "      summary: <property key>\n"
            "      raw_transcript: <property key>\n"
        )
        print("Raw type payload:")
        print(json.dumps(target, indent=2, ensure_ascii=False)[:2000])
        return 0
    finally:
        await sink.aclose()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quicknotes", description=__doc__)
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "doctor", "introspect"],
        help="run the bot (default), check the setup, or inspect the Anytype type",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    handler = {"run": _cmd_run, "doctor": _cmd_doctor, "introspect": _cmd_introspect}[
        args.command
    ]
    try:
        return asyncio.run(handler(cfg))
    except KeyboardInterrupt:
        return 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
