"""Fakes for tests that run the real python-telegram-bot stack.

``FakeTelegramAPI`` replaces the HTTP call under python-telegram-bot
(``HTTPXRequest.do_request``), so the bridge's own ``Application``, its
polling loop, offsets and error handling all run for real against canned
Bot API answers. ``FakeHub`` stands in for ``HiveMessageBusClient``: it
records what the bridge emits and delivers hub messages on its own bus
thread, one after the other, like the real client's websocket thread.
"""
import asyncio
import json
import queue
import threading
import time
from typing import Callable, Dict, List, Optional

import pytest
from telegram.request import HTTPXRequest

BOT_USER = {"id": 999, "is_bot": True, "first_name": "Hive", "username": "HiveBot"}


def text_update(update_id: int, text: str, chat_id: int = 42, user_id: int = 1,
                date: Optional[float] = None, chat_type: str = "private",
                language_code: Optional[str] = None) -> dict:
    """A Bot API ``Update`` carrying a text message."""
    sender = {"id": user_id, "is_bot": False, "first_name": f"user{user_id}",
              "username": f"user{user_id}"}
    if language_code:
        sender["language_code"] = language_code
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time() if date is None else date),
            "chat": {"id": chat_id, "type": chat_type},
            "from": sender,
            "text": text,
        },
    }


class FakeTelegramAPI:
    """Answers the Bot API calls python-telegram-bot makes.

    ``status[method]`` overrides the HTTP status of a method (401, 409...);
    ``latency[method]`` delays its answer. Every call is recorded in
    ``calls`` as ``(monotonic time, method, parameters)``.
    """

    def __init__(self):
        self.calls: List[tuple] = []
        self.done: List[tuple] = []  # like calls, once the answer is sent back
        self.status: Dict[str, int] = {}
        self.latency: Dict[str, float] = {}
        self.flood: Dict[str, int] = {}  # method -> how many calls get 429 first
        self.flood_wait = 1  # the retry_after those 429s ask for
        self.refuse_text: set = set()  # sendMessage texts answered 400
        self.served: Dict[int, float] = {}  # update_id -> when getUpdates returned it
        self._pending: List[dict] = []
        self._lock = threading.Lock()

    def push(self, *updates: dict) -> None:
        with self._lock:
            self._pending.extend(updates)

    def calls_to(self, method: str) -> List[tuple]:
        return [call for call in list(self.calls) if call[1] == method]

    def done_to(self, method: str) -> List[tuple]:
        """Calls to ``method`` that were answered successfully."""
        return [call for call in list(self.done) if call[1] == method]

    async def __call__(self, request, url: str, method: str, request_data=None, **_):
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((time.monotonic(), api_method, params))
        if self.latency.get(api_method):
            await asyncio.sleep(self.latency[api_method])
        if self.flood.get(api_method):
            self.flood[api_method] -= 1
            return 429, json.dumps({"ok": False, "error_code": 429,
                                    "description": f"Too Many Requests: retry after {self.flood_wait}",
                                    "parameters": {"retry_after": self.flood_wait}}).encode()
        if api_method == "sendMessage" and params.get("text") in self.refuse_text:
            return 400, json.dumps({"ok": False, "error_code": 400,
                                    "description": "Bad Request: chat not found"}).encode()
        if api_method == "sendMessage" and len(str(params.get("text", "")).encode("utf-16-le")) > 8192:
            return 400, json.dumps({"ok": False, "error_code": 400,
                                    "description": "Bad Request: message is too long"}).encode()
        status = self.status.get(api_method, 200)
        if status != 200:
            description = {401: "Unauthorized", 404: "Not Found",
                           409: "Conflict: terminated by other getUpdates request"}.get(status, "Error")
            if api_method == "getUpdates":
                await asyncio.sleep(0.02)
            return status, json.dumps({"ok": False, "error_code": status,
                                       "description": description}).encode()
        result = await self._result(api_method, params)
        self.done.append((time.monotonic(), api_method, params))
        return 200, json.dumps({"ok": True, "result": result}).encode()

    async def _result(self, api_method: str, params: dict):
        if api_method == "getMe":
            return BOT_USER
        if api_method == "getUpdates":
            offset = params.get("offset") or 0
            deadline = time.monotonic() + 0.2
            while True:
                with self._lock:
                    # like Telegram: an update below the offset is confirmed and gone
                    self._pending = [u for u in self._pending if u["update_id"] >= offset]
                    ready = list(self._pending)
                if ready or time.monotonic() >= deadline:
                    now = time.monotonic()
                    for update in ready:
                        self.served.setdefault(update["update_id"], now)
                    return ready
                await asyncio.sleep(0.002)
        if api_method == "sendMessage":
            return {"message_id": len(self.calls), "date": int(time.time()),
                    "chat": {"id": params.get("chat_id"), "type": "private"},
                    "text": params.get("text", "")}
        return True


