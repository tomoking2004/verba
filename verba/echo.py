"""Removal of speaker echo, by comparing transcribed utterances with each other.

The other side's voice coming out of speakers also enters the microphone, and gets transcribed
as an utterance of mine too. The audio is left alone: an utterance of mine whose text overlaps
an utterance of the other side close in time is dropped. On a partial overlap only the
overlapping part is removed.

Weakness: when both sides say the same words close in time, my utterance is wrongly removed.
"""

from __future__ import annotations

import logging
import unicodedata
from difflib import SequenceMatcher

from .config import SPEAKER_ME, SPEAKER_OTHER

log = logging.getLogger(__name__)

MIN_CHARS = 4  # A shorter overlap is kept as coincidence (short responses and the like).
COMMAS = " 　、,，"  # Commas left dangling at the end once the tail is removed
JOINTS = COMMAS + "。.．"  # Punctuation left without a lead once a middle part is removed


def remove_echo(utterances: list[dict], since: float, until: float,
                window: float, threshold: float) -> list[dict]:
    """Return the utterances, with echo removed from mine between since and until.

    The range is widened by window seconds each way.
    """
    others = [u for u in utterances if u["speaker"] == SPEAKER_OTHER]
    kept = []
    for u in utterances:
        if u["speaker"] != SPEAKER_ME or u["end"] < since - window or u["start"] > until + window:
            kept.append(u)
            continue
        heard = "".join(o["text"] for o in others
                        if o["end"] >= u["start"] - window and o["start"] <= u["end"] + window)
        text = strip_echo(u["text"], heard, threshold)
        if text != u["text"]:
            log.info("echo removed at %.1fs: %r -> %r", u["start"], u["text"], text)
        if text:
            kept.append({**u, "text": text})
    return kept


def strip_echo(mine: str, heard: str, threshold: float) -> str:
    """Remove from mine the parts that overlap heard. Return an empty string if all of it overlaps."""
    other, _ = comparable(heard)
    while other:
        own, index = comparable(mine)
        span = _overlap(own, other, threshold)
        if span is None:
            break
        head = mine[: index[span[0]]]
        tail = mine[index[span[1] - 1] + 1:].lstrip(JOINTS)
        mine = head + tail if tail else head.rstrip(COMMAS)
        if len(comparable(mine)[0]) < 2:  # Punctuation or a single character alone means nothing.
            return ""
    return mine


def comparable(text: str) -> tuple[str, list[int]]:
    """Return the characters to compare, and the original position of each.

    Whitespace and punctuation are dropped; full/half width and upper/lower case are unified.
    """
    chars, index = [], []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if c.isalnum():
                chars.append(c)
                index.append(i)
    return "".join(chars), index


def _overlap(own: str, other: str, threshold: float) -> tuple[int, int] | None:
    """Return the longest range of own that fuzzily matches other, or None.

    The matched characters must make up at least threshold of the range's length
    (the longer of the own side and the other side).
    """
    blocks = [b for b in SequenceMatcher(None, own, other, autojunk=False).get_matching_blocks() if b.size >= 2]
    best = None
    for i, first in enumerate(blocks):
        matched = 0
        for last in blocks[i:]:
            matched += last.size
            own_len = last.a + last.size - first.a
            other_len = last.b + last.size - first.b
            if (matched >= MIN_CHARS and matched >= threshold * max(own_len, other_len)
                    and (best is None or own_len > best[1] - best[0])):
                best = (first.a, first.a + own_len)
    return best
