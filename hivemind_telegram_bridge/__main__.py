"""CLI entry point for the HiveMind <-> Telegram bridge.

HiveMind identity (key/password/host/port) defaults to the values stored
by ``hivemind-client set-identity``; flags override them.

Every option can also come from the environment, so a container never has
to put a secret on its command line, where any process on the host can
read it. Secrets additionally accept a ``*_FILE`` variable naming a file
that holds the value, the convention for secrets mounted into a container.
A flag wins over its environment variable, and the variable over its file.
"""
import argparse
import math
import os
from typing import Mapping, Optional, Sequence

from ovos_utils.log import LOG

import hivemind_telegram_bridge
from hivemind_telegram_bridge import REPLY_TIMEOUT, HiveMindTelegramBridge, TokenRejected

#: option -> environment variable it falls back to.
ENVIRONMENT = {
    "token": "TELEGRAM_BOT_TOKEN",
    "allowed_chats": "TELEGRAM_ALLOWED_CHATS",
    "require_mention": "TELEGRAM_REQUIRE_MENTION",
    "session_scope": "TELEGRAM_SESSION_SCOPE",
    "health_port": "BRIDGE_HEALTH_PORT",
    "reply_timeout": "BRIDGE_REPLY_TIMEOUT",
    "key": "HIVEMIND_ACCESS_KEY",
    "password": "HIVEMIND_PASSWORD",
    "host": "HIVEMIND_HOST",
    "port": "HIVEMIND_PORT",
    "site_id": "HIVEMIND_SITE_ID",
    "self_signed": "HIVEMIND_SELF_SIGNED",
    "lang": "HIVEMIND_LANG",
}
#: options whose value may be read from the file named by ``<VARIABLE>_FILE``.
SECRETS = ("token", "key", "password")

_TRUE = {"1", "true", "yes", "on"}


def connect_telegram_to_hivemind(token, key=None, password=None,
                                 host=None, port=5678, self_signed=False,
                                 lang="en-us", site_id="telegram",
                                 allowed_chats=None, require_mention=True,
                                 session_scope="chat", health_port=None,
                                 reply_timeout=REPLY_TIMEOUT):
    bridge = HiveMindTelegramBridge(
        token=token, key=key, password=password, host=host, port=port,
        self_signed=self_signed, lang=lang, site_id=site_id,
        allowed_chats=allowed_chats, require_mention=require_mention,
        session_scope=session_scope, health_port=health_port,
        reply_timeout=reply_timeout,
    )
    bridge.start()
    return bridge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge a Telegram bot to a HiveMind node",
        epilog="Every option falls back to an environment variable "
               "(TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHATS, TELEGRAM_REQUIRE_MENTION, TELEGRAM_SESSION_SCOPE, BRIDGE_HEALTH_PORT, "
               "BRIDGE_REPLY_TIMEOUT, "
               "HIVEMIND_ACCESS_KEY, HIVEMIND_PASSWORD, HIVEMIND_HOST, "
               "HIVEMIND_PORT, HIVEMIND_SITE_ID, HIVEMIND_SELF_SIGNED, "
               "HIVEMIND_LANG). The token, access key and password also "
               "accept <VARIABLE>_FILE naming a file that holds the value.")
    # Telegram
    parser.add_argument("--token", default=None,
                        help="Telegram bot token from @BotFather")
    parser.add_argument("--allowed-chat", dest="allowed_chats",
                        action="append", type=int, default=None,
                        help="Telegram chat id allowed to talk to the bridge "
                             "(repeatable); default: any chat the bot is in")
    parser.add_argument("--no-require-mention", dest="require_mention",
                        action="store_false", default=None,
                        help="in a group, forward every message, not only those "
                             "that mention the bot or reply to it")
    parser.add_argument("--session-scope", choices=("chat", "person"), default=None,
                        help="one hub conversation per chat (default) or per "
                             "person in each chat")
    parser.add_argument("--health-port", type=int, default=None,
                        help="serve /healthz and /readyz on this port (default: off)")
    parser.add_argument("--reply-timeout", type=float, default=None,
                        help="seconds to wait for the hub's answer, showing typing..., "
                             "before saying it cannot answer right now; 0 turns "
                             f"this off (default: {REPLY_TIMEOUT:g})")
    # HiveMind
    parser.add_argument("--access-key", dest="key", default=None,
                        help="HiveMind access key (default: from identity file)")
    parser.add_argument("--password", default=None,
                        help="HiveMind password (default: from identity file)")
    parser.add_argument("--host", default=None,
                        help="HiveMind host, e.g. ws://127.0.0.1 (default: from identity file)")
    parser.add_argument("--port", type=int, default=None,
                        help="HiveMind port (default: 5678)")
    parser.add_argument("--site-id", default=None,
                        help="this bridge's HiveMind site id (default: telegram)")
    parser.add_argument("--self-signed", action="store_true", default=None,
                        help="accept self-signed SSL certificates")
    parser.add_argument("--lang", default=None, help="utterance language (default: en-us)")
    return parser


