"""The core of the app: drives recording, transcription and storage together."""

from __future__ import annotations

import logging
import threading

import soundfile as sf

from .audio import Capture, list_devices, split
from .config import SAMPLE_RATE, SPEAKER_ME, SPEAKER_OTHER, USAGE_PATH, Settings, load_api_key, save_api_key
from .errors import UserError
from .gemini import Gemini, GeminiError, Usage
from .store import NotFound, Store, clock, new_segment, timestamp
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
        self._preparing: set[str] = set()  # Sessions whose recording is being cut anew for a re-transcription
        self._recover()

    # ---- Settings ----
    def set_api_key(self, key: str) -> dict:
        """Check the API key, then save it. It is saved even when the check cannot reach Google."""
        key = key.strip()
        if not key:
            raise UserError("API キーを入力してください")
        ok = self.gemini.check_key(key)
        if ok is False:
            raise UserError("この API キーは使えません。コピーし間違いがないか確かめてください")
        save_api_key(key)
        return {"checked": ok is True}

    # ---- Recording ----
    def start(self, title: str = "", memo: str = "") -> dict:
        """Create a session and start recording into it. Return the session."""
        with self._lock:
            if self._capture is not None:
                raise UserError("すでに録音中です")
            if not self.gemini.available:
                raise UserError("Gemini の API キーが未設定です。設定から入力してください")
            session = self.store.create(title, memo, self.settings.get("echo_removal"))
            sid = session["id"]
            try:
                capture = Capture(self.store.audio_path(sid), self.settings.get("mic"),
                                  self.settings.get("speaker"), self.settings.get("segment_seconds"),
                                  on_segment=lambda start, end, mic, system: self._on_segment(sid, start, end, mic, system))
                capture.start()
            except Exception as exc:
                log.exception("cannot start capture")
                self.store.delete(sid)
                raise UserError("録音を開始できませんでした。「自分の声」と「相手の声」の選択を確かめてください") from exc
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

    def stop(self) -> None:
        """Stop recording, if there is one. The session is ended even if the recording cannot be closed."""
        with self._lock:
            if self._capture is None:
                return
            capture, sid = self._capture, self._recording_sid
            self._capture, self._recording_sid = None, None
        try:
            start, end, mic, system = capture.stop()
        except Exception as exc:  # The last write failed (a full disk, for example).
            log.exception("cannot close the recording of %s", sid)
            try:
                self.transcriber.submit(sid, self._close(sid, capture.seconds))
            except Exception:  # noqa: BLE001  The same cause may stop this save too; startup recovers the session.
                log.exception("cannot end the session %s", sid)
            raise UserError("録音を最後まで保存できませんでした。保存先の空き容量を確かめてください") from exc
        # The remainder becomes the last segment, in the same save that ends the session: the transcriber
        # completes the transcript on the last segment of an ended session, so there must always be one.
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
        """Return what the UI polls: the live recording, and whether a key is set."""
        with self._lock:
            capture, sid = self._capture, self._recording_sid
        rec = None
        if capture is not None:
            rec = {"session": sid, "seconds": capture.seconds, "paused": capture.paused,
                   "levels": capture.levels(), "devices": capture.device_names}
        return {"recording": rec, "has_key": self.gemini.available}

    def session(self, sid: str) -> dict:
        """Return a session together with the work in progress on it."""
        s = self.store.get(sid)
        s["pending"] = self.transcriber.pending(sid)
        s["summarizing"] = sid in self._summarizing
        s["fixing_names"] = sid in self._fixing_names
        return s

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
                raise UserError("録音中の記録は削除できません")
        self.store.delete(sid)

    def retry(self, sid: str) -> int:
        """Put the failed segments back in the queue. Return how many."""
        failed = self.store.mutate(sid, lambda s: [
            seg.update(status="queued", error=None) or seg["id"] for seg in s["segments"] if seg["status"] == "error"
        ])
        for seg_id in failed:
            self.transcriber.submit(sid, seg_id)
        return len(failed)

    def retranscribe(self, sid: str) -> None:
        """Transcribe the whole recording again with the current title, memo and segment length.

        The segments are cut anew from the recording. The old utterances stay until the result
        of each new segment replaces them.
        """
        with self._lock:
            if sid == self._recording_sid:
                raise UserError("録音の停止後に作り直せます")
            # Cutting a long recording takes seconds, during which nothing is queued yet: a second
            # request in that time is turned away like one made while segments are being transcribed.
            if self.transcriber.pending(sid) or sid in self._fixing_names or sid in self._preparing:
                raise UserError("文字起こしの途中です")
            self.store.get(sid)  # Raises if there is no such session.
            self._preparing.add(sid)
        try:
            try:
                spans = split(self.store.audio_path(sid), self.settings.get("segment_seconds"))
            except Exception as exc:
                log.exception("cannot split audio of %s", sid)
                raise UserError("作り直せませんでした。録音のファイルを読めません") from exc

            def apply(s: dict) -> list[str]:
                s["segments"] = [new_segment(start, end) for start, end in spans]
                for u in s["utterances"]:  # Keep each old utterance under the new segment that covers its time.
                    owner = next((seg for seg in s["segments"] if u["start"] < seg["end"]), None)
                    u["segment"] = owner and owner["id"]
                s["utterances"] = [u for u in s["utterances"] if u["segment"]]
                s["names_error"] = None
                return [seg["id"] for seg in s["segments"]]

            for seg_id in self.store.mutate(sid, apply):
                self.transcriber.submit(sid, seg_id)
        finally:
            with self._lock:
                self._preparing.discard(sid)

    def fix_names(self, sid: str) -> None:
        """Redo, in the background, only the proper-noun fix that was skipped (the transcript is not redone)."""
        with self._lock:
            if sid in self._fixing_names:
                return
            if sid == self._recording_sid or self.transcriber.pending(sid) or sid in self._preparing:
                raise UserError("文字起こしの途中です")
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
                raise UserError("文字起こしがまだありません")
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
        """Close sessions a crash left as "recording", and queue the segments left unprocessed."""
        for item in self.store.list():
            sid = item["id"]
            if item["status"] == "recording":
                self._close(sid, item["duration"])
                log.info("recovered interrupted session %s", sid)
            for seg in self.store.get(sid)["segments"]:
                if seg["status"] in ("queued", "working"):
                    self.transcriber.submit(sid, seg["id"])

    def _close(self, sid: str, seconds: float) -> str:
        """End a session whose recording did not end cleanly, going by what its file holds.

        seconds is the length to take if the file cannot tell. Return the id of the last segment,
        which is still to be queued.
        """
        try:
            seconds = sf.info(str(self.store.audio_path(sid))).frames / SAMPLE_RATE
        except Exception:  # noqa: BLE001
            pass

        def apply(s: dict) -> str:
            # As in stop(): what was recorded after the last cut becomes the last segment.
            last = max((seg["end"] for seg in s["segments"]), default=0.0)
            seg = new_segment(last, max(last, seconds))
            s["segments"].append(seg)
            s.update(status="done", duration=round(seconds, 2))
            return seg["id"]

        return self.store.mutate(sid, apply)

    def shutdown(self) -> None:
        """Stop recording and the transcription thread."""
        try:
            self.stop()
        except UserError:
            pass  # stop() has logged it and ended the session; nobody is left to read the message.
        finally:
            self.transcriber.stop()
