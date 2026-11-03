"""Split a reply that is too long for one chat message, without changing it.

Telegram refuses a message over 4096 characters, so a long answer (an LLM's,
typically) used to be lost whole. Splitting keeps every character in order:
the pieces, joined, are the reply. Cuts fall on a paragraph, then a line,
then a sentence, then a word boundary in the second half of a piece, and
only mid-word when a piece has no space at all. Lengths are counted in
UTF-16 code units, as Telegram counts them, so an emoji outside the Basic
Multilingual Plane counts as two.
"""
from typing import List

#: Telegram's limit on the text of one message.
TELEGRAM_LIMIT = 4096

_BOUNDARIES = ("\n\n", "\n", ". ", " ")


def _fits(text: str, limit: int) -> int:
    """How many characters of ``text`` fit in ``limit`` UTF-16 code units."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return index
    return len(text)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """``text`` as pieces of at most ``limit`` UTF-16 code units each.

    A piece that would hold only whitespace is left out: Telegram refuses
    an empty message, and whitespace between pieces carries nothing.
    """
    if limit < 2:
        raise ValueError("limit must be at least 2")
    pieces = []
    rest = text
    while True:
        fit = _fits(rest, limit)
        if fit == len(rest):
            pieces.append(rest)
            break
        window = rest[:fit]
        cut = fit
        for boundary in _BOUNDARIES:
            at = window.rfind(boundary)
            if at >= fit // 2:
                cut = at + len(boundary)
                break
        pieces.append(rest[:cut])
        rest = rest[cut:]
    return [piece for piece in pieces if piece.strip()]
