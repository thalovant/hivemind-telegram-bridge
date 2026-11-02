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
import os
from typing import Mapping, Optional, Sequence

from ovos_utils.log import LOG

from hivemind_telegram_bridge import HiveMindTelegramBridge

#: option -> environment variable it falls back to.
ENVIRONMENT = {
    "token": "TELEGRAM_BOT_TOKEN",
    "allowed_chats": "TELEGRAM_ALLOWED_CHATS",
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
                                 allowed_chats=None):
    bridge = HiveMindTelegramBridge(
        token=token, key=key, password=password, host=host, port=port,
        self_signed=self_signed, lang=lang, site_id=site_id,
        allowed_chats=allowed_chats,
    )
    bridge.start()
    return bridge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge a Telegram bot to a HiveMind node",
        epilog="Every option falls back to an environment variable "
               "(TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHATS, "
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
    }


def main(argv: Optional[Sequence[str]] = None):
    options = resolve_options(argv)
    LOG.info("bridge starting; press Ctrl-C to stop")
    try:
        connect_telegram_to_hivemind(**options)
    except KeyboardInterrupt:
        LOG.info("shutting down")


if __name__ == '__main__':
    main()
