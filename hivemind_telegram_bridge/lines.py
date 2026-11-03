"""The few lines the bridge says itself, in the person's language.

The hub writes every answer. The bridge speaks only when the hub cannot:
no skill matched the question (``NO_ANSWER``), or no answer came at all
(``UNAVAILABLE``: the hub did not answer in time, could not be reached, or
refused the request). The lines are short on purpose: they stand in for an
answer, they are not one.
"""
from typing import Optional

#: The hub understood nothing it could answer (``complete_intent_failure``).
NO_ANSWER = "no_answer"
#: No answer came: timeout, hub unreachable, request refused.
UNAVAILABLE = "unavailable"

LINES = {
    "en": {NO_ANSWER: "I don't know how to answer that.",
           UNAVAILABLE: "Sorry, I can't answer right now."},
    "fr": {NO_ANSWER: "Je ne sais pas répondre à ça.",
           UNAVAILABLE: "Désolé, je ne peux pas répondre pour le moment."},
    "es": {NO_ANSWER: "No sé cómo responder a eso.",
           UNAVAILABLE: "Lo siento, ahora mismo no puedo responder."},
    "de": {NO_ANSWER: "Darauf weiß ich keine Antwort.",
           UNAVAILABLE: "Entschuldigung, ich kann gerade nicht antworten."},
    "it": {NO_ANSWER: "Non so come rispondere.",
           UNAVAILABLE: "Mi dispiace, al momento non posso rispondere."},
    "pt": {NO_ANSWER: "Não sei responder a isso.",
           UNAVAILABLE: "Desculpe, não consigo responder agora."},
    "nl": {NO_ANSWER: "Daar weet ik geen antwoord op.",
           UNAVAILABLE: "Sorry, ik kan nu niet antwoorden."},
}

DEFAULT_LANGUAGE = "en"


def language_of(tag: Optional[str]) -> Optional[str]:
    """The base language of a tag (``fr-FR``, ``fr_ca``, ``FR`` -> ``fr``), or None."""
    if not isinstance(tag, str) or not tag.strip():
        return None
    return tag.strip().replace("_", "-").split("-")[0].lower()


def bridge_line(kind: str, *languages: Optional[str]) -> str:
    """The ``kind`` line in the first of ``languages`` there is one for, else English.

    ``languages`` are tags in order of preference, e.g. the sender's own
    language first and the hub's ``HIVEMIND_LANG`` after it; ``None`` and
    languages without lines are skipped.
    """
    for tag in languages:
        lines = LINES.get(language_of(tag) or "")
        if lines:
            return lines[kind]
    return LINES[DEFAULT_LANGUAGE][kind]
