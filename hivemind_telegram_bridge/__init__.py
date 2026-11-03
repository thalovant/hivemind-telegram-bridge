"""HiveMind <-> Telegram bridge.

A HiveMind bridge is a satellite whose input and output are a chat
platform instead of a microphone. This one uses
:class:`~hivemind_bus_client.HiveMessageBusClient` directly (composition,
same shape as ``HiveMind_mattermost_bridge``'s ``HiveMindMattermostBridge``)
and ``python-telegram-bot`` v21+'s async ``Application`` for the Telegram
side.

Connection lifecycle, spelled out because getting it wrong is the
recurring bug across every HiveMind bridge written so far:

- ``HiveMessageBusClient.connect()`` already starts and owns the
  reconnect worker in a background thread, and blocks synchronously
  until the handshake completes (or fails). Call it exactly once, from
  ``start()``. Do not also call ``run_forever()`` afterwards to "keep it
  running" -- there is nothing left to start, the connection is already
  live in its own thread for as long as the process runs.
- Nothing is forwarded to HiveMind before ``connect()`` returns, and
  Telegram's own bot messages are filtered out before they are ever
  considered, so neither an unauthenticated connection nor a feedback
  loop of the bot talking to itself reaches the hub.
- host/port are configuration, not constants (``ws://127.0.0.1:5678``
  is only ever a default value).
- the hub decides which message types this client may send (on a
  self-managed hub an admin runs ``hivemind-core allow-msg
  recognizer_loop:utterance <client_id>``); this bridge cannot do that
  step itself. It says so in its log only when the hub actually refuses
  an utterance for that reason (``hive.policy.denied``, code
  ``acl_disallowed_type``). See the README.

Threads, and why neither side waits for the other:

- the asyncio loop runs python-telegram-bot (getUpdates, sendMessage,
  "typing…") and owns every piece of waiting state;
- the HiveMind client's bus thread delivers the hub's messages one after
  the other. ``speak()`` only schedules the send on the loop and returns:
  a send that waited there would hold up every later hub message;
- one hub-send thread runs every ``emit()``: never on the loop, where a
  slow hub socket would hold up every reply, and one at a time, in order.
"""
import asyncio
import logging
import math
import re
import threading
import time
import warnings
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from http import HTTPStatus
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from hivemind_bus_client import (
    HiveMessage,
    HiveMessageType,
    HiveMessageBusClient,
)
from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from hivemind_telegram_bridge.health import HealthServer
from hivemind_telegram_bridge.lines import NO_ANSWER, UNAVAILABLE, bridge_line
from hivemind_telegram_bridge.split import TELEGRAM_LIMIT, split_message
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

platform = "HiveMindTelegramBridgeV0.1"

# hivemind-bus-client >= 1.0.13a1 makes HiveMessageBusClient.connect()
# block on the handshake; handshake_max_retries=None (the client's own
# default) retries forever. Bound it here so a stalled/unreachable hub
# (down, wrong password) fails fast instead of hanging the bridge.
DEFAULT_HANDSHAKE_MAX_RETRIES = 10

#: Chat types where several people talk and the bot is one participant.
GROUP_CHAT_TYPES = frozenset({"group", "supergroup", "channel"})

#: How conversations are kept apart on the hub. ``chat``: one session per
#: Telegram chat, shared by everyone in a group. ``person``: one session per
#: person in each chat, so in a group each person's follow-up questions
#: continue their own conversation.
SESSION_SCOPES = ("chat", "person")

#: A message older than this (seconds) when the bridge reads it is not
#: answered: Telegram keeps a bot's messages for up to 24 hours while it is
#: down, and a reply to a question asked long before means nothing any more.
MAX_MESSAGE_AGE = 120

#: How many (chat, person) languages are remembered for the bridge's own lines.
LANGUAGES_KEPT = 4096

#: ovos-core's "no skill could answer" and the name older hubs relayed it under.
INTENT_FAILURE_TYPES = ("complete_intent_failure", "hive.complete_intent_failure")
#: ovos-core's "this utterance is dealt with", sent after the answer, or
#: alone when the hub chose not to answer (a cancelled utterance).
HANDLED_TYPE = "ovos.utterance.handled"
#: HiveMind-core's refusal of a message (quota, rate limit, agent down).
DENIED_TYPE = "hive.policy.denied"
#: The refusal code for a message type the hub does not allow this client.
ACL_DISALLOWED_TYPE = "acl_disallowed_type"

