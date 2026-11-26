"""The core of the app: drives recording, transcription and storage together."""

from __future__ import annotations

import logging
import threading

import soundfile as sf

from .audio import CannotWrite, Capture, list_devices, split
from .config import (BATCH_SECONDS, SAMPLE_RATE, SPEAKER_ME, SPEAKER_OTHER, USAGE_PATH, Settings, load_api_key,
                     save_api_key)
from .errors import UserError
from .gemini import Gemini, GeminiError, Usage
from .store import NotFound, Store, clock, new_segment, show_version, timestamp, versions_of
from .transcriber import Transcriber, apply_name_fixes, find_name_fixes

log = logging.getLogger(__name__)


class Controller:
    """Every operation the UI can ask for. Failures the user should read are raised as UserError."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.store = Store()
        self.usage = Usage(USAGE_PATH)
        self.gemini = Gemini(api_key=load_api_key, model=lambda: self.settings.get("model"), usage=self.usage)
        self.transcriber = Transcriber(self.store, self.gemini, self.settings)
        self._lock = threading.RLock()
        self._capture: Capture | None = None
        self._recording_sid: str | None = None
        self._summarizing: set[str] = set()
        self._fixing_names: set[str] = set()
        # Sessions whose recording is being cut into pieces, each with whether the pieces are to be
        # queued (a cancellation meanwhile turns it off, or takes the session out so that the cut is dropped).
        self._preparing: dict[str, dict] = {}
        self._recover()

    # ---- Settings ----
    def set_api_key(self, key: str) -> dict:
        """Check the API key, then save it. It is saved even when the check cannot reach Google."""
        key = key.strip()
        if not key:
            raise UserError("API キーを保存できませんでした。キーが空です")
        ok = self.gemini.check_key(key)
        if ok is False:
            raise UserError("API キーを保存できませんでした。Google がこのキーを受け付けません")
        save_api_key(key)
        return {"checked": ok is True}

    # ---- Recording ----
    def start(self, title: str = "", memo: str = "", muted: dict | None = None) -> dict:
        """Create a session and start recording into it. Return the session.

        muted tells which tracks ("me", "other") start muted.
        """
        with self._lock:
            if self._capture is not None:
                raise UserError("録音を開始できませんでした。すでに録音中です")
            if not self.gemini.available:
                raise UserError("録音を開始できませんでした。Gemini の API キーが設定されていません")
            muted = {key: bool(muted.get(key)) for key in ("me", "other")} if isinstance(muted, dict) else None
            live = self.settings.get("live")
            session = self.store.create(title, memo, self.settings.get("echo_removal"), live)
            sid = session["id"]
            try:
                capture = Capture(self.store.audio_path(sid), self.settings.get("mic"), self.settings.get("speaker"),
                                  self.settings.get("segment_seconds") if live else None,
                                  on_segment=lambda start, end, mic, system: self._on_segment(sid, start, end, mic, system),
                                  muted=muted)
                capture.start()
            except Exception as exc:
                log.exception("cannot start capture")
                self.store.delete(sid)
                why = "保存先に書き込めません" if isinstance(exc, CannotWrite) else "「自分の声」か「相手の声」のデバイスを開けません"
                raise UserError(f"録音を開始できませんでした。{why}") from exc
            self._capture, self._recording_sid = capture, sid
            log.info("recording started: %s (%s)", sid, capture.device_names)
            return self.store.get(sid)

    def pause(self) -> None:
        """Pause the recording, if there is one."""
        with self._lock:
            if self._capture:
                self._capture.pause()

    def resume(self) -> None:
        """Resume the paused recording, if there is one."""
        with self._lock:
            if self._capture:
                self._capture.resume()

    def mute(self, me: bool | None = None, other: bool | None = None) -> None:
        """Mute or unmute a track of the recording, if there is one. None leaves a track as it is."""
        with self._lock:
            if self._capture:
                self._capture.set_muted(me, other)

    def stop(self) -> None:
        """Stop recording, if there is one. The session is ended even if the recording cannot be closed."""
        with self._lock:
            if self._capture is None:
                return
            capture, sid = self._capture, self._recording_sid
            self._capture, self._recording_sid = None, None
        try:
            rest = capture.stop()
        except Exception as exc:  # The last write failed (a full disk, for example).
            log.exception("cannot close the recording of %s", sid)
            try:
                self._close(sid, capture.seconds)
                self._queue_rest(sid)
            except Exception:  # noqa: BLE001  The same cause may stop this save too; startup recovers the session.
                log.exception("cannot end the session %s", sid)
            raise UserError("録音を最後まで保存できませんでした。保存先に書き込めません") from exc
        if rest is None:  # Not transcribed while recording: the whole file is cut into pieces now.
            end = capture.seconds
            with self._lock:  # So that the session is never seen ended, without segments and not being cut.
                self.store.mutate(sid, lambda s: s.update(status="done", duration=round(end, 2)))
                self._prepare(sid)
        else:
            # The remainder becomes the last segment, in the same save that ends the session: the transcriber
            # completes the transcript on the last segment of an ended session, so there must always be one.
            start, end, mic, system = rest
            seg = new_segment(start, end)
            self.store.mutate(sid, lambda s: (s["segments"].append(seg), s.update(status="done", duration=round(end, 2))))
            self.transcriber.submit(sid, seg["id"], (mic, system))
        log.info("recording stopped: %s (%s)", sid, clock(end))

    def _on_segment(self, sid: str, start: float, end: float, mic, system) -> None:
        seg = new_segment(start, end)
        self.store.mutate(sid, lambda s: (s["segments"].append(seg), s.update(duration=seg["end"])))
        self.transcriber.submit(sid, seg["id"], (mic, system))

    # ---- State ----
    def state(self) -> dict:
        """Return what the UI polls: the live recording, whether a key is set, and the model in use."""
        with self._lock:
            capture, sid = self._capture, self._recording_sid
        rec = None
        if capture is not None:
            rec = {"session": sid, "seconds": capture.seconds, "paused": capture.paused,
                   "levels": capture.levels(), "devices": capture.device_names,
                   "muted": dict(capture.muted), "live": capture.live}
        return {"recording": rec, "has_key": self.gemini.available, "model": self.gemini.status()}

    def session(self, sid: str) -> dict:
        """Return a session together with the work in progress on it.

        The other versions of the transcript stay behind (each is as large as the transcript):
        versions tells how many there are with the shown one, version which of them is shown,
        can_move whether another can be shown now, and redoing whether the shown one is a
        re-transcription yet to complete.
        """
        s = self.store.get(sid)
        s["versions"] = len(s["versions"]) + 1
        s["redoing"] = s.pop("redo_from") is not None
        with self._lock:
            s["pending"] = self.transcriber.pending(sid)
            s["preparing"] = sid in self._preparing
            s["summarizing"] = sid in self._summarizing
            s["fixing_names"] = sid in self._fixing_names
            s["can_move"] = s["versions"] > 1 and not self._in_progress(sid)
        return s

    def _in_progress(self, sid: str) -> bool:
        """Whether the session's transcript is still being made (call with the lock held)."""
        return bool(sid == self._recording_sid or self.transcriber.pending(sid)
                    or sid in self._fixing_names or sid in self._preparing)

    def devices(self) -> dict:
        """Return the microphones and output devices to choose from."""
        return list_devices()

    # ---- Editing a session ----
    def update_session(self, sid: str, changes: dict) -> dict:
        """Apply edits to the title, the memo and utterances (text, speaker). Return the session.

        An utterance whose text is emptied is removed.
        """
        def apply(s: dict) -> None:
            for key in ("title", "memo"):
                if key in changes:
                    s[key] = str(changes[key]).strip()
            for edit in changes.get("utterances", []):
                u = next((u for u in s["utterances"] if u["id"] == edit.get("id")), None)
                if u is None:
                    continue
                if "text" in edit:
                    u["text"] = str(edit["text"]).strip()
                if edit.get("speaker") in (SPEAKER_ME, SPEAKER_OTHER):
                    u["speaker"] = edit["speaker"]
            s["utterances"] = [u for u in s["utterances"] if u["text"]]
        self.store.mutate(sid, apply)
        return self.session(sid)

    def delete_session(self, sid: str) -> None:
        """Delete a session, unless it is the one being recorded."""
        with self._lock:
            if sid == self._recording_sid:
                raise UserError("削除できませんでした。録音中の記録です")
        self.store.delete(sid)

    def retry(self, sid: str) -> int:
        """Put the failed and the stopped segments back in the queue. Return how many."""
        again = self.store.mutate(sid, lambda s: [
            seg.update(status="queued", error=None) or seg["id"]
            for seg in s["segments"] if seg["status"] in ("error", "stopped")
        ])
        for seg_id in again:
            self.transcriber.submit(sid, seg_id)
        return len(again)

    def retranscribe(self, sid: str) -> None:
        """Transcribe the whole recording again with the current title and memo, in long pieces.

        The transcript so far stays as a version (show() and cancel() bring it back). The new one
        is added after the last version and starts empty.
        """
        with self._lock:
            if sid == self._recording_sid:
                raise UserError("作り直せませんでした。この記録はまだ録音中です")
            if self._in_progress(sid):
                raise UserError("作り直せませんでした。文字起こしの途中です")
            self.store.get(sid)  # Raises if there is no such session.
            try:
                sf.info(str(self.store.audio_path(sid)))
            except Exception as exc:
                log.exception("cannot read audio of %s", sid)
                raise UserError("作り直せませんでした。録音のファイルを読めません") from exc
            self.store.mutate(sid, lambda s: s.update(
                versions=versions_of(s), version=len(s["versions"]) + 1, redo_from=s["version"],
                segments=[], utterances=[], transcribed_at=None, names_error=None))
            self._prepare(sid)

    def cancel(self, sid: str) -> bool:
        """Call off the transcription of a session. Return whether another version was put back.

        A re-transcription is undone: its transcript goes, and the version shown when it started is
        shown again. Otherwise the segments not yet transcribed are marked "stopped", and a retry
        takes them up again.
        """
        with self._lock:
            if sid == self._recording_sid:
                raise UserError("中止できませんでした。この記録はまだ録音中です")
            # First the transcriber, then the session: no result can land after the change below (transcriber.py).
            self.transcriber.cancel(sid)

            def apply(s: dict) -> bool:
                if s["redo_from"] is not None:
                    versions = versions_of(s)
                    del versions[s["version"]]
                    show_version(s, versions, s["redo_from"])
                    s["redo_from"] = None
                    return True
                for seg in s["segments"]:
                    if seg["status"] in ("queued", "working"):
                        seg.update(status="stopped", error=None)
                return False

            restored = self.store.mutate(sid, apply)
            if restored:
                self._preparing.pop(sid, None)  # A cut in progress finds itself dropped.
            elif sid in self._preparing:
                # The pieces are still cut, so that the recording has segments to retry, but they are not queued.
                self._preparing[sid]["queue"] = False
            return restored

    def switch_version(self, sid: str, index: int) -> None:
        """Show the version of the transcript at index (from 0, in the order they were made).

        The one shown so far is kept as it is, edits and all. A re-transcription that did not
        complete (some piece failed) becomes a version like the others.
        """
        with self._lock:
            if self._in_progress(sid):
                raise UserError("版を切り替えられませんでした。文字起こしの途中です")

            def apply(s: dict) -> bool:
                versions = versions_of(s)
                if type(index) is not int or not 0 <= index < len(versions):
                    return False
                show_version(s, versions, index)
                s["redo_from"] = None
                return True

            if not self.store.mutate(sid, apply):
                raise UserError("版を切り替えられませんでした。その版の文字起こしはありません")

    def _prepare(self, sid: str) -> None:
        """Cut the recording of an ended session into long pieces and queue them, in the background.

        Cutting an hour takes seconds. session() tells that it is going on.
        """
        ticket = {"queue": True}
        with self._lock:
            self._preparing[sid] = ticket
        threading.Thread(target=self._cut, args=(sid, ticket), daemon=True).start()

    def _cut(self, sid: str, ticket: dict) -> None:
        try:
            try:
                spans = split(self.store.audio_path(sid), BATCH_SECONDS)
            except Exception:  # noqa: BLE001
                log.exception("cannot split audio of %s", sid)
                spans = None

            def apply(s: dict) -> list[str]:
                # A file that cannot be read becomes one piece: an ended recording always has a segment.
                # Reading it fails again in the transcriber, which shows as that segment's failure.
                s["segments"] = [new_segment(start, end) for start, end in spans or [(0.0, s["duration"])]]
                if ticket["queue"]:
                    return [seg["id"] for seg in s["segments"]]
                for seg in s["segments"]:
                    seg["status"] = "stopped"
                return []

            with self._lock:  # Held over the save and the queueing, so that a cancellation comes before or after both.
                if self._preparing.get(sid) is ticket:
                    for seg_id in self.store.mutate(sid, apply):
                        self.transcriber.submit(sid, seg_id)
        except (NotFound, OSError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("cannot prepare %s", sid)
        finally:
            with self._lock:
                if self._preparing.get(sid) is ticket:
                    del self._preparing[sid]

    def fix_names(self, sid: str) -> None:
        """Redo, in the background, only the proper-noun fix that was skipped (the transcript is not redone)."""
        with self._lock:
            if sid in self._fixing_names:
                return
            if self._in_progress(sid):
                raise UserError("固有名詞を直せませんでした。文字起こしの途中です")
            self.store.get(sid)  # Raises if there is no such session.
            self._fixing_names.add(sid)
        threading.Thread(target=self._fix_names, args=(sid,), daemon=True).start()

    def _fix_names(self, sid: str) -> None:
        try:
            session = self.store.get(sid)
            fixes, error = find_name_fixes(self.gemini, session, session["utterances"])
            self.store.mutate(sid, lambda s: apply_name_fixes(s, fixes, error))
        except (NotFound, OSError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("name fixes failed")
        finally:
            with self._lock:
                self._fixing_names.discard(sid)

    # ---- Summary ----
    def summarize(self, sid: str) -> None:
        """Make the summary in the background. session() tells that it is being made."""
        with self._lock:
            if sid in self._summarizing:
                return
            session = self.store.get(sid)
            if not session["utterances"]:
                raise UserError("要約できませんでした。文字起こしがまだありません")
            self._summarizing.add(sid)
        threading.Thread(target=self._summarize, args=(sid, session), daemon=True).start()

    def _summarize(self, sid: str, session: dict) -> None:
        title, error = "", None
        transcript = "\n".join(f"[{clock(u['start'])}] {u['speaker']}: {u['text']}" for u in session["utterances"])
        try:
            text, model, title = self.gemini.summarize(transcript, session["title"], session["memo"])
            summary = {"text": text.strip(), "model": model, "created_at": timestamp(), "error": None}
        except GeminiError as exc:
            error = str(exc)
        except Exception:  # noqa: BLE001
            log.exception("summary failed")
            error = "要約に失敗しました。もう一度試してください"
        if error:  # The summary made before, if any, stays; only the reason is added to it.
            old = session.get("summary") or {}
            summary = {**old, "text": old.get("text", ""), "error": error}
        try:
            def apply(s: dict) -> None:
                s["summary"] = summary
                if title and not s["title"]:  # A title written while the summary was being made wins.
                    s["title"] = title

            self.store.mutate(sid, apply)
        except (NotFound, OSError):
            pass
        finally:
            with self._lock:
                self._summarizing.discard(sid)

    # ---- Startup and shutdown ----
    def _recover(self) -> None:
        """Close sessions a crash left as "recording", and queue what was left unprocessed."""
        for item in self.store.list():
            sid = item["id"]
            if item["status"] == "recording":
                self._close(sid, item["duration"])
                log.info("recovered interrupted session %s", sid)
            self._queue_rest(sid)

    def _close(self, sid: str, seconds: float) -> None:
        """End a session whose recording did not end cleanly, going by what its file holds.

        seconds is the length to take if the file cannot tell. What is left is still to be queued (_queue_rest).
        """
        try:
            seconds = sf.info(str(self.store.audio_path(sid))).frames / SAMPLE_RATE
        except Exception:  # noqa: BLE001
            pass

        def apply(s: dict) -> None:
            if s["live"]:  # As in stop(): what was recorded after the last cut becomes the last segment.
                last = max((seg["end"] for seg in s["segments"]), default=0.0)
                s["segments"].append(new_segment(last, max(last, seconds)))
            s.update(status="done", duration=round(seconds, 2))

        self.store.mutate(sid, apply)

    def _queue_rest(self, sid: str) -> None:
        """Queue the segments of a session that are still to be transcribed.

        An ended session without segments was interrupted before its recording was cut (one not
        transcribed while recording, or a re-transcription): it is cut now.
        """
        session = self.store.get(sid)
        if not session["segments"]:
            self._prepare(sid)
        for seg in session["segments"]:
            if seg["status"] in ("queued", "working"):
                self.transcriber.submit(sid, seg["id"])

    def shutdown(self) -> None:
        """Stop recording and the transcription thread."""
        try:
            self.stop()
        except UserError:
            pass  # stop() has logged it and ended the session; nobody is left to read the message.
        finally:
            self.transcriber.stop()
