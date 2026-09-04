# QuickNotes

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

Speak a thought into Telegram, find it in Anytype.

A voice message (or a typed one) goes to a private Telegram bot, gets transcribed
by a local Whisper container, gets a title and a short summary from Claude Haiku,
and lands as a `Quicknote` object in your Anytype space — synced to every device.

```
Telegram ──► queue ──► whisper (local) ──► Haiku ──► Anytype
 long poll    SQLite      transcript        title      object
                                            summary
```

## Design notes

**Nothing listens on the internet.** The bot uses Telegram long polling, so it
dials out. No published host port, no reverse proxy, no TLS certificate. Whisper
and Anytype are reachable only inside the compose network.

**The Anytype bot is not you.** `anytype-cli` only supports dedicated bot
accounts — a recovery phrase cannot be imported. The bot joins *one* shared space
as an Editor, so a compromised VPS exposes that one space and the API keys, never
your vault or identity. Revoke it any time from the member list.

**Short notes cost nothing.** Under `threshold_words` (default 10) no model is
called at all: the note text is its own title. A four-word reminder does not need
an AI title.

**Nothing gets lost.** Captures are written to a SQLite queue before any network
call. Failures retry with backoff; after `max_attempts` the job is dead-lettered
and the bot says so instead of failing silently. A crash mid-processing replays
the job on restart.

**Every stage is swappable.** `Source`, `Transcriber`, `Enricher` and `Sink` are
protocols; the sinks are a fan-out list. Adding Obsidian or Notion is one file
plus a config entry. Moving transcription to Groq is a `base_url` change.

## Setup

Requires Docker and Docker Compose on the host. Everything runs as containers.

### 1. Gather credentials

