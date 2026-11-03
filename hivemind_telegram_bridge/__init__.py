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
import itertools
import logging
import math
import re
import threading
import time
import warnings
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from http import HTTPStatus
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

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
#: ... and at most this many times, however short the waits.
RETRY_AFTER_MAX_RETRIES = 10
#: Replies waiting to be sent to one chat at most (Telegram slow or holding
#: the bot back); beyond that a reply is dropped and logged, not queued.
SENDS_PER_CHAT_MAX = 32
#: The bridge's own lines may go past that cap by this many, so a reply the
#: cap drops is still followed by "cannot answer right now".
LINES_ROOM = 8
#: Context key carrying the bridge's number for a question. The hub echoes
#: the question's context on its replies, so a reply carrying it ends
#: exactly that question's wait.
QUESTION_KEY = "bridge_question"
#: "cannot answer right now" lines a full chat queue could not take are owed
#: and sent as the queue drains; at most this many per chat.
OWED_MAX = 64
#: When the bridge stops, replies already being sent get this long to finish.
SEND_DRAIN = 5.0
#: How long a reply is remembered, so the hub's "handled" that follows it
#: is recognised and ends no further question.
REPLIED_KEPT = 600.0
#: A refusal from the hub (``hive.policy.denied``) comes at once, when the
#: hub admits or refuses the question; it names none. It is pinned on a
#: question only when that is the one unanswered question being sent to the
#: hub, or sent this recently (counted from the end of the send, not from
#: when it was queued).
REFUSAL_WINDOW = 5.0
#: Questions waiting for the hub-send thread at most. Beyond that the hub
#: is not keeping up: the person is told at once instead of queueing more.
HUB_BACKLOG_MAX = 64

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
        # Questions waiting for the hub: one per question, oldest first, by
        # (chat id, session id); when each session last asked something; how
        # many replies per session are not yet followed by the hub's
        # "handled"; when the last "typing…" went to each chat. Only touched
        # on the Telegram loop.
        self._pending: Dict[Tuple[int, str], Deque["_Question"]] = {}
        self._replied: Dict[Tuple[int, Optional[str], Optional[int]], List] = {}
        self._recent: Deque["_Question"] = deque()  # being sent, or sent within REFUSAL_WINDOW
        self._owed: Dict[int, List[Optional[str]]] = {}  # chat -> languages of owed lines
        self._hub_echoes_numbers = False  # seen a reply carrying QUESTION_KEY
        self._typing_at: Dict[int, float] = {}
        self._numbers = itertools.count()
        self._hub_backlog = 0  # questions submitted to the hub-send thread, not yet sent
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
            self._close_hub()
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
        self._close_hub()
        self._started = False
        self._connected.clear()
        if self._health is not None:
            self._health.stop()
            self._health = None

    def _close_hub(self) -> None:
        """Close the hub connection; questions not yet sent are not sent."""
        self._hub_sender.shutdown(wait=False, cancel_futures=True)
        try:
            self.client.close()
        except Exception:
            LOG.exception("error closing HiveMind client")

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

        session_id = self.session_id(chat.id, sender)
        user_id = getattr(sender, "id", None)
        language = _language_code(sender)
        if self._hub_backlog >= HUB_BACKLOG_MAX:
            LOG.warning(f"{self._hub_backlog} questions are waiting to be sent to "
                        f"the hub; not queueing one more from chat {chat.id}")
            self._spawn(self._say_unavailable(chat.id, language))
            return
        # Sent on the hub-send thread, never on this loop, so the loop keeps
        # sending replies while the hub socket is slow; "typing…" starts
        # while it goes.
        loop = asyncio.get_running_loop()
        number = next(self._numbers)
        question = self._expect_answer(chat.id, session_id, user_id, language, number)
        written = getattr(message, "date", None)
        try:
            sent = loop.run_in_executor(
                self._hub_sender, self._forward_unless_late, time.monotonic(),
                written.timestamp() if isinstance(written, datetime) else None,
                text, sender, chat.id, number, question, loop)
        except RuntimeError:  # the bridge is stopping: the hub-send thread is shut
            if question is not None:
                self._end(question)
            return
        self._hub_backlog += 1
        try:
            if not await sent and question is not None:
                # Not sent (too late): the hub will never answer it; say so now.
                self._give_up(question, chat.id, language)
        except Exception as error:
            # emit() puts the whole message, text included, in some of its
            # errors: name the kind of failure only.
            LOG.warning(f"could not send a message from chat {chat.id} to the "
                        f"hub: {type(error).__name__}")
            self._give_up(question, chat.id, language)
        finally:
            self._hub_backlog -= 1

    def _forward_unless_late(self, read_at: float, written_at: Optional[float],
                             text: str, sender, chat_id: int, number: Optional[int] = None,
                             question: Optional["_Question"] = None,
                             loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
        """``forward_to_hivemind``, unless the question waited too long to be sent.

        When the hub socket is slow, questions queue for the hub-send thread.
        One that waited past the reply timeout has already been told the
        bridge cannot answer, and one now older than ``max_message_age``
        would not have been answered had it been read now: sending either
        would bring an answer that means nothing any more, so it is dropped.
        """
        limit = self.reply_timeout if self.reply_timeout > 0 else self.max_message_age
        waited = time.monotonic() - read_at
        age = time.time() - written_at if written_at is not None else 0.0
        if waited >= limit or age > self.max_message_age:
            LOG.info(f"not sending a question from chat {chat_id} that waited "
                     f"{waited:.0f} s for the hub ({age:.0f} s after it was written)")
            return False
        if question is not None and loop is not None:
            # queued on the loop before the emit, so ahead of any refusal
            loop.call_soon_threadsafe(self._sent_to_hub, question, time.monotonic())
        try:
            self.forward_to_hivemind(text, sender, chat_id, number)
        finally:
            if question is not None and loop is not None:
                try:
                    loop.call_soon_threadsafe(self._emit_done, question, time.monotonic())
                except RuntimeError:  # the loop ended: shutting down
                    pass
        return True

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
        # INFO, not DEBUG: if every new message is dropped, the host clock is
        # probably wrong, and this line is how an operator finds out.
        LOG.info(f"not answering a message sent {age:.0f} s ago (older than "
                 f"{self.max_message_age:g} s; the host clock must be right)")
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

    def forward_to_hivemind(self, text: str, sender, chat_id: int,
                            question_number: Optional[int] = None) -> None:
        """Emit ``text`` to the hub as an utterance from ``sender`` in ``chat_id``.

        Blocking (it writes to the hub socket): ``handle_telegram_message``
        runs it on the hub-send thread. ``question_number`` goes in the
        context (``QUESTION_KEY``), which the hub echoes on its replies.
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
        if question_number is not None:
            msg.context[QUESTION_KEY] = question_number
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
        if not isinstance(utterance, str) or not utterance.strip():
            # Nothing Telegram could show: not an answer. Remembered, so the
            # "handled" that follows does not end the question in silence.
            self._on_loop(self._blank_reply, chat_id, _session_of(message),
                          _asker_of(message), _question_of(message))
            return
        self.speak(utterance, chat_id, _session_of(message), _asker_of(message),
                   _question_of(message))

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
        user_id = _asker_of(message)
        language = self._language_of(chat_id, user_id)
        self.speak(self.line(NO_ANSWER, language), chat_id, _session_of(message), user_id,
                   _question_of(message), room=LINES_ROOM)

    def handle_handled(self, message: Message) -> None:
        """The hub is done with a question: stop waiting for it, say nothing."""
        chat_id = message.context.get("chat_id")
        if chat_id is not None:
            self._on_loop(self._handled, chat_id, _session_of(message), _asker_of(message),
                          _question_of(message))

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

    def speak(self, text: str, chat_id: int, session_id: Optional[str] = None,
              user_id: Optional[int] = None, question_id: Optional[int] = None,
              room: int = 0) -> None:
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
        # The bookkeeping goes first, as a plain callback: callbacks run in the
        # order the hub's messages came, and an "ovos.utterance.handled"
        # right behind this reply must find it already counted (a task's
        # first step would only run after that).
        ticket: dict = {}  # filled by _reply_came with the question this reply ends
        self._on_loop(self._reply_came, chat_id, session_id, user_id, ticket, question_id)
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_reply(chat_id, text, ticket, room), loop)
        except RuntimeError:  # the loop closed in between: shutting down
            LOG.warning("Telegram loop stopped, dropping reply")
            return
        future.add_done_callback(partial(self._log_send_failure, chat_id))

    async def _send_reply(self, chat_id: int, text: str, ticket: dict, room: int = 0) -> None:
        """Send the hub's reply; if it cannot go, say the bridge cannot answer.

        The question the reply answers stopped waiting when the reply came
        (so "typing…" stops at once). Should Telegram refuse the reply, that
        person would otherwise get nothing at all.
        """
        try:
            sent = await self._send_in_order(chat_id, text, room)
        except Exception:
            question = ticket.get("question")
            if question is not None:
                await self._say_unavailable(chat_id, question.language)
            raise
        if not sent and ticket.get("question") is not None:
            # dropped by the per-chat cap: the line has room past the cap
            await self._say_unavailable(chat_id, ticket["question"].language)

    async def _send_in_order(self, chat_id: int, text: str, room: int = 0) -> bool:
        """Send ``text`` to ``chat_id`` after the replies scheduled before it.

        A reply longer than one Telegram message goes out as several, in
        order (:func:`split_message`). Returns ``False`` when the chat already
        has ``SENDS_PER_CHAT_MAX`` (+ ``room``) sends waiting and this one is
        dropped.
        """
        entry = self._chat_sends.get(chat_id)
        if entry is None:
            entry = self._chat_sends[chat_id] = [asyncio.Lock(), 0]
        if entry[1] >= SENDS_PER_CHAT_MAX + room:
            LOG.warning(f"{entry[1]} replies are waiting to be sent to chat {chat_id}; "
                        f"dropping one more")
            return False
        entry[1] += 1
        task = asyncio.current_task()
        if task is not None:
            self._sending.add(task)
        try:
            async with entry[0]:
                budget = [0.0, 0]  # flood waits and retries, for the whole reply
                for piece in split_message(text, TELEGRAM_LIMIT):
                    await self._send_one(chat_id, piece, budget)
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._chat_sends.pop(chat_id, None)
            if task is not None:
                self._sending.discard(task)
            self._flush_owed(chat_id)
        # Telegram clears "typing…" when the bot's message arrives: renew it
        # for anyone in this chat still waiting for an answer.
        self._typing_at.pop(chat_id, None)
        for question in self._waiting_in(chat_id):
            question.wake.set()
        return True

    async def _send_one(self, chat_id: int, text: str, budget: Optional[List] = None) -> None:
        """sendMessage, waiting out Telegram's flood control when it asks to.

        Waits exactly what Telegram asks for, while the waits for the reply
        stay within ``RETRY_AFTER_BUDGET`` and ``RETRY_AFTER_MAX_RETRIES``;
        beyond that the reply is given up at once (the error is logged by the
        caller). ``budget`` (seconds waited, retries) is shared by all the
        pieces of one split reply.
        """
        if budget is None:
            budget = [0.0, 0]
        while True:
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=text)
                return
            except RetryAfter as error:
                budget[1] += 1
                with warnings.catch_warnings():  # int vs timedelta deprecation
                    warnings.simplefilter("ignore")
                    after = error.retry_after
                seconds = max(after.total_seconds() if isinstance(after, timedelta)
                              else float(after), 0.0)
                if budget[1] > RETRY_AFTER_MAX_RETRIES or budget[0] + seconds > RETRY_AFTER_BUDGET:
                    LOG.warning(f"Telegram asks to wait {seconds:g} s before writing to "
                                f"chat {chat_id}; giving the reply up")
                    raise
                LOG.info(f"Telegram asks to wait {seconds:g} s before writing to chat {chat_id}")
                await asyncio.sleep(seconds)
                budget[0] += seconds

    # ------------------------------------------------------------------
    # waiting for the hub: "typing…", then a line if no answer comes
    # (all on the Telegram loop)
    # ------------------------------------------------------------------
    def _expect_answer(self, chat_id: int, session_id: str,
                       user_id: Optional[int] = None,
                       language: Optional[str] = None,
                       number: Optional[int] = None) -> Optional["_Question"]:
        """Wait for the hub's answer to one question, showing "typing…".

        Every question has its own wait, queued behind the earlier ones of
        its session. "typing…" shows in ``chat_id`` while any question there
        waits. A question with no answer within ``reply_timeout`` gets its
        own "cannot answer right now" line, in the language of the person
        who asked it. Returns the question, or ``None`` with waiting off.
        """
        if not self.reply_timeout or self.reply_timeout <= 0:
            return None
        loop = asyncio.get_running_loop()
        key = (chat_id, session_id)
        question = _Question(chat_id, session_id, user_id, language,
                             loop.time() + self.reply_timeout,
                             next(self._numbers) if number is None else number)
        self._pending.setdefault(key, deque()).append(question)
        question.task = loop.create_task(self._wait_for_answer(question))
        return question

    async def _wait_for_answer(self, question: "_Question") -> None:
        loop = asyncio.get_running_loop()
        try:
            while not question.done:
                remaining = question.deadline - loop.time()
                if remaining <= 0:
                    break
                question.wake.clear()
                self._keep_typing(question.chat_id)
                try:
                    await asyncio.wait_for(question.wake.wait(), min(TYPING_EVERY, remaining))
                except asyncio.TimeoutError:
                    pass
            if not question.done:
                LOG.info(f"no answer from the hub for a question in chat "
                         f"{question.chat_id} in {self.reply_timeout:g} s")
                self._end(question)
                await self._say_unavailable(question.chat_id, question.language)
        finally:
            self._end(question)

    def _end(self, question: "_Question") -> None:
        """Stop waiting for ``question`` (answered, given up or timed out)."""
        if question.done:
            return
        question.done = True
        question.wake.set()
        key = (question.chat_id, question.session_id)
        queue = self._pending.get(key)
        if queue is not None:
            try:
                queue.remove(question)
            except ValueError:
                pass
            if not queue:
                del self._pending[key]
        if not self._waiting_in(question.chat_id):
            self._typing_at.pop(question.chat_id, None)

    def _waiting_in(self, chat_id: int) -> List["_Question"]:
        """The questions waiting in ``chat_id``, oldest first."""
        waiting = [q for (chat, _), queue in self._pending.items() if chat == chat_id
                   for q in queue]
        return sorted(waiting, key=lambda q: q.number)

    def _question_for(self, chat_id: int, session_id: Optional[str],
                      user_id: Optional[int],
                      question_id: Optional[int] = None) -> Optional["_Question"]:
        """The waiting question a hub message is about, if any.

        The hub echoes the question's context. When it carries the bridge's
        question number (``QUESTION_KEY``), that question, if it still
        waits; a late answer or a further sentence then ends nothing else.
        Once the hub has been seen echoing numbers, a message without one is
        about no question (a notification, say), and ends no one's wait.
        From a hub that never echoes them, matched by session and person,
        independently: the oldest question waiting from that person (when
        named) in that session (when named), else in the chat. A message for
        a person or a session with nothing waiting is about none.
        """
        if question_id is not None:
            self._hub_echoes_numbers = True
            return next((q for q in self._waiting_in(chat_id) if q.number == question_id), None)
        if self._hub_echoes_numbers:
            return None
        if session_id is not None:
            waiting = list(self._pending.get((chat_id, session_id), ()))
        else:
            waiting = self._waiting_in(chat_id)
        if user_id is not None:
            waiting = [q for q in waiting if q.user_id == user_id]
        return waiting[0] if waiting else None

    def _reply_came(self, chat_id: int, session_id: Optional[str],
                    user_id: Optional[int] = None, ticket: Optional[dict] = None,
                    question_id: Optional[int] = None) -> None:
        """A reply (``speak``, or the no-answer line): ends one question's wait."""
        question = self._question_for(chat_id, session_id, user_id, question_id)
        if question is None:
            return
        question.answered = True
        self._end(question)
        if ticket is not None:
            ticket["question"] = question
        if question_id is not None:
            return  # its "handled" names it too: nothing to pair
        now = time.monotonic()
        for old in [k for k, (_, at) in self._replied.items() if now - at >= REPLIED_KEPT]:
            del self._replied[old]
        key = (chat_id, session_id, user_id)
        count = self._replied.get(key, [0, now])[0]
        self._replied[key] = [count + 1, now]

    def _handled(self, chat_id: int, session_id: Optional[str],
                 user_id: Optional[int] = None, question_id: Optional[int] = None) -> None:
        """The hub's ``ovos.utterance.handled``.

        After a reply it only closes that reply's question (already ended).
        Without one, a question was dealt with silently (a cancelled
        utterance): its wait ends, quietly.
        """
        if question_id is None:
            key = (chat_id, session_id, user_id)
            replied = self._replied.get(key)
            if replied and replied[0] > 0:
                replied[0] -= 1
                if not replied[0]:
                    del self._replied[key]
                return
        question = self._question_for(chat_id, session_id, user_id, question_id)
        if question is None:
            return
        if question.blank:
            # the hub "answered" with nothing to show: say so, as at a timeout
            self._give_up(question, chat_id, question.language)
            return
        question.answered = True  # dealt with by the hub (a cancelled utterance)
        self._end(question)

    def _blank_reply(self, chat_id: int, session_id: Optional[str],
                     user_id: Optional[int] = None, question_id: Optional[int] = None) -> None:
        """A ``speak`` with nothing to show: the question keeps waiting, marked."""
        question = self._question_for(chat_id, session_id, user_id, question_id)
        if question is not None:
            question.blank = True

    def _sent_to_hub(self, question: "_Question", at: float) -> None:
        """``question`` is being sent to the hub (``at``, monotonic): refusable now."""
        question.sending = True
        question.sent_at = at
        self._prune_recent(at)
        self._recent.append(question)

    def _emit_done(self, question: "_Question", at: float) -> None:
        """The send of ``question`` ended: the window runs from here."""
        question.sending = False
        question.sent_at = at

    def _prune_recent(self, now: float) -> None:
        while (self._recent and not self._recent[0].sending
               and now - self._recent[0].sent_at > REFUSAL_WINDOW):
            self._recent.popleft()

    def _denied(self) -> None:
        """A refusal names no question: pin it only when it can be one.

        The hub refuses at admission, so its refusal comes while the
        question is being sent or right after. The candidates are the
        questions being sent or sent in the last ``REFUSAL_WINDOW`` seconds,
        except those the hub answered (an answered question was admitted).
        Timed-out and given-up ones stay candidates: a refusal may be theirs.
        When exactly one candidate is left and it still waits, it is that
        one; otherwise the reply timeout tells the person.
        """
        self._prune_recent(time.monotonic())
        candidates = [q for q in self._recent if not q.answered]
        if len(candidates) == 1 and not candidates[0].done:
            question = candidates[0]
            self._give_up(question, question.chat_id, question.language)

    def _give_up(self, question: Optional["_Question"], chat_id: int,
                 language: Optional[str]) -> None:
        """The hub will not answer this question: stop waiting and say so now."""
        if question is not None:
            if question.done:
                return
            self._end(question)
        self._spawn(self._say_unavailable(chat_id, language))

    async def _say_unavailable(self, chat_id: int, language: Optional[str]) -> None:
        """Say "cannot answer right now" in ``chat_id``: once per unanswered question.

        If the chat's queue is full even past ``LINES_ROOM``, the line is
        owed and sent as the queue drains (``_flush_owed``), never dropped
        while fewer than ``OWED_MAX`` are owed.
        """
        try:
            if not await self._send_in_order(chat_id, self.line(UNAVAILABLE, language), LINES_ROOM):
                owed = self._owed.setdefault(chat_id, [])
                if len(owed) < OWED_MAX:
                    owed.append(language)
                else:
                    LOG.warning(f"{len(owed)} lines are owed to chat {chat_id}; dropping one more")
        except Exception as error:
            self._log_failure(chat_id, error)

    def _flush_owed(self, chat_id: int) -> None:
        """A send to ``chat_id`` ended: send one owed line, if there is room now."""
        owed = self._owed.get(chat_id)
        if not owed:
            return
        entry = self._chat_sends.get(chat_id)
        if entry is not None and entry[1] >= SENDS_PER_CHAT_MAX + LINES_ROOM:
            return
        language = owed.pop(0)
        if not owed:
            del self._owed[chat_id]
        self._spawn(self._say_unavailable(chat_id, language))

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


class _Question:
    """One question waiting for the hub's answer."""

    __slots__ = ("chat_id", "session_id", "user_id", "language", "deadline",
                 "number", "done", "wake", "task", "sent_at", "sending", "answered",
                 "blank")

    def __init__(self, chat_id: int, session_id: str, user_id: Optional[int],
                 language: Optional[str], deadline: float, number: int):
        self.chat_id = chat_id
        self.session_id = session_id
        self.user_id = user_id  # who asked it
        self.language = language  # the language their app reports
        self.deadline = deadline
        self.number = number  # order of asking, across the bridge
        self.sent_at = 0.0  # time.monotonic() when its send to the hub started, then ended
        self.sending = False  # its emit is running
        self.answered = False  # ended by the hub's answer (so it was admitted)
        self.blank = False  # the hub's only answer so far had nothing to show
        self.done = False
        self.wake = asyncio.Event()
        self.task: Optional[asyncio.Task] = None


def _language_code(sender) -> Optional[str]:
    """The language the sender's Telegram app reports, when it does."""
    code = getattr(sender, "language_code", None)
    return code if isinstance(code, str) and code else None


def _asker_of(message: Message) -> Optional[int]:
    """The Telegram user id of the person a hub message answers, when it says."""
    user = message.context.get("user")
    user_id = user.get("telegram_user_id") if isinstance(user, dict) else None
    return user_id if isinstance(user_id, int) else None


def _question_of(message: Message) -> Optional[int]:
    """The bridge's question number a hub message echoes, when it does."""
    number = message.context.get(QUESTION_KEY)
    return number if isinstance(number, int) and not isinstance(number, bool) else None


def _session_of(message: Message) -> Optional[str]:
    """The session id a hub message answers, when it says."""
    session = message.context.get("session")
    return session.get("session_id") if isinstance(session, dict) else None


__all__ = ["HiveMindTelegramBridge", "TokenRejected", "report_token_rejected", "platform"]