def _read_secret_file(path: str, variable: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as error:
        raise SystemExit(f"{variable}: cannot read {path}: {error.strerror}") from None


def _from_environment(option: str, environ: Mapping[str, str]) -> Optional[str]:
    variable = ENVIRONMENT[option]
    value = environ.get(variable)
    if value:
        return value
    if option in SECRETS:
        path = environ.get(f"{variable}_FILE")
        if path:
            return _read_secret_file(path, f"{variable}_FILE")
    return None


def resolve_options(argv: Optional[Sequence[str]] = None,
                    environ: Optional[Mapping[str, str]] = None) -> dict:
    """The bridge's settings: flags first, then the environment, then defaults.

    Raises ``SystemExit`` with a usage message when no bot token is given
    anywhere, or when a numeric setting is not a number.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    def pick(option):
        given = getattr(args, option)
        return given if given is not None else _from_environment(option, environ)

    token = pick("token")
    if not token:
        parser.error("a bot token is required: --token, TELEGRAM_BOT_TOKEN "
                     "or TELEGRAM_BOT_TOKEN_FILE")

    allowed_chats = args.allowed_chats
    if allowed_chats is None:
        listed = _from_environment("allowed_chats", environ)
        if listed:
            try:
                allowed_chats = [int(item) for item in listed.replace(",", " ").split()]
            except ValueError:
                parser.error("TELEGRAM_ALLOWED_CHATS must list chat ids separated "
                             "by commas or spaces")

    port = pick("port")
    try:
        port = int(port) if port is not None else 5678
    except ValueError:
        parser.error("HIVEMIND_PORT must be a number")

    self_signed = args.self_signed
    if self_signed is None:
        self_signed = str(environ.get(ENVIRONMENT["self_signed"], "")).lower() in _TRUE

    require_mention = args.require_mention
    if require_mention is None:
        setting = environ.get(ENVIRONMENT["require_mention"])
        require_mention = True if not setting else setting.lower() in _TRUE

    health_port = pick("health_port")
    try:
        health_port = int(health_port) if health_port is not None else None
    except ValueError:
        parser.error("BRIDGE_HEALTH_PORT must be a number")

    reply_timeout = pick("reply_timeout")
    try:
        reply_timeout = float(reply_timeout) if reply_timeout is not None else REPLY_TIMEOUT
    except ValueError:
        parser.error("BRIDGE_REPLY_TIMEOUT must be a number of seconds")
    if not math.isfinite(reply_timeout) or reply_timeout < 0:
        parser.error("BRIDGE_REPLY_TIMEOUT must be a finite number of seconds, 0 or more")

    session_scope = pick("session_scope") or "chat"
    if session_scope not in ("chat", "person"):
        parser.error("TELEGRAM_SESSION_SCOPE must be 'chat' or 'person'")

    host = pick("host")
    if host and not host.startswith("ws://") and not host.startswith("wss://"):
        host = "ws://" + host

    return {
        "token": token,
        "key": pick("key"),
        "password": pick("password"),
        "host": host,
        "port": port,
        "self_signed": self_signed,
        "lang": pick("lang") or "en-us",
        "site_id": pick("site_id") or "telegram",
        "allowed_chats": allowed_chats,
        "require_mention": require_mention,
        "session_scope": session_scope,
        "health_port": health_port,
        "reply_timeout": reply_timeout,
    }


def main(argv: Optional[Sequence[str]] = None):
    """Run the bridge until stopped.

    When Telegram refuses the bot token, writes ``token_rejected`` to the
    termination log and exits with status 3: restarting cannot help, the
    token has to be replaced.
    """
    options = resolve_options(argv)
    LOG.info("bridge starting; press Ctrl-C to stop")
    try:
        connect_telegram_to_hivemind(**options)
    except TokenRejected:
        LOG.error("Telegram rejected the bot token; replace it to start the bridge again")
        hivemind_telegram_bridge.report_token_rejected()
        raise SystemExit(hivemind_telegram_bridge.EXIT_TOKEN_REJECTED) from None
    except KeyboardInterrupt:
        LOG.info("shutting down")


if __name__ == '__main__':
    main()