| What | Where |
|---|---|
| Telegram bot token | [@BotFather](https://t.me/BotFather) → `/newbot` |
| Your Telegram user id | [@userinfobot](https://t.me/userinfobot) → `/start` |
| Anthropic API key | console.anthropic.com |

```bash
cp .env.example .env   # then fill in the three values above
```

Leave `ANYTYPE_API_KEY` and `ANYTYPE_SPACE_ID` empty for now — they come from
step 3.

### 2. Build

```bash
docker compose build
```

### 3. Pull the Whisper model

The whisper server ships without weights and downloads them on request. Pull the
model once (~1.5 GB, cached in a volume):

```bash
docker compose up -d whisper
docker compose exec whisper \
  curl -X POST http://localhost:8000/v1/models/deepdml/faster-whisper-large-v3-turbo-ct2
```

### 4. Pair the Anytype bot

The server has to be running before an account can be created against it -- and
the HTTP API only binds once that account is logged in:

```bash
# 1. Start the server. It comes up without an account ("skipping auto-login").
docker compose up -d anytype-cli

# 2. Create the bot identity. Save the printed account key somewhere safe --
#    it is the only way to log this bot in again on another machine.
docker compose exec anytype-cli anytype auth create quicknote-bot
```

In your personal Anytype: open the space the notes should land in, make sure your
**Quicknote** type exists there, then Space settings → share via link → copy the
invite link.

```bash
docker compose exec anytype-cli anytype space join "https://invite.any.coop/..."
```

Approve the join request in your personal Anytype **as Editor**. Then:

```bash
docker compose exec anytype-cli anytype space list                       # -> ANYTYPE_SPACE_ID
docker compose exec anytype-cli anytype auth apikey create quicknotes    # -> ANYTYPE_API_KEY
```

Put both into `.env`.

### 5. Check and start

```bash
docker compose run --rm quicknotes doctor
```

Every line should be green. Then:

```bash
docker compose up -d
```

Send the bot a voice message.

### 6. Optional: structured properties

The note works without this — the title becomes the object name, the summary its
description, the text its Markdown body. To also fill custom properties of your
Quicknote type:

```bash
docker compose run --rm quicknotes introspect
```

Copy the property keys it prints into `property_map` in `config.yaml`, then
`docker compose up -d --force-recreate quicknotes`.

## Commands

In the chat: `/undo` removes the last note, `/status` reports queue depth and
backend health, `/help` explains itself.

On the host:

```bash
docker compose run --rm quicknotes doctor      # check every backend
docker compose run --rm quicknotes introspect  # inspect the Anytype type
docker compose logs -f quicknotes              # follow
```

## Tuning

`config.yaml` carries the wiring; `.env` carries the secrets.

**Transcription quality vs. speed** — `WHISPER__MODEL` in `compose.yaml`. Any
model you switch to has to be pulled the same way as in step 3:

Measured on a 4 vCPU / 8 GB VPS with an 18.2 s German clip, model warm:

| Model | Time | Extrapolated 60 s note |
|---|---|---|
| `Systran/faster-whisper-small` | 7.7 s | ~25 s |
| `Systran/faster-whisper-medium` | 21.8 s | ~72 s |
| `deepdml/faster-whisper-large-v3-turbo-ct2` (default) | 25.4 s | ~84 s |

Turbo is barely faster than medium here because it only distils the *decoder* --
the encoder is still large-v3 sized, and on CPU the encoder dominates. All three
transcribed the test clip identically, but that was clean synthetic speech; the
gap opens up on real voice notes with noise, accents and false starts. Default is
turbo for accuracy; switch to `small` if you would rather have notes back in
about a quarter of the time.

**Use a hosted STT instead** — point the transcriber at Groq, no code change:

```yaml
transcriber:
  base_url: https://api.groq.com/openai/v1
  model: whisper-large-v3-turbo
  api_key_env: GROQ_API_KEY
```

**Run without any LLM** — set `enricher.type: noop`. Notes keep their own text as
the title and no summary is generated.

## Troubleshooting

### `space join` fails with `DeadlineExceeded ... RST_STREAM ... CANCEL`

Known upstream bug: [anytype-cli#59](https://github.com/anyproto/anytype-cli/pull/59).
The headless CLI never sets `PreferYamuxTransport`, so anytype-heart prefers QUIC.
Where QUIC stalls, anything that fetches from the network dies on a deadline --
while TCP-based calls (creating objects, listing spaces) keep working, which
makes it look like a permissions problem rather than a transport one.

`docker/anytype-entrypoint.sh` works around it by stripping the `quic://` entries
from the node's peer list, leaving only TCP. It re-applies every 15 s because
heart re-fetches the config from the coordinator. Remove the workaround once the
upstream fix ships.

To confirm this is what you are hitting: the node has healthy TCP connections
(`cat /proc/net/tcp` inside the container shows established sessions to
`51.77.x` / `5.39.x`) and the invite is fetchable over HTTPS
(`curl https://invite.any.coop/<cid>` returns ~3 KB), yet heart logs *nothing at
all* during the failing join.

## Development

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

### Adding a backend

Each stage is a `Protocol` in its package's `base.py`. A new one is a single
module plus a branch in the matching `build_*` factory in `app.py`:

- **Sink** (`create`, `delete`, `health`, `aclose`) — an Obsidian sink writes
  Markdown into a synced folder; a Notion sink posts to its API. Sinks are a
  list, so notes can go to several places at once.
- **Source** (`run`, `notify`, `health`) — anything that can hand over text or
  audio: email, a webhook, another chat network.
- **Transcriber** / **Enricher** — swap the STT or LLM provider.

Nothing else needs to change: `config.yaml` picks the implementation by `type`.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

If you run a modified version as a network service, the AGPL requires you to
offer its source to that service's users.

## Disclaimer

Not affiliated with or endorsed by Anytype, Telegram or Anthropic. Anytype's
local API and `anytype-cli` are young and moving; see Troubleshooting above for
the workaround this project currently carries.

## Layout

```
src/quicknotes/
  models.py       RawItem, Note, Transcript, Enrichment
  config.py       YAML + env; *_env fields name a variable, never a value
  queue.py        durable SQLite queue, retries, /undo history
  pipeline.py     raw -> note, threshold logic, sink fan-out
  app.py          wiring, worker loop, undo, status
  sources/        telegram
  transcribers/   openai_compatible (local | groq | openai), passthrough
  enrichers/      anthropic, noop
  sinks/          anytype
```
