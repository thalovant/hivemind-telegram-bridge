# HiveMind Telegram Bridge

This bridges a Telegram bot to a HiveMind node. A HiveMind bridge is a
satellite whose input and output are a chat platform instead of a
microphone: messages sent to the bot become HiveMind utterances, and the
hub's spoken replies are posted back into the same Telegram chat.

New here? [docs/setup.md](docs/setup.md) walks through all of this from
scratch, including the `allow-msg` whitelist step below and how to
verify a message round-trips.

## Getting a bot token

Talk to [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
and follow the prompts. BotFather gives you a token that looks like
`123456789:AAExampleTokenDoNotShareThisInAnyForm`. Treat it like a
password: anyone who has it can send messages as your bot. Do not commit
it to a repo or paste it into a public issue.

## Registering the bridge on the hub

Every HiveMind client needs credentials and, separately, permission to
send the message types it uses. On the machine running `hivemind-core`:

```bash
hivemind-core add-client
```

This prints an access key and password; pass them to the bridge as
`--access-key` / `--password` (or store them once with
`hivemind-client set-identity` and omit the flags).

A freshly added client is denied every message type by default. The
bridge needs at least:

```bash
hivemind-core allow-msg recognizer_loop:utterance <client_id>
hivemind-core allow-msg speak <client_id>
```

`<client_id>` is printed by `add-client` (and by `hivemind-core
list-clients` afterwards). Skipping this step is the single most common
reason a bridge "connects fine" but nothing ever seems to happen: the hub
silently drops every message the client sends until it is whitelisted.

## Running the bridge

```bash
pip install .
hivemind-telegram-bridge \
  --token <your-botfather-token> \
  --access-key <key> --password <password> \
  --host ws://127.0.0.1 --port 5678
```

By default the bridge answers any chat it has been added to or that
messages it directly. Pass `--allowed-chat <chat_id>` (repeatable) to
restrict it to specific chats.

In a group, the bridge forwards only messages that mention the bot
(`@botname what time is it`) or reply to one of its messages, and removes
the mention before forwarding, so the hub is asked `what time is it`.
Private chats need no mention. Pass `--no-require-mention` to forward every
group message the bot receives (Telegram only delivers them all when the
bot's privacy mode is off).

Each Telegram chat is one conversation on the hub, so in a group one
person's follow-up ("and tomorrow?") continues whatever the last person
asked. Pass `--session-scope person` to give each person their own
conversation in each chat.

`--health-port <port>` serves two endpoints for container platforms:
`/healthz` answers 200 while the process runs, and `/readyz` answers 200
only while the HiveMind session is up and Telegram is being polled (503
otherwise). They serve no data and accept no input.

Useful flags:

- `--site-id`: this bridge's HiveMind site id. If you run more than one
  bridge (or more than one instance of this bridge) on the same host,
  give each a distinct site id — otherwise they collide over the same
  identity file and pinned peer keys.
- `--self-signed`: accept a self-signed TLS certificate on `wss://` hubs.
- `--lang`: the language tag attached to forwarded utterances (default
  `en-us`).

Run `hivemind-telegram-bridge --help` for the full list.

Every flag also falls back to an environment variable, so the bridge can
run with no secret on its command line, where any other process on the
host could read it:

| Flag | Variable |
| --- | --- |
| `--token` | `TELEGRAM_BOT_TOKEN` |
| `--allowed-chat` | `TELEGRAM_ALLOWED_CHATS` (ids separated by commas) |
| `--no-require-mention` | `TELEGRAM_REQUIRE_MENTION` (`true`/`false`) |
| `--session-scope` | `TELEGRAM_SESSION_SCOPE` (`chat`/`person`) |
| `--health-port` | `BRIDGE_HEALTH_PORT` |
| `--access-key` | `HIVEMIND_ACCESS_KEY` |
| `--password` | `HIVEMIND_PASSWORD` |
| `--host` | `HIVEMIND_HOST` |
| `--port` | `HIVEMIND_PORT` |
| `--site-id` | `HIVEMIND_SITE_ID` |
| `--self-signed` | `HIVEMIND_SELF_SIGNED` (`true`/`false`) |
| `--lang` | `HIVEMIND_LANG` |

The token, access key and password also accept `<VARIABLE>_FILE`, the
path of a file holding the value, which is how container platforms mount
secrets. A flag wins over its variable, and the variable over its file.

## Docker

```bash
docker build -t hivemind-telegram-bridge .
docker run --rm \
  -e TELEGRAM_BOT_TOKEN=... \
  -e HIVEMIND_ACCESS_KEY=... \
  -e HIVEMIND_PASSWORD=... \
  -e HIVEMIND_HOST=ws://hivemind-core \
  hivemind-telegram-bridge
```

or via `docker-compose.yml` — copy it, fill in the environment section,
and `docker compose up`.

The image runs as an unprivileged user and keeps the HiveMind identity in
`/home/bridge/.config/hivemind`. Mount a volume there so the identity
survives a restart: the hub pins the identity's key, and a new one on
every start is refused.

## What this bridge does, precisely

- Connects to Telegram with `python-telegram-bot`'s async `Application`
  and long-polling; connects to the HiveMind hub with
  `hivemind_bus_client.HiveMessageBusClient`.
- Skips its own messages (so it can never talk to itself) and any
  non-text message.
- Only forwards messages once the HiveMind handshake has completed —
  forwarding earlier would get the connection killed by the hub instead
  of just failing the one message.
- Forwards each remaining message as a `recognizer_loop:utterance` bus
  message, carrying the Telegram chat id and user in the message
  context so the hub's `speak` reply can be routed back to the right
  chat.
- Posts `speak` replies (and a fixed fallback line on
  `hive.complete_intent_failure`) back into the originating chat.

## Testing

```bash
pip install -e .[test]
pytest tests/
```

The test suite mocks both the Telegram `Application` and the HiveMind
`HiveMessageBusClient`, so it runs without a live token or hub. It has
not been exercised against a real Telegram bot or a real HiveMind hub —
that needs an actual bot token, which this repository does not have.
