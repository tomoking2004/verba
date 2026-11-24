"""The queue that transcribes segments in order, on a thread of its own.

Segments are processed one at a time in recording order, because the preceding utterances
are passed along as context. The last segment of a finished recording also completes the
transcript: misspelled proper nouns are looked for in the whole of it and fixed, and the time
is stamped. A recording always ends with a segment (controller.py), so there is always one to do it.
Audio comes from memory the first time a segment of a live recording is tried, and from the file
otherwise (retries, a re-transcription, and work left over from the previous run). A failed
segment's audio is let go at once, so that a recording made while Gemini cannot be reached
does not pile up in memory.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import numpy as np

from .audio import encode_flac, is_silent, read_range
from .config import SAMPLE_RATE, Settings
from .echo import remove_echo
from .gemini import Gemini, GeminiError
from .store import NotFound, Store, new_id, timestamp

log = logging.getLogger(__name__)

CONTEXT_UTTERANCES = 8  # How many preceding utterances are passed as context
# The file of a recording in progress can be read, except about the last second, which the encoder
# still holds. A segment read back before the file reaches its end is waited for this long (seconds).
FILE_WAIT = 3.0
FILE_POLL = 0.25  # Seconds between looks at the file during that wait


class Transcriber:
    """Transcribes queued segments and writes the results into the store."""

    def __init__(self, store: Store, gemini: Gemini, settings: Settings) -> None:
        self.store = store
        self.gemini = gemini
        self.settings = settings
        self._queue: deque[tuple[str, str]] = deque()
        self._audio: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
        self._cond = threading.Condition()
        self._busy: tuple[str, str] | None = None
        self._again: set[tuple[str, str]] = set()  # Submitted while in hand: queued again once let go
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self._thread.start()

    def submit(self, sid: str, seg_id: str, audio: tuple[np.ndarray, np.ndarray] | None = None) -> None:
        """Queue a segment. audio is its (mic, system) samples if they are at hand; otherwise the file is read."""
        key = (sid, seg_id)
        with self._cond:
            if audio is not None:
                self._audio[key] = audio
            if key == self._busy:
                # A retry can arrive after the failure is saved but before the thread lets go of the
                # segment. Dropping it would leave the segment "queued" for good.
                self._again.add(key)
            elif key not in self._queue:
                self._queue.append(key)
            self._cond.notify()

    def pending(self, sid: str) -> int:
        """Return how many segments of a session are queued or being transcribed."""
        with self._cond:
            return sum(1 for s, _ in self._queue if s == sid) + (1 if self._busy and self._busy[0] == sid else 0)

    def stop(self) -> None:
        """Stop the thread once the segment in hand is done."""
        with self._cond:
            self._stopping = True
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                job = self._busy = self._queue.popleft()
            try:
                self._process(*job)
            except Exception:  # noqa: BLE001
                log.exception("transcription crashed for %s", job)
                self._fail(*job, "文字起こしに失敗しました。もう一度試してください")
            finally:
                with self._cond:
                    self._busy = None
                    if job in self._again:
                        self._again.discard(job)
                        self._queue.append(job)

    def _process(self, sid: str, seg_id: str) -> None:
        try:
            session = self.store.get(sid)
        except (NotFound, OSError):
            session = None  # The session was deleted while the segment was queued.
        seg = session and _segment(session, seg_id)
        if seg is None or seg["status"] in ("done", "silent"):
            self._forget(sid, seg_id)
            return

        with self._cond:
            audio = self._audio.get((sid, seg_id))
        if audio is None:
            try:
                audio = self._read(sid, seg)
            except Exception:  # noqa: BLE001
                log.exception("cannot read audio for %s/%s", sid, seg_id)
                self._fail(sid, seg_id, "録音のファイルを読めませんでした")
                return
            if audio is None:
                log.warning("the file does not reach the end of %s/%s yet", sid, seg_id)
                self._fail(sid, seg_id,
                           "この区間の終わりが、まだ録音のファイルに書かれていません。録音を再開するか、停止してから試してください")
                return
        mic, system = audio

        sent = not is_silent(mic, system)
        utterances = []
        if sent:
            self._set(sid, seg_id, "working")
            context = "\n".join(
                f"{u['speaker']}: {u['text']}"
                for u in [u for u in session["utterances"] if u["start"] < seg["start"]][-CONTEXT_UTTERANCES:]
            )
            try:
                utterances = self.gemini.transcribe(
                    encode_flac(mic), encode_flac(system), seg["end"] - seg["start"],
                    title=session["title"], memo=session["memo"], context=context,
                )
            except GeminiError as exc:
                self._fail(sid, seg_id, str(exc))
                return

        last = self._is_last(sid, seg_id)
        fixes, error = self._find_name_fixes(sid, seg_id, utterances, working=sent) if last else ([], None)

        def apply(s: dict) -> None:
            target = _segment(s, seg_id)
            if target is None:
                return
            # Whatever the segment held before goes, also when it turns out silent (a redo cuts the segments anew).
            s["utterances"] = [u for u in s["utterances"] if u.get("segment") != seg_id]
            for u in utterances:
                s["utterances"].append({"id": new_id(), "segment": seg_id, "speaker": u["speaker"],
                                        "start": round(seg["start"] + u["start"], 2),
                                        "end": round(seg["start"] + u["end"], 2), "text": u["text"]})
            s["utterances"].sort(key=lambda u: u["start"])
            if utterances and s["echo_removal"]:
                # Utterances of the neighbouring segments are compared too, to catch echo across a boundary.
                s["utterances"] = remove_echo(s["utterances"], seg["start"], seg["end"],
                                              self.settings.get("echo_window"), self.settings.get("echo_threshold"))
            if last:
                apply_name_fixes(s, fixes, error)
                s["transcribed_at"] = timestamp() if s["utterances"] else None
            target.update(status="done" if sent else "silent", error=None)

        try:
            self.store.mutate(sid, apply)
        except (NotFound, OSError):
            pass
        self._forget(sid, seg_id)

    def _read(self, sid: str, seg: dict) -> tuple[np.ndarray, np.ndarray] | None:
        """Read a segment back from the file. Return None if a recording in progress has not written its end yet.

        A read past what is written comes back short without an error, which would lose the end of
        the segment unnoticed. While recording goes on the file catches up within about a second;
        it does not while paused.
        """
        wanted = int(seg["end"] * SAMPLE_RATE) - int(seg["start"] * SAMPLE_RATE)
        deadline = time.monotonic() + FILE_WAIT
        while True:
            # Asked before each read: once the recording has ended, the file holds all there is.
            live = self.store.get(sid)["status"] == "recording"
            audio = read_range(self.store.audio_path(sid), seg["start"], seg["end"])
            if not live or len(audio[0]) >= wanted:
                return audio
            if time.monotonic() >= deadline:
                return None
            time.sleep(FILE_POLL)

    def _is_last(self, sid: str, seg_id: str) -> bool:
        """Whether recording has ended and no other segment is waiting."""
        try:
            s = self.store.get(sid)  # Read again after transcribing: recording may have just stopped.
        except (NotFound, OSError):
            return False
        return s["status"] == "done" and not any(
            seg["id"] != seg_id and seg["status"] in ("queued", "working") for seg in s["segments"])

    def _find_name_fixes(self, sid: str, seg_id: str, utterances: list[dict],
                         working: bool) -> tuple[list[tuple[str, str]], str | None]:
        """On the last segment of a session, look for misspelled proper nouns in the whole transcript.

        Return (replacement pairs, the reason the search failed). The segment shows as "working"
        meanwhile (working tells whether it already does; a silent one does not). A failed search
        does not hold back the transcript.
        """
        try:
            s = self.store.get(sid)
        except (NotFound, OSError):
            return [], None
        seg = _segment(s, seg_id)
        kept = [u for u in s["utterances"] if u.get("segment") != seg_id]
        new = [{**u, "start": seg["start"] + u["start"]} for u in utterances]
        if kept and not working:
            self._set(sid, seg_id, "working")
        return find_name_fixes(self.gemini, s, sorted(kept + new, key=lambda u: u["start"]))

    def _set(self, sid: str, seg_id: str, status: str, error: str | None = None) -> None:
        def apply(s: dict) -> None:
            seg = _segment(s, seg_id)
            if seg is not None:
                seg.update(status=status, error=error)

        try:
            self.store.mutate(sid, apply)
        except (NotFound, OSError):
            pass

    def _fail(self, sid: str, seg_id: str, error: str) -> None:
        """Mark the segment failed and let go of its audio. A retry reads the file, also while recording."""
        self._set(sid, seg_id, "error", error)
        self._forget(sid, seg_id)

    def _forget(self, sid: str, seg_id: str) -> None:
        with self._cond:
            self._audio.pop((sid, seg_id), None)


def find_name_fixes(gemini: Gemini, session: dict, utterances: list[dict]) -> tuple[list[tuple[str, str]], str | None]:
    """Look for proper-noun replacements in all the utterances.

    Return (replacement pairs, the reason the search failed).
    """
    lines = [f"{u['speaker']}: {u['text']}" for u in utterances]
    if not lines:
        return [], None
    try:
        return gemini.fix_names(lines, session["title"], session["memo"]), None
    except GeminiError as exc:
        log.warning("name fixes skipped for %s: %s", session["id"], exc)
        return [], str(exc)


def apply_name_fixes(session: dict, fixes: list[tuple[str, str]], error: str | None) -> None:
    """Apply the replacements to the utterances, and record the reason if the search failed.

    The UI offers a retry when a reason is recorded.
    """
    for u in session["utterances"]:
        for old, new in fixes:
            u["text"] = u["text"].replace(old, new)
    session["names_error"] = error


def _segment(session: dict, seg_id: str) -> dict | None:
    return next((seg for seg in session["segments"] if seg["id"] == seg_id), None)
