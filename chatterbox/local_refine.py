"""Fully offline, rule-based Polished cleanup: no network, no model, no key.

Deliberately conservative to match REFINEMENT_INSTRUCTIONS in api.py: only
filler removal, whitespace/punctuation normalization, and capitalization.
No rewriting, translation, or summarization is attempted.
"""
from __future__ import annotations

import re

_FILLERS = re.compile(r"(?<!\w)(um+|uh+|erm+|hmm+)(?!\w)[,]?\s*", re.IGNORECASE)
_REPEATED_WORD = re.compile(r"\b(\w+)([ \t]+\1)+\b", re.IGNORECASE)
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,!?;:])")
_SPACE_AFTER_PUNCT = re.compile(r"([.,!?;:])(?=[^\s.,!?;:])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def refine_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Local refinement requires nonempty text.")
    cleaned = _FILLERS.sub("", text)
    cleaned = _REPEATED_WORD.sub(r"\1", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _SPACE_AFTER_PUNCT.sub(r"\1 ", cleaned)
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    if not cleaned:
        return text.strip()
    sentences = []
    for sentence in _SENTENCE_SPLIT.split(cleaned):
        sentence = sentence.strip()
        if sentence and sentence[0].isalpha():
            sentence = sentence[0].upper() + sentence[1:]
        if sentence:
            sentences.append(sentence)
    result = " ".join(sentences) if sentences else cleaned
    if result and result[-1] not in ".!?":
        result += "."
    return result