#: Seconds the bridge waits for the hub's answer, showing "typing…", before
#: it says it cannot answer. ``0`` turns waiting off (no typing, no line).
REPLY_TIMEOUT = 60.0
#: Telegram shows "typing…" for 5 s after a chat action: renew it before.
TYPING_EVERY = 4.0
#: A reply Telegram's flood control holds back (429, "retry after N s") is
#: sent again after exactly the wait Telegram asks for, as long as the waits
#: for that reply add up to no more than this many seconds; a longer one is
#: given up at once (and logged) rather than holding the chat's later
#: replies behind it.
RETRY_AFTER_BUDGET = 120.0
#: When the bridge stops, replies already being sent get this long to finish.
SEND_DRAIN = 5.0
#: How long a question the bridge stopped waiting for is remembered, so a
#: late answer to it is recognised and does not end other people's waits.
ENDED_KEPT = 600.0
#: The "cannot answer right now" line is said at most once per person and
#: chat in this many seconds, however many questions go unanswered.
UNAVAILABLE_EVERY = 30.0

#: What the bridge writes to the termination log when Telegram refuses the
#: bot token, so the platform can ask for a new token instead of restarting
#: a bridge that can never log in.
TOKEN_REJECTED = "token_rejected"
#: Where a container says why it stopped (Kubernetes' default
#: ``terminationMessagePath``).
TERMINATION_LOG = "/dev/termination-log"
#: Exit status when Telegram refuses the bot token.
EXIT_TOKEN_REJECTED = 3

# httpx logs every request URL at INFO, and a Telegram API URL carries the
# bot token (https://api.telegram.org/bot<token>/getUpdates). Keep those
# loggers quiet whatever level the rest of the process logs at.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class TokenRejected(RuntimeError):
    """Telegram refused the bot token.

    401 Unauthorized from any call, or ``InvalidToken`` while starting (a
    malformed token gets 404 from getMe). The message never contains the
    token: python-telegram-bot's own ``InvalidToken`` does, so it is not
    chained.
    """


def report_token_rejected(path: Optional[str] = None) -> None:
    """Write exactly ``token_rejected`` to the termination log.

    A file that cannot be written (not in a container, read-only) is
    ignored: the exit status still says the bridge stopped.
    """
    try:
        with open(path or TERMINATION_LOG, "w", encoding="utf-8") as handle:
            handle.write(TOKEN_REJECTED)
    except OSError:
        pass


class _TokenWatch(HTTPXRequest):
    """The HTTP transport, reporting every 401 Unauthorized from Telegram.

    python-telegram-bot raises ``InvalidToken`` for a 401 on getMe at start,
    but a 401 on getUpdates later (the token was revoked while the bridge
    runs) only ends its polling task: the process would stay up, look
    healthy and never hear another message. 409 Conflict (another process
    polls with the same token) is not a rejected token and is left to the
    library's retry loop.
    """

    def __init__(self, on_unauthorized: Callable[[], None], **kwargs):
        super().__init__(**kwargs)
        self._on_unauthorized = on_unauthorized

    async def do_request(self, *args, **kwargs):
        code, payload = await super().do_request(*args, **kwargs)
        if code == HTTPStatus.UNAUTHORIZED:
            self._on_unauthorized()
        return code, payload


