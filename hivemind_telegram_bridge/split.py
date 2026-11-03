"""Split a reply that is too long for one chat message, without changing it.

Telegram refuses a message over 4096 characters, so a long answer (an LLM's,
typically) used to be lost whole. Splitting keeps the reply's text in order:
the pieces, joined, are the reply, except that a piece holding nothing but
whitespace is not sent (Telegram refuses an empty message, and trims the
edges of every message anyway), so a run of whitespace longer than a whole
message is shortened. Cuts fall on a paragraph, then a line, then a
sentence, then a word boundary in the second half of a piece, and only
mid-word when a piece has no space at all. Lengths are counted in UTF-16
code units, as Telegram counts them, so an emoji outside the Basic
Multilingual Plane counts as two. The text is walked by offset, never
copied piece by piece, so splitting stays linear in its length.
"""
from typing import List

#: Telegram's limit on the text of one message.
TELEGRAM_LIMIT = 4096

_BOUNDARIES = ("\n\n", "\n", ". ", " ")


def _fits(text: str, start: int, limit: int) -> int:
    """The end of the longest run of ``text`` from ``start`` within ``limit`` UTF-16 units."""
    window = text[start:start + limit]
    if len(window.encode("utf-16-le")) // 2 == len(window):  # no astral character
        return start + len(window)
    units = 0
    for index, char in enumerate(window):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return start + index
    return start + len(window)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """``text`` as pieces of at most ``limit`` UTF-16 code units each.

    A piece that would hold only whitespace is left out: Telegram refuses
    an empty message, so a whitespace run longer than a piece cannot be
    sent as it is. Every other character is kept, in order.
    """
    if limit < 2:
        raise ValueError("limit must be at least 2")
    pieces = []
    start = 0
    while start < len(text):
        end = _fits(text, start, limit)
        if end < len(text):
            for boundary in _BOUNDARIES:
                at = text.rfind(boundary, start, end)
                # never end a piece that would hold only whitespace: it could
                # not be sent, and a shorter run would be lost for nothing
                if at >= start + (end - start) // 2 and text[start:at + len(boundary)].strip():
                    end = at + len(boundary)
                    break
        pieces.append(text[start:end])
        start = end
    return [piece for piece in pieces if piece.strip()]