@pytest.fixture
def telegram_api(monkeypatch):
    api = FakeTelegramAPI()

    async def do_request(self, url, method, request_data=None, **kwargs):
        return await api(self, url, method, request_data, **kwargs)

    monkeypatch.setattr(HTTPXRequest, "do_request", do_request)
    return api


class FakeHub:
    """A HiveMind hub, as far as the bridge can tell.

    ``emit`` records each message (``emitted``: ``(time, Message)``) and can
    take ``emit_delay`` seconds, like a slow socket. ``answer(message)``
    returns the reply text for an utterance, delivered as ``speak``
    ``think`` seconds after the emit, on the bus thread; ``replies`` records
    ``(time sent by the hub, chat_id)``. A bus handler that blocks holds up
    every later hub message, exactly as on the real client.
    """

    def __init__(self, answer: Optional[Callable] = None, think: float = 0.0,
                 emit_delay=0.0, refuse: Optional[Callable] = None):
        self.answer = answer
        self.think = think
        self.emit_delay = emit_delay  # seconds, or a function of the message
        self.refuse = refuse  # message -> True: answer with hive.policy.denied
        self.handlers: Dict[str, Callable] = {}
        self.emitted: List[tuple] = []
        self.replies: List[tuple] = []
        self.held: List[float] = []  # how long each bus handler kept the bus thread
        self.handshake_event = threading.Event()
        self._bus: "queue.PriorityQueue" = queue.PriorityQueue()
        self._order = 0
        self._thread = threading.Thread(target=self._run_bus, name="fake-hub-bus", daemon=True)
        self._thread.start()

    # the HiveMessageBusClient surface the bridge uses
    def connect(self, site_id=None, handshake_max_retries=None):
        self.handshake_event.set()

    def on_mycroft(self, msg_type, handler):
        self.handlers[msg_type] = handler

    def emit(self, hive_message):
        started = time.monotonic()
        message = hive_message.payload
        delay = self.emit_delay(message) if callable(self.emit_delay) else self.emit_delay
        if delay:
            time.sleep(delay)
        self.emitted.append((started, message))
        if self.refuse is not None and self.refuse(message):
            self.send("hive.policy.denied", {"denied_type": "recognizer_loop:utterance",
                                             "code": "intent_quota_exceeded"}, {})
            return
        if self.answer is not None:
            text = self.answer(message)
            if text is not None:
                self.send("speak", {"utterance": text}, dict(message.context),
                          at=time.monotonic() + self.think)

    def close(self):
        self._bus.put((float("inf"), 0, None))

    # the hub's side
    def send(self, msg_type: str, data: dict, context: dict, at: Optional[float] = None):
        """Deliver a hub message to the bridge at ``at`` (default: now)."""
        self._order += 1
        self._bus.put((time.monotonic() if at is None else at, self._order,
                       (msg_type, data, context)))

    def _run_bus(self):
        from ovos_bus_client.message import Message

        while True:
            due, _, item = self._bus.get()
            if item is None:
                return
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            msg_type, data, context = item
            if msg_type == "speak":
                self.replies.append((due, context.get("chat_id")))
            handler = self.handlers.get(msg_type)
            if handler is not None:
                started = time.monotonic()
                handler(Message(msg_type, data, context))
                self.held.append(time.monotonic() - started)


@pytest.fixture
def hub():
    made = FakeHub()
    yield made
    made.close()


def run_in_thread(bridge) -> dict:
    """Run ``bridge.start()`` in a thread; returns a dict that gets ``error`` set."""
    outcome: dict = {}

    def run():
        try:
            bridge.start()
        except BaseException as error:  # the test looks at it
            outcome["error"] = error
        finally:
            outcome["done"] = True

    thread = threading.Thread(target=run, name="bridge", daemon=True)
    outcome["thread"] = thread
    thread.start()
    return outcome


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()
