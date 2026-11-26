"""The speaker of an utterance, checked against the levels of the two tracks.

The model is given both tracks, but it does not reliably tell which of them a voice is in: in a
real recording of an hour where only the other side spoke, heard through loudspeakers, it gave
196 of 454 utterances to me, whole segments at a time. It may also write the other side's words
twice, once from each track. The tracks themselves tell. The other side's voice reaches the
microphone only as an echo, at a steady fraction of its level in the system track (about 3.4% in
that recording), and my voice is in the microphone alone.
"""

from __future__ import annotations

import numpy as np

from .audio import BLOCK, QUIET, envelope
from .config import SAMPLE_RATE, SPEAKER_ME, SPEAKER_OTHER
from .echo import comparable, strip_echo

BLOCKS_PER_SECOND = SAMPLE_RATE // BLOCK
PAD = 0.3  # Seconds added on each side of an utterance: the model's times are off by about this much.
MIN_ACTIVE = 50  # Blocks (5 s) of the other side's sound needed to measure the echo; with fewer, no echo is assumed.
OTHER_SILENT = 0.1  # The other side is silent when its track is active in less than this share of an utterance.
# How loud the microphone may be, relative to what the echo and its own noise would give, and still
# hold nothing but the echo. In the recording above, where it held nothing else, the ratio had
# a median of 0.98, a 95th percentile of 2.4 and a 99th of 4.0.
ECHO_MARGIN = 4.0
MIC_SPEAKS = 0.5  # I am speaking when the microphone is active in this share of an utterance.
COPY_WINDOW = 1.0  # Seconds by which the two copies of the same words may be apart.
COPY_MATCH = 0.8  # How much of the text must match to count as the same words (as echo_threshold in config.py).


def check(utterances: list[dict], mic: np.ndarray, system: np.ndarray) -> list[dict]:
    """Return the utterances of a segment, each with the speaker the two tracks point to.

    start and end are seconds within the segment. The model's speaker is kept wherever the
    levels do not tell: both sides speak at once, or neither track is clearly active.
    An utterance given to me that the microphone holds only as the echo of the other side is
    theirs; when the model also wrote it from their track, it is a copy and is dropped.
    Applied to the recording above, this left 2 of the 196 and changed no utterance wrongly.
    """
    m, y = envelope(mic), envelope(system)
    if not len(m):
        return utterances
    active = y > QUIET
    # How much of the other side's level arrives in the microphone, and the microphone's own noise.
    gain = float(np.median(m[active] / y[active])) if active.sum() >= MIN_ACTIVE else 0.0
    floor = float(np.percentile(m, 10))
    theirs = [u for u in utterances if u["speaker"] == SPEAKER_OTHER]
    checked = []
    for u in utterances:
        a = min(max(int((u["start"] - PAD) * BLOCKS_PER_SECOND), 0), len(m) - 1)
        b = min(max(int(np.ceil((u["end"] + PAD) * BLOCKS_PER_SECOND)), a + 1), len(m))
        other = float(active[a:b].mean())
        excess = float(m[a:b].mean()) / (gain * float(y[a:b].mean()) + floor + 1e-9)
        speaker = u["speaker"]
        if other >= OTHER_SILENT and excess < ECHO_MARGIN:
            if speaker == SPEAKER_ME and _copied(u, theirs):
                continue
            speaker = SPEAKER_OTHER  # The microphone holds no more than the echo.
        elif other < OTHER_SILENT and float((m[a:b] > QUIET).mean()) >= MIC_SPEAKS:
            speaker = SPEAKER_ME  # The other side is silent and the microphone is not.
        checked.append({**u, "speaker": speaker})
    return checked


def _copied(u: dict, theirs: list[dict]) -> bool:
    """Whether the other side's utterances about the same time hold most of u's words.

    What is left over is the words the two copies spell differently (fillers and the like).
    """
    near = [o for o in theirs if o["end"] >= u["start"] - COPY_WINDOW and o["start"] <= u["end"] + COPY_WINDOW]
    words = comparable(u["text"])[0]
    if not near or not words:
        return False
    if any(comparable(o["text"])[0] == words for o in near):  # Short words, too short for strip_echo
        return True
    rest = comparable(strip_echo(u["text"], "".join(o["text"] for o in near), COPY_MATCH))[0]
    return len(rest) * 2 < len(words)
