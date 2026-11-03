"""Split a reply that is too long for one chat message, without changing it.

Telegram refuses a message over 4096 characters, so a long answer (an LLM's,
typically) used to be lost whole. Splitting keeps the reply's text in order:
the pieces, joined, are the reply. A message must hold more than
whitespace (Telegram refuses an empty one), so a whitespace run that would
make a piece of its own is joined to the text next to it; only a run longer
than a whole message, which no message can hold, is shortened. Cuts fall on a paragraph, then a line, then a
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


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """``text`` as pieces of at most ``limit`` UTF-16 code units each.

    Every piece holds more than whitespace (Telegram refuses an empty
    message). Every character is kept, in order, except in a whitespace run
    longer than a whole message: the part no message can hold is left out.
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
    return _place_whitespace(pieces, limit)


def _place_whitespace(pieces: List[str], limit: int) -> List[str]:
    """Give each whitespace-only piece to the text next to it, when it fits.

    Joined to a neighbour whole if the two fit in one message. Otherwise
    the visible character nearest to it in the piece before comes over, with
    the whitespace after it (a run shorter than a message always fits beside
    one character); if that leaves the piece before with only whitespace,
    that piece is placed in turn. A piece that still cannot be placed holds
    whitespace no message can carry beside visible text, and is dropped.
    """
    pieces = [piece for piece in pieces if piece]
    i = 0
    while i < len(pieces):
        piece = pieces[i]
        if piece.strip():
            i += 1
            continue
        if i > 0 and _units(pieces[i - 1] + piece) <= limit:
            pieces[i - 1] += piece
            del pieces[i]
            continue
        if i + 1 < len(pieces) and _units(piece + pieces[i + 1]) <= limit:
            pieces[i + 1] = piece + pieces[i + 1]
            del pieces[i]
            continue
        if i > 0 and pieces[i - 1].strip():
            before = pieces[i - 1]
            last = len(before.rstrip()) - 1  # its last visible character
            if _units(before[last:] + piece) <= limit:
                pieces[i] = before[last:] + piece
                if last == 0:
                    del pieces[i - 1]
                    continue
                pieces[i - 1] = before[:last]
                if not pieces[i - 1].strip():
                    i -= 1  # left with whitespace only: place it in turn
                else:
                    i += 1
                continue
        if i + 1 < len(pieces):
            after = pieces[i + 1]
            first = len(after) - len(after.lstrip())  # its first visible character
            if after[first + 1:].strip() and _units(piece + after[:first + 1]) <= limit:
                pieces[i], pieces[i + 1] = piece + after[:first + 1], after[first + 1:]
                i += 1
                continue
        del pieces[i]  # whitespace no message can carry beside visible text
    return pieces