class HiveMindTelegramBridge:
    """Bridge a Telegram bot to a HiveMind node."""

    def __init__(self,
                 token: Optional[str] = None,
                 key: Optional[str] = None,
                 password: Optional[str] = None,
                 host: Optional[str] = None,
                 port: int = 5678,
                 self_signed: bool = False,
                 lang: str = "en-us",
                 site_id: str = "telegram",
                 allowed_chats: Optional[Iterable[int]] = None,
                 handshake_max_retries: int = DEFAULT_HANDSHAKE_MAX_RETRIES,
                 require_mention: bool = True,
                 session_scope: str = "chat",
                 health_port: Optional[int] = None,
                 max_message_age: float = MAX_MESSAGE_AGE,
                 reply_timeout: float = REPLY_TIMEOUT,
                 *,
                 client: Optional[HiveMessageBusClient] = None,
                 app: Optional[Application] = None):
        """
        Parameters
        ----------
        token: Telegram bot token from @BotFather. Required unless ``app``
            is supplied (tests inject a pre-built Application).
        key, password, host, port, self_signed: HiveMind hub connection.
        lang: default utterance language tag.
        site_id: this bridge's HiveMind site id.
        allowed_chats: if given, only messages from these Telegram chat
            ids are forwarded; everything else is ignored. Leave unset to
            accept DMs and any chat the bot has been added to.
        handshake_max_retries: bounds how many times ``connect()`` retries
            the HiveMind handshake before giving up. ``None`` retries
            forever, which hangs the bridge on a stalled/unreachable hub.
        require_mention: in a group, forward only messages that mention
            the bot (``@botname``) or reply to one of its messages. Private
            chats are always forwarded. With Telegram's privacy mode turned
            off a bot receives every message in a group, and without this
            the hub would be asked about every one of them.
        session_scope: ``"chat"`` (one hub session per Telegram chat) or
            ``"person"`` (one per person in each chat). In a private chat
            the two are the same conversation.
        health_port: if set, serve ``/healthz`` and ``/readyz`` on this
            port (see :mod:`hivemind_telegram_bridge.health`).
        max_message_age: messages sent longer ago than this many seconds
            are not answered (they were waiting while the bridge was
            down). They are still read, so Telegram does not send them
            again.
        reply_timeout: seconds to wait for the hub's answer, showing
            "typing…" in the chat, before saying the bridge cannot answer
            right now. A late answer is still delivered. ``0`` turns this
            off.
        client: pre-built HiveMessageBusClient (tests / advanced setups).
            NOTE: HiveMessageBusClient does NOT open a connection in
            __init__ -- call start() (or connect_hivemind()) to connect.
        app: pre-built telegram Application (tests).
        """
        if app is None and not token:
            raise ValueError("token is required unless an Application is injected")

        self.lang = lang
        self.site_id = site_id
        self.allowed_chats = set(allowed_chats) if allowed_chats else None
        self.handshake_max_retries = handshake_max_retries
        self.require_mention = require_mention
        if session_scope not in SESSION_SCOPES:
            raise ValueError(f"session_scope must be one of {SESSION_SCOPES}, "
                             f"not {session_scope!r}")
        self.session_scope = session_scope
        if not max_message_age >= 0:  # also refuses NaN
            raise ValueError(f"max_message_age must be >= 0, not {max_message_age!r}")
        self.max_message_age = max_message_age
        if not (math.isfinite(reply_timeout) and reply_timeout >= 0):
            raise ValueError("reply_timeout must be a finite number of seconds >= 0, "
                             f"not {reply_timeout!r}")
        self.reply_timeout = reply_timeout

        self._token_rejected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._own_requests: Tuple[HTTPXRequest, ...] = ()
        self.app: Application = app or self._build_application(token)
        # block=False: each message is handled as its own task, so a slow hub
        # send for one never holds up reading (and typing for) the next.
        # Hub sends still go out in order: each handler submits its send to
        # the hub-send thread before its first await.
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_telegram_message,
                           block=False)
        )

        self.client = client or HiveMessageBusClient(
            key=key,
            password=password,
            host=host,
            port=port,
            useragent=platform,
            self_signed=self_signed,
        )

        self.health_port = health_port
        self._health: Optional[HealthServer] = None
        self._polling = threading.Event()
        self._started = False
        self._connected = threading.Event()
        # Every hub send goes through this one thread: off the Telegram loop
        # (a slow or reconnecting socket must not hold up replies), and one
        # at a time, in order (the client's encrypted transport is not meant
        # to be written from two threads at once).
        self._hub_sender = ThreadPoolExecutor(max_workers=1,
                                              thread_name_prefix="hivemind-send")
        # chat id -> [lock, sends waiting]: replies to one chat go out in
        # the order the hub spoke them; different chats do not wait for
        # each other. Only touched on the Telegram loop.
        self._chat_sends: Dict[int, List] = {}
        # (chat id, user id) -> the language their Telegram app reports (None:
        # it reports none), for the lines the bridge says itself; written on
        # the hub-send thread, read on the bus thread.
        self._languages: "OrderedDict[Tuple[int, int], Optional[str]]" = OrderedDict()
        self._languages_lock = threading.Lock()
        # Questions waiting for the hub, by (chat id, session id); when the
        # last "typing…" went to each chat; when each was last told the hub
        # cannot answer. Only touched on the Telegram loop.
        self._waits: Dict[Tuple[int, str], "_Wait"] = {}
        self._typing_at: Dict[int, float] = {}
        self._unavailable_at: Dict[Tuple[int, Optional[int]], float] = {}
        self._ended: Dict[Tuple[int, str], float] = {}
        self._sending: Set[asyncio.Task] = set()
        self._background: Set[asyncio.Task] = set()
        self._told_about_acl = False  # bus thread only

    def _build_application(self, token: str) -> Application:
        """The Telegram Application, on a transport that reports a rejected token.

        Pool sizes are python-telegram-bot's own defaults (256 for API
        calls, 1 for getUpdates).
        """
        requests = (_TokenWatch(self._on_unauthorized, connection_pool_size=256),
                    _TokenWatch(self._on_unauthorized, connection_pool_size=1))
        self._own_requests = requests
        return (ApplicationBuilder().token(token)
                .request(requests[0])
                .get_updates_request(requests[1])
                .build())

    def _on_unauthorized(self) -> None:
        """Telegram answered 401: stop, and let ``start()`` report it."""
        if not self._token_rejected:
            LOG.error("Telegram answered 401 Unauthorized: the bot token is "
                      "wrong or was revoked")
        self._token_rejected = True
        if self._stop_event is not None:
            self._stop_event.set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect_hivemind(self) -> None:
        """Connect to the HiveMind hub and wait for the handshake.

        Calls ``HiveMessageBusClient.connect()`` exactly once; that call
        already starts and owns the reconnect worker thread. Never call
        ``run_forever()`` in addition to this -- it would try to claim
        the same lifecycle a second time.
        """
        self.client.connect(site_id=self.site_id,
                           handshake_max_retries=self.handshake_max_retries)
        self.client.on_mycroft("speak", self.handle_speak)
        for msg_type in INTENT_FAILURE_TYPES:
            self.client.on_mycroft(msg_type, self.handle_intent_failure)
        self.client.on_mycroft(HANDLED_TYPE, self.handle_handled)
        self.client.on_mycroft(DENIED_TYPE, self.handle_denied)
        self._connected.set()
        LOG.info("== connected to HiveMind")

    def start(self) -> None:
        """Connect to HiveMind, then run the Telegram bot until stopped.

        Blocks the calling thread for as long as the bot is polling
        Telegram. Intended to be the last call in a ``__main__``.

        Raises :class:`TokenRejected` when Telegram refuses the bot token,
        at start or later; the HiveMind connection is closed first.
        """
        if self._started:
            return
        self._started = True
        if self.health_port is not None:
            self._health = HealthServer(self.health_port, self.ready)
            self._health.start()
        self.connect_hivemind()
        asyncio.run(self._run_telegram())
        if self._token_rejected:
            try:
                self.client.close()
            except Exception:
                LOG.exception("error closing HiveMind client")
            if self._health is not None:
                self._health.stop()
                self._health = None
            self._started = False
            self._connected.clear()
            raise TokenRejected("Telegram rejected the bot token")

    async def _run_telegram(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        try:
            await self.app.initialize()
        except InvalidToken:
            # Its message holds the token: never log or re-raise it.
            self._token_rejected = True
            # Application.shutdown() does nothing after a failed initialize,
            # so close the HTTP clients this bridge made itself.
            for request in self._own_requests:
                try:
                    await request.shutdown()
                except Exception:
                    LOG.debug("error closing a Telegram HTTP client")
            return
        try:
            await self.app.start()
            try:
                await self.app.updater.start_polling()
            except InvalidToken:
                self._token_rejected = True
                return
            self._polling.set()
            LOG.info("== listening to Telegram")
            try:
                await self._stop_event.wait()
            finally:
                self._polling.clear()
                await self._drain_sends()
                await self.app.updater.stop()
        finally:
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()

    async def _drain_sends(self) -> None:
        """Give replies already being sent ``SEND_DRAIN`` seconds to finish.

        Stopping ends the loop, which cancels whatever is still running: a
        long reply would stop half way through its pieces.
        """
        pending = [task for task in self._sending if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=SEND_DRAIN)

    def stop(self) -> None:
        """Stop polling Telegram and close the HiveMind connection."""
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:  # the loop already ended
                pass
        try:
            self.client.close()
        except Exception:
            LOG.exception("error closing HiveMind client")
        self._started = False
        self._connected.clear()
        if self._health is not None:
            self._health.stop()
            self._health = None

    def ready(self) -> bool:
        """Whether a message can go end to end right now.

        The HiveMind session must be live, not just once established: the
        client clears its handshake event when the connection drops and sets
        it again on reconnect. And Telegram must be polled, by an updater
        that is still running.
        """
        handshake = getattr(self.client, "handshake_event", None)
        hub_up = self._connected.is_set() and (handshake is None or handshake.is_set())
        updater = getattr(self.app, "updater", None)
        polling = self._polling.is_set() and bool(getattr(updater, "running", True))
        return bool(hub_up and polling)

    # ------------------------------------------------------------------
    # Telegram -> HiveMind
    # ------------------------------------------------------------------
    async def handle_telegram_message(self, update: Update,
                                      context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward an inbound Telegram text message onto the HiveMind bus.

        Drops the message instead of forwarding when: it has no text
        (filters already restrict this, kept as a hard guard), it came
        from the bot itself (would otherwise create a feedback loop),
        it is outside ``allowed_chats`` (when configured), or HiveMind
        has not completed its handshake yet (forwarding before that
        point gets the connection killed by the hub).
        """
        message = update.effective_message
        if message is None or not message.text:
            return
        if self._is_stale(message):
            return

        sender = update.effective_user
        if sender is not None and sender.is_bot:
            bot_id = getattr(self.app.bot, "id", None)
            if bot_id is None or sender.id == bot_id:
                return

        chat = update.effective_chat
        if chat is None:
            return
        if self.allowed_chats is not None and chat.id not in self.allowed_chats:
            LOG.debug(f"ignoring message from non-allowed chat {chat.id}")
            return

        text = self.addressed_text(message, chat)
        if text is None:
            return

        if not self._connected.is_set():
            LOG.warning("dropping Telegram message, not connected to "
                       "HiveMind yet")
            return

        # Sent on the hub-send thread, never on this loop, so the loop keeps
        # sending replies while the hub socket is slow; "typing…" starts
        # while it goes.
        sent = asyncio.get_running_loop().run_in_executor(
            self._hub_sender, self.forward_to_hivemind, text, sender, chat.id)
        session_id = self.session_id(chat.id, sender)
        user_id = getattr(sender, "id", None)
        language = _language_code(sender)
        self._expect_answer(chat.id, session_id, user_id, language)
        try:
            await sent
        except Exception as error:
            # emit() puts the whole message, text included, in some of its
            # errors: name the kind of failure only.
            LOG.warning(f"could not send a message from chat {chat.id} to the "
                        f"hub: {type(error).__name__}")
            self._give_up(chat.id, session_id, user_id, language)

    def _is_stale(self, message) -> bool:
        """Whether ``message`` was sent more than ``max_message_age`` ago.

        Telegram holds a bot's updates while it is down and hands them all
        over on the first getUpdates. Reading them (python-telegram-bot
        confirms each by its offset) and not answering is what keeps an
        old question from being answered now, or ever again.
        """
        sent = getattr(message, "date", None)
        if not isinstance(sent, datetime):
            return False
        age = time.time() - sent.timestamp()
        if age <= self.max_message_age:
            return False
        LOG.debug(f"not answering a message sent {age:.0f} s ago")
        return True

    def addressed_text(self, message, chat) -> Optional[str]:
        """The text to forward, or ``None`` when the message is not for the bot.

        The bot's ``@username`` is removed wherever it appears: in a group a
        question reads "@botname what time is it", and the mention is not
        part of what was asked. In a group, with ``require_mention``, a
        message that neither mentions the bot nor replies to it is not
        addressed to the bot at all.
        """
        username = getattr(self.app.bot, "username", None)
        text = message.text
        mentioned = False
        if isinstance(username, str) and username:
            pattern = re.compile(rf"(?<![\w@])@{re.escape(username)}\b", re.IGNORECASE)
            text, count = pattern.subn(" ", text)
            mentioned = count > 0
            text = " ".join(text.split())
        if not text:
            return None
        if self.require_mention and getattr(chat, "type", None) in GROUP_CHAT_TYPES:
            if not (mentioned or self._replies_to_bot(message)):
                return None
        return text

    def _replies_to_bot(self, message) -> bool:
        replied = getattr(message, "reply_to_message", None)
        author = getattr(replied, "from_user", None) if replied is not None else None
        bot_id = getattr(self.app.bot, "id", None)
        return author is not None and bot_id is not None and getattr(author, "id", None) == bot_id

    def forward_to_hivemind(self, text: str, sender, chat_id: int) -> None:
        """Emit ``text`` to the hub as an utterance from ``sender`` in ``chat_id``.

        Blocking (it writes to the hub socket): ``handle_telegram_message``
        runs it on the hub-send thread.
        """
        username = (sender.username if sender and sender.username
                   else str(sender.id) if sender else "unknown")
        session_id = self.session_id(chat_id, sender)
        self._remember_language(chat_id, getattr(sender, "id", None), _language_code(sender))
        msg = Message(
            "recognizer_loop:utterance",
            {"utterances": [text], "lang": self.lang},
            {
                "source": platform,
                "destination": "HiveMind",
                "platform": platform,
                "chat_id": chat_id,
                "user": {"telegram_username": username,
                        "telegram_user_id": sender.id if sender else None},
                "session": {"session_id": session_id},
            },
        )
        self.client.emit(HiveMessage(HiveMessageType.BUS, msg))

    def _remember_language(self, chat_id: int, user_id: Optional[int],
                           language_code: Optional[str]) -> None:
        """Remember what ``user_id`` speaks in ``chat_id`` (``None``: not told)."""
        if user_id is None:
            return
        key = (chat_id, user_id)
        with self._languages_lock:
            self._languages[key] = language_code
            self._languages.move_to_end(key)
            while len(self._languages) > LANGUAGES_KEPT:
                self._languages.popitem(last=False)

    def _language_of(self, chat_id: int, user_id: Optional[int]) -> Optional[str]:
        with self._languages_lock:
            return self._languages.get((chat_id, user_id))

    def line(self, kind: str, language_code: Optional[str] = None) -> str:
        """A line the bridge says itself (see :mod:`hivemind_telegram_bridge.lines`).

        In ``language_code`` (the language the person's Telegram app reports)
        when there is a line in it, else in ``lang`` (``HIVEMIND_LANG``), else
        in English.
        """
        return bridge_line(kind, language_code, self.lang)

    def session_id(self, chat_id: int, sender) -> str:
        """The hub session a message from ``sender`` in ``chat_id`` belongs to."""
        if self.session_scope == "person" and sender is not None:
            return f"telegram-{chat_id}-{sender.id}"
        return f"telegram-{chat_id}"

    # ------------------------------------------------------------------
    # HiveMind -> Telegram
    # ------------------------------------------------------------------
    def handle_speak(self, message: Message) -> None:
        chat_id = message.context.get("chat_id")
        if chat_id is None:
            return
        utterance = message.data.get("utterance")
        if not utterance:
            return
        self.speak(utterance, chat_id, _session_of(message))

    def handle_intent_failure(self, message: Message) -> None:
        """No skill could answer: say so, in the person's language.

        The one bridge-authored reply to a hub message: the hub sent no
        utterance to relay. Current hubs relay ovos-core's
        ``complete_intent_failure``; older ones sent
        ``hive.complete_intent_failure``.
        """
        chat_id = message.context.get("chat_id")
        if chat_id is None:
            return
        LOG.info("the hub found no answer (complete intent failure)")
        # The hub echoes the question's context: the person who asked it.
        user = message.context.get("user")
        user_id = user.get("telegram_user_id") if isinstance(user, dict) else None
        language = self._language_of(chat_id, user_id)
        self.speak(self.line(NO_ANSWER, language), chat_id, _session_of(message))

    def handle_handled(self, message: Message) -> None:
        """The hub is done with a question: stop waiting for it, say nothing."""
        chat_id = message.context.get("chat_id")
        if chat_id is not None:
            self._on_loop(self._answered, chat_id, _session_of(message))

    def handle_denied(self, message: Message) -> None:
        """The hub refused a question (quota, rate limit, agent unreachable).

        The refusal names no chat. When exactly one question is waiting it
        can only be that one, and the person is told now; otherwise the
        reply timeout tells them.
        """
        if message.data.get("denied_type") != "recognizer_loop:utterance":
            return
        code = message.data.get("code")
        if code == ACL_DISALLOWED_TYPE and not self._told_about_acl:
            # Said once, and only now that the hub has refused one: a managed
            # hub grants the type itself, and a hint at every start misleads.
            self._told_about_acl = True
            LOG.warning("the hub does not allow this bridge to send "
                        "recognizer_loop:utterance (acl_disallowed_type). On a "
                        "self-managed hub an admin grants it with `hivemind-core "
                        "allow-msg recognizer_loop:utterance <client_id>`.")
        else:
            LOG.info(f"the hub refused a question: {code}")
        self._on_loop(self._denied)

    def _on_loop(self, callback: Callable, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:  # closed in between: shutting down
            pass

    def speak(self, text: str, chat_id: int, session_id: Optional[str] = None) -> None:
        """Post ``text`` back to ``chat_id``, without waiting for Telegram.

        Called from the HiveMind bus's own thread, never from the
        asyncio loop Telegram's Application runs on. Scheduling the send
        with ``run_coroutine_threadsafe`` is what makes that safe -- an
        ``await`` called directly from a foreign thread would raise or
        silently do nothing depending on timing. It returns at once: the
        bus thread also delivers every other hub message (and the client's
        keepalive), so it never waits for a send; a failure is logged when
        the send ends.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            LOG.warning("Telegram loop not running yet, dropping reply")
            return
        # The length, never the text: replies carry what people asked about,
        # and a debug log is kept, shipped and read far more widely than the chat.
        LOG.debug(f"Sending message to Telegram chat {chat_id}: {len(text)} characters")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._deliver(chat_id, session_id, text), loop)
        except RuntimeError:  # the loop closed in between: shutting down
            LOG.warning("Telegram loop stopped, dropping reply")
            return
        future.add_done_callback(partial(self._log_send_failure, chat_id))

    async def _deliver(self, chat_id: int, session_id: Optional[str], text: str) -> None:
        self._answered(chat_id, session_id)
        await self._send_in_order(chat_id, text)

    async def _send_in_order(self, chat_id: int, text: str) -> None:
        """Send ``text`` to ``chat_id`` after the replies scheduled before it.

        A reply longer than one Telegram message goes out as several, in
        order, every character kept (:func:`split_message`).
        """
        entry = self._chat_sends.get(chat_id)
        if entry is None:
            entry = self._chat_sends[chat_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        task = asyncio.current_task()
        if task is not None:
            self._sending.add(task)
        try:
            async with entry[0]:
                for piece in split_message(text, TELEGRAM_LIMIT):
                    await self._send_one(chat_id, piece)
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._chat_sends.pop(chat_id, None)
            if task is not None:
                self._sending.discard(task)
        # Telegram clears "typing…" when the bot's message arrives: renew it
        # for anyone in this chat still waiting for an answer.
        self._typing_at.pop(chat_id, None)
        for (waiting_chat, _), wait in list(self._waits.items()):
            if waiting_chat == chat_id:
                wait.wake.set()

    async def _send_one(self, chat_id: int, text: str) -> None:
        """sendMessage, waiting out Telegram's flood control when it asks to.

        Waits exactly what Telegram asks for, while the waits for this
        message stay within ``RETRY_AFTER_BUDGET``; beyond that the reply is
        given up at once (the error is logged by the caller).
        """
        waited = 0.0
        while True:
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=text)
                return
            except RetryAfter as error:
                with warnings.catch_warnings():  # int vs timedelta deprecation
                    warnings.simplefilter("ignore")
                    after = error.retry_after
                seconds = max(after.total_seconds() if isinstance(after, timedelta)
                              else float(after), 0.0)
                if waited + seconds > RETRY_AFTER_BUDGET:
                    LOG.warning(f"Telegram asks to wait {seconds:g} s before writing to "
                                f"chat {chat_id}; giving the reply up")
                    raise
                LOG.info(f"Telegram asks to wait {seconds:g} s before writing to chat {chat_id}")
                await asyncio.sleep(seconds)
                waited += seconds

    # ------------------------------------------------------------------
    # waiting for the hub: "typing…", then a line if no answer comes
    # (all on the Telegram loop)
    # ------------------------------------------------------------------
    def _expect_answer(self, chat_id: int, session_id: str,
                       user_id: Optional[int] = None,
                       language: Optional[str] = None) -> None:
        """Show "typing…" in ``chat_id`` until the hub answers ``session_id``.

        If no answer comes within ``reply_timeout``, the person is told the
        bridge cannot answer right now. A second question from the same
        person before the answer restarts the wait; it does not add one.
        """
        if not self.reply_timeout or self.reply_timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        key = (chat_id, session_id)
        wait = self._waits.get(key)
        if wait is None or wait.task is None or wait.task.done():
            wait = self._waits[key] = _Wait()
            wait.task = loop.create_task(self._wait_for_answer(key, wait))
        wait.deadline = loop.time() + self.reply_timeout
        # the line, if it comes, answers the latest question
        wait.user_id, wait.language = user_id, language
        wait.wake.set()

    async def _wait_for_answer(self, key: Tuple[int, str], wait: "_Wait") -> None:
        chat_id, session_id = key
        loop = asyncio.get_running_loop()
        try:
            while not wait.answered:
                remaining = wait.deadline - loop.time()
                if remaining <= 0:
                    break
                wait.wake.clear()
                self._keep_typing(chat_id)
                try:
                    await asyncio.wait_for(wait.wake.wait(), min(TYPING_EVERY, remaining))
                except asyncio.TimeoutError:
                    pass
            if not wait.answered:
                LOG.info(f"no answer from the hub for chat {chat_id} in "
                         f"{self.reply_timeout:g} s")
                self._forget(key, wait)
                await self._say_unavailable(chat_id, wait.user_id, wait.language)
        finally:
            self._forget(key, wait)

    def _forget(self, key: Tuple[int, str], wait: "_Wait") -> None:
        wait.answered = True
        if self._waits.get(key) is wait:
            del self._waits[key]
            now = time.monotonic()
            for old in [k for k, at in self._ended.items() if now - at >= ENDED_KEPT]:
                del self._ended[old]
            self._ended[key] = now
        if not any(chat == key[0] for chat, _ in self._waits):
            self._typing_at.pop(key[0], None)

    def _answered(self, chat_id: int, session_id: Optional[str]) -> None:
        """The hub answered (or is done with) ``session_id`` in ``chat_id``.

        A session the bridge is not waiting for (an answer without one, or
        a hub that rewrote it) ends every wait in the chat: a "cannot
        answer" line after an answer is worse than none. A late or second
        answer to a question whose wait already ended ends nothing else.
        """
        key = (chat_id, session_id)
        if key in self._waits:
            keys = [key]
        elif session_id is not None and key in self._ended:
            keys = []
        else:
            keys = [k for k in self._waits if k[0] == chat_id]
        for done in keys:
            wait = self._waits.get(done)
            if wait is not None:
                self._forget(done, wait)
                wait.wake.set()

    def _denied(self) -> None:
        if len(self._waits) == 1:
            (key, wait), = self._waits.items()
            self._give_up(key[0], key[1], wait.user_id, wait.language)

    def _give_up(self, chat_id: int, session_id: str, user_id: Optional[int],
                 language: Optional[str]) -> None:
        """The hub will not answer this question: stop waiting and say so now."""
        self._answered(chat_id, session_id)
        self._spawn(self._say_unavailable(chat_id, user_id, language))

    async def _say_unavailable(self, chat_id: int, user_id: Optional[int],
                               language: Optional[str]) -> None:
        """Say "cannot answer right now" to ``user_id`` in ``chat_id``, at most every 30 s."""
        loop = asyncio.get_running_loop()
        key, now = (chat_id, user_id), loop.time()
        for old in [k for k, at in self._unavailable_at.items() if now - at >= UNAVAILABLE_EVERY]:
            del self._unavailable_at[old]
        if key in self._unavailable_at:
            return
        self._unavailable_at[key] = now
        try:
            await self._send_in_order(chat_id, self.line(UNAVAILABLE, language))
        except Exception as error:
            self._log_failure(chat_id, error)

    def _keep_typing(self, chat_id: int) -> None:
        """Send "typing…" to ``chat_id`` unless one went there moments ago."""
        loop = asyncio.get_running_loop()
        last = self._typing_at.get(chat_id)
        if last is not None and loop.time() - last < TYPING_EVERY * 0.75:
            return
        self._typing_at[chat_id] = loop.time()
        self._spawn(self._send_typing(chat_id))

    async def _send_typing(self, chat_id: int) -> None:
        try:
            await self.app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as error:  # cosmetic: never let it fail anything
            LOG.debug(f"no typing indicator in chat {chat_id}: {type(error).__name__}")

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @classmethod
    def _log_send_failure(cls, chat_id: int, future: Future) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            cls._log_failure(chat_id, error)

    @staticmethod
    def _log_failure(chat_id: int, error: BaseException) -> None:
        # Telegram's own description ("Forbidden: bot was blocked by the
        # user") helps and holds no message text; anything else, the kind only.
        detail = f": {error}" if isinstance(error, TelegramError) else ""
        LOG.warning(f"failed to send a reply to Telegram chat {chat_id}: "
                    f"{type(error).__name__}{detail}")


class _Wait:
    """A question waiting for the hub's answer."""

    __slots__ = ("deadline", "answered", "wake", "task", "user_id", "language")

    def __init__(self):
        self.deadline = 0.0
        self.answered = False
        self.wake = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.user_id: Optional[int] = None  # who asked the latest question
        self.language: Optional[str] = None  # and the language their app reports


def _language_code(sender) -> Optional[str]:
    """The language the sender's Telegram app reports, when it does."""
    code = getattr(sender, "language_code", None)
    return code if isinstance(code, str) and code else None


def _session_of(message: Message) -> Optional[str]:
    """The session id a hub message answers, when it says."""
    session = message.context.get("session")
    return session.get("session_id") if isinstance(session, dict) else None


__all__ = ["HiveMindTelegramBridge", "TokenRejected", "report_token_rejected", "platform"]
