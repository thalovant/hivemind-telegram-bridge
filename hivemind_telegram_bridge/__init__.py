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
- a freshly registered HiveMind client is denied every message type
  until a hub admin runs ``hivemind-core allow-msg
  recognizer_loop:utterance <client_id>`` (and usually ``speak`` too);
  this bridge cannot do that step itself. See the README.
"""
import asyncio
import re
import threading
from typing import Iterable, Optional

from hivemind_bus_client import (
    HiveMessage,
    HiveMessageType,
    HiveMessageBusClient,
)
from ovos_bus_client.message import Message
from ovos_utils.log import LOG
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

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

        self.app: Application = app or ApplicationBuilder().token(token).build()
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_telegram_message)
        )

        self.client = client or HiveMessageBusClient(
            key=key,
            password=password,
            host=host,
            port=port,
            useragent=platform,
            self_signed=self_signed,
        )

        self._started = False
        self._connected = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None

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
        self.client.on_mycroft("hive.complete_intent_failure",
                               self.handle_intent_failure)
        self._connected.set()
        LOG.info("== connected to HiveMind")

    def start(self) -> None:
        """Connect to HiveMind, then run the Telegram bot until stopped.

        Blocks the calling thread for as long as the bot is polling
        Telegram. Intended to be the last call in a ``__main__``.
        """
        if self._started:
            return
        self._started = True
        self.connect_hivemind()
        LOG.warning(
            "a freshly registered HiveMind client is denied every message "
            "type until an admin runs `hivemind-core allow-msg "
            "recognizer_loop:utterance <client_id>` on the hub (and "
            "usually `speak` too). If messages seem to vanish silently, "
            "check that first."
        )
        asyncio.run(self._run_telegram())

    async def _run_telegram(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        LOG.info("== listening to Telegram")
        try:
            await self._stop_event.wait()
        finally:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    def stop(self) -> None:
        """Stop polling Telegram and close the HiveMind connection."""
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        try:
            self.client.close()
        except Exception:
            LOG.exception("error closing HiveMind client")
        self._started = False
        self._connected.clear()

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

        self.forward_to_hivemind(text, sender, chat.id)

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
        username = (sender.username if sender and sender.username
                   else str(sender.id) if sender else "unknown")
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
                "session": {"session_id": self.session_id(chat_id, sender)},
            },
        )
        self.client.emit(HiveMessage(HiveMessageType.BUS, msg))

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
        self.speak(utterance, chat_id)

    def handle_intent_failure(self, message: Message) -> None:
        chat_id = message.context.get("chat_id")
        if chat_id is None:
            return
        LOG.error("complete intent failure")
        self.speak("I don't know how to answer that", chat_id)

    def speak(self, text: str, chat_id: int) -> None:
        """Post ``text`` back to ``chat_id``.

        Called from the HiveMind bus's own thread, never from the
        asyncio loop Telegram's Application runs on. Scheduling the send
        with ``run_coroutine_threadsafe`` is what makes that safe -- an
        ``await`` called directly from a foreign thread would raise or
        silently do nothing depending on timing.
        """
        if self._loop is None:
            LOG.warning("Telegram loop not running yet, dropping reply")
            return
        LOG.debug(f"Sending message to Telegram chat {chat_id}: {text}")
        future = asyncio.run_coroutine_threadsafe(
            self.app.bot.send_message(chat_id=chat_id, text=text), self._loop
        )
        try:
            future.result(timeout=10)
        except Exception:
            LOG.exception(f"failed to send Telegram message to chat={chat_id}")


__all__ = ["HiveMindTelegramBridge", "platform"]
