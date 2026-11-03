"""Nothing anyone wrote or was told ends up in the bridge's log, at any level.

A debug log is shipped and read far more widely than the chat it came from.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import hivemind_telegram_bridge as bridge_module

SECRET_QUESTION = "is my biopsy result back"
SECRET_ANSWER = "Your appointment is on Tuesday"


def test_neither_questions_nor_answers_are_logged(monkeypatch):
    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = lambda *a, **k: lines.append(" ".join(map(str, a)))
    monkeypatch.setattr(bridge_module, "LOG", log)

    client = MagicMock()
    app = MagicMock()
    app.bot.id = 999
    app.bot.username = "HiveBot"
    app.bot.send_message = AsyncMock()
    made = bridge_module.HiveMindTelegramBridge(client=client, app=app)
    made.connect_hivemind()

    upd = MagicMock()
    upd.effective_message.text = SECRET_QUESTION
    upd.effective_user.is_bot = False
    upd.effective_chat.id = 42
    upd.effective_chat.type = "private"
    asyncio.run(made.handle_telegram_message(upd, MagicMock()))

    loop = asyncio.new_event_loop()
    try:
        import threading

        runner = threading.Thread(target=loop.run_forever, daemon=True)
        runner.start()
        made._loop = loop
        made.speak(SECRET_ANSWER, 42)
        import time

        deadline = time.monotonic() + 2
        while not app.bot.send_message.await_count and time.monotonic() < deadline:
            time.sleep(0.01)  # the send runs on the loop; let it finish
    finally:
        loop.call_soon_threadsafe(loop.stop)

    assert lines, "the bridge logged nothing; the test would prove nothing"
    joined = "\n".join(lines)
    assert SECRET_QUESTION not in joined
    assert SECRET_ANSWER not in joined
