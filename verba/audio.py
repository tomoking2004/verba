"""Simultaneous recording of the microphone (me) and the system audio (the other side).

The two inputs are aligned in time and written, as they come, to a stereo Opus file
(left = me, right = the other side). Along the way "segments" are cut at a pause in speech
after the target length, and handed to a callback.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import soundcard as sc
import soundfile as sf

from .config import SAMPLE_RATE

log = logging.getLogger(__name__)

BLOCK = SAMPLE_RATE // 10  # Read every 0.1 s.
MAX_SKEW = SAMPLE_RATE // 2  # When one input lags by more than 0.5 s, pad it with silence to align.
QUIET_TAIL = int(SAMPLE_RATE * 0.4)  # This much quiet counts as a pause in speech.
OVERRUN = 20  # Seconds past the target length after which a cut is forced, pause or not
MIN_TAIL = SAMPLE_RATE // 2  # A segment shorter than this (the remainder at the end of a recording) cannot hold speech.
QUIET = 0.006  # An RMS level below this is not speech.

SegmentCallback = Callable[[float, float, np.ndarray, np.ndarray], None]


def list_devices() -> dict:
    """Return the microphones and output devices to offer as choices."""
    def entry(dev):
        return {"id": str(dev.id), "name": str(dev.name)}

    try:
        default_mic = str(sc.default_microphone().id)
    except Exception:  # noqa: BLE001
        default_mic = None
    try:
        default_speaker = str(sc.default_speaker().id)
    except Exception:  # noqa: BLE001
        default_speaker = None
    return {
        "mics": [entry(d) for d in sc.all_microphones(include_loopback=False)],
        "speakers": [entry(d) for d in sc.all_speakers()],
        "default_mic": default_mic,
        "default_speaker": default_speaker,
    }


def _find_mic(device_id: str | None):
    if device_id:
        for dev in sc.all_microphones(include_loopback=False):
            if str(dev.id) == device_id:
                return dev
    return sc.default_microphone()


def _find_loopback(speaker_id: str | None):
    speaker = None
    if speaker_id:
        speaker = next((s for s in sc.all_speakers() if str(s.id) == speaker_id), None)
    speaker = speaker or sc.default_speaker()
    return sc.get_microphone(id=str(speaker.id), include_loopback=True)


def encode_flac(samples: np.ndarray) -> bytes:
    """Return a segment's audio, losslessly compressed, to send to Gemini."""
    buf = io.BytesIO()
    sf.write(buf, samples, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


def read_range(path: Path, start: float, end: float) -> tuple[np.ndarray, np.ndarray]:
    """Read a segment back from the recorded file (for retries, a re-transcription, and work left over at startup)."""
    data, _ = sf.read(str(path), start=int(start * SAMPLE_RATE), stop=int(end * SAMPLE_RATE),
                      dtype="float32", always_2d=True)
    if data.shape[1] < 2:
        return data[:, 0], np.zeros(len(data), dtype="float32")
    return data[:, 0], data[:, 1]


def split(path: Path, segment_seconds: int) -> list[tuple[float, float]]:
    """Cut a recorded file into segments anew, by the same rule as while recording.

    Return (start, end) pairs in seconds. The remainder at the end is the last segment, however short.
    """
    window, spans, start = _Window(segment_seconds), [], 0
    with sf.SoundFile(str(path)) as f:
        for block in f.blocks(blocksize=SAMPLE_RATE // 4, dtype="float32", always_2d=True):
            window.add(block[:, 0], block[:, -1])
            if window.due():
                spans.append((start, start + window.frames))
                start += window.frames
                window.take()
    spans.append((start, start + window.frames))
    return [(a / SAMPLE_RATE, b / SAMPLE_RATE) for a, b in spans]


def is_silent(mic: np.ndarray, system: np.ndarray) -> bool:
    """Whether the segment is too short, or neither track loud enough, to hold speech (such a segment is not sent)."""
    def loudest(x: np.ndarray) -> float:
        n = len(x) // BLOCK
        if n == 0:
            return 0.0
        frames = x[: n * BLOCK].reshape(n, BLOCK)
        return float(np.sqrt((frames ** 2).mean(axis=1)).max())

    return len(mic) < MIN_TAIL or (loudest(mic) < QUIET and loudest(system) < QUIET)


class _Window:
    """The segment being accumulated. It is cut at a pause in speech after reaching the target length."""

    def __init__(self, segment_seconds: int) -> None:
        self.target = segment_seconds * SAMPLE_RATE
        self.frames = 0
        self._chunks: list[tuple[np.ndarray, np.ndarray]] = []

    def add(self, mic: np.ndarray, system: np.ndarray) -> None:
        self._chunks.append((mic, system))
        self.frames += len(mic)

    def due(self) -> bool:
        """Whether to cut now."""
        if self.frames < self.target:
            return False
        if self.frames >= self.target + OVERRUN * SAMPLE_RATE:
            return True
        tail = np.concatenate([m + s for m, s in self._chunks[-12:]])[-QUIET_TAIL:]
        whole = np.concatenate([m + s for m, s in self._chunks])
        n = len(whole) // BLOCK
        rms = np.sqrt((whole[: n * BLOCK].reshape(n, BLOCK) ** 2).mean(axis=1))
        floor = float(np.percentile(rms, 20))
        return float(np.sqrt((tail ** 2).mean())) < max(QUIET, floor * 1.5)

    def take(self) -> tuple[np.ndarray, np.ndarray]:
        """Return the two accumulated tracks and start over empty."""
        empty = np.zeros(0, dtype="float32")
        mic = np.concatenate([empty] + [m for m, _ in self._chunks])
        system = np.concatenate([empty] + [s for _, s in self._chunks])
        self._chunks, self.frames = [], 0
        return mic, system


class _Input:
    """Keeps reading one input device on a thread of its own."""

    def __init__(self, name: str, device) -> None:
        self.name = name
        self.device = device
        self.level = 0.0
        self.error: str | None = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._running = False
        self._accepting = True
        self._thread = threading.Thread(target=self._run, name=f"input-{name}", daemon=True)

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def set_accepting(self, accepting: bool) -> None:
        with self._lock:
            self._accepting = accepting
            if not accepting:
                self._chunks.clear()

    def drain(self) -> np.ndarray:
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")

    def _run(self) -> None:
        channels = max(1, min(2, int(getattr(self.device, "channels", 1) or 1)))
        try:
            with self.device.recorder(samplerate=SAMPLE_RATE, channels=channels, blocksize=BLOCK) as rec:
                while self._running:
                    block = rec.record(numframes=BLOCK)
                    mono = block.mean(axis=1).astype("float32") if block.ndim == 2 else block.astype("float32")
                    self.level = float(np.sqrt((mono ** 2).mean())) if len(mono) else 0.0
                    with self._lock:
                        if self._accepting:
                            self._chunks.append(mono)
        except Exception as exc:  # noqa: BLE001
            # Losing one input does not stop the other (the missing side is padded with silence).
            log.exception("input %s failed", self.name)
            self.error = str(exc)
            self.level = 0.0


class Capture:
    """Records the two tracks, mine and the other side's, aligned, and calls back for each segment."""

    def __init__(self, path: Path, mic_id: str | None, speaker_id: str | None,
                 segment_seconds: int, on_segment: SegmentCallback) -> None:
        self.path = path
        self.on_segment = on_segment
        self.frames = 0  # Frames written so far = recorded time (does not advance while paused)
        self.paused = False
        self._mic = _Input("mic", _find_mic(mic_id))
        self._system = _Input("system", _find_loopback(speaker_id))
        self._pending = (np.zeros(0, "float32"), np.zeros(0, "float32"))
        self._window = _Window(segment_seconds)
        self._window_start = 0
        self._file: sf.SoundFile | None = None
        self._running = False
        self._step_lock = threading.Lock()
        self._aligner = threading.Thread(target=self._align_loop, name="aligner", daemon=True)

    @property
    def device_names(self) -> dict:
        """The names of the devices in use."""
        return {"mic": str(self._mic.device.name), "speaker": str(self._system.device.name)}

    @property
    def seconds(self) -> float:
        """Recorded time in seconds."""
        return self.frames / SAMPLE_RATE

    def levels(self) -> dict:
        """Return the current input levels, and which inputs have stopped (the reason is in app.log)."""
        return {"me": self._mic.level, "other": self._system.level,
                "errors": {k: True for k, v in (("mic", self._mic.error), ("speaker", self._system.error)) if v}}

    def start(self) -> None:
        """Open the file and start reading both inputs."""
        self._file = sf.SoundFile(str(self.path), "w", SAMPLE_RATE, 2, format="OGG", subtype="OPUS")
        self._running = True
        try:
            self._mic.start()
            self._system.start()
            self._aligner.start()
        except Exception:  # Leave nothing running and the file closed: the caller deletes the session.
            self._running = False
            self._mic.stop()
            self._system.stop()
            self._file.close()
            raise

    def pause(self) -> None:
        """Stop taking in audio until resume()."""
        self.paused = True
        self._mic.set_accepting(False)
        self._system.set_accepting(False)

    def resume(self) -> None:
        """Take in audio again."""
        with self._step_lock:
            self._pending = (np.zeros(0, "float32"), np.zeros(0, "float32"))
        self.paused = False
        self._mic.set_accepting(True)
        self._system.set_accepting(True)

    def stop(self) -> tuple[float, float, np.ndarray, np.ndarray]:
        """End the recording and close the file.

        Return the remainder, which is the last segment however short: (start, end, mic, system).
        It is not handed to the callback, so that the caller can queue it once the session is marked as ended.
        """
        self._running = False
        self._mic.stop()
        self._system.stop()
        self._aligner.join(timeout=5)
        try:
            self._step(final=True)
        finally:  # Closed even if the last write fails, so that the file can be read and deleted.
            if self._file is not None:
                file, self._file = self._file, None
                file.close()
        mic, system = self._window.take()
        return self._window_start / SAMPLE_RATE, self.seconds, mic, system

    def _align_loop(self) -> None:
        while self._running:
            time.sleep(0.25)
            try:
                self._step()
            except Exception:  # noqa: BLE001
                log.exception("aligner step failed")

    def _step(self, final: bool = False) -> None:
        with self._step_lock:
            self._step_locked(final)

    def _step_locked(self, final: bool) -> None:
        mic = np.concatenate([self._pending[0], self._mic.drain()])
        system = np.concatenate([self._pending[1], self._system.drain()])
        if final or abs(len(mic) - len(system)) > MAX_SKEW:
            n = max(len(mic), len(system))
            mic = np.pad(mic, (0, n - len(mic)))
            system = np.pad(system, (0, n - len(system)))
        else:
            n = min(len(mic), len(system))
        self._pending = (mic[n:], system[n:])
        if n == 0:
            return
        mic, system = mic[:n], system[:n]
        self._file.write(np.stack([mic, system], axis=1))
        self._window.add(mic, system)
        self.frames += n
        if not final and self._window.due():
            self._cut()

    def _cut(self) -> None:
        mic, system = self._window.take()
        start = self._window_start / SAMPLE_RATE
        end = (self._window_start + len(mic)) / SAMPLE_RATE
        self._window_start += len(mic)
        try:
            self.on_segment(start, end, mic, system)
        except Exception:  # noqa: BLE001
            log.exception("segment callback failed")
