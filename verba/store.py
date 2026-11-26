"""Storage of sessions.

One recording is one folder, sessions/<date_time>/:
  session.json    the source of truth (segments, utterances, summary)
  transcript.md   for people to read (rebuilt from session.json on every save)
  audio.ogg       the recording (stereo Opus; left = me, right = the other side)

In session.json, a session's status is "recording" or "done". live tells whether it was transcribed
while it was recorded; if not, it has no segments until the recording ends and its file is cut.
A segment starts as "queued", is "working" while Gemini transcribes it, and ends as "done",
"silent" (nothing was said, so it was not sent), "error" or "stopped" (the user called the
transcription off). A retry puts the last two back to "queued". model is the id of the model that
transcribed a segment.
transcribed_at is when the transcript was completed, and names_error is the reason proper
nouns could not be fixed.
A re-transcription keeps the transcript it replaces (segments, utterances, transcribed_at,
names_error), edits and all, as a version to go back to. The transcript at the top level is the
one shown, which is the one edited; versions holds the others in the order they were made, and
version is the place of the shown one among all of them: the full order is versions[:version],
the shown one, versions[version:]. A re-transcription adds its transcript at the end and shows it.
redo_from is the place of the version shown when a re-transcription started, until it completes:
calling it off meanwhile shows that version again.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import threading
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import SESSIONS_DIR
from .errors import UserError
from .files import patiently, write_text

log = logging.getLogger(__name__)

AUDIO_NAME = "audio.ogg"
UNTITLED = "無題の記録"
TRANSCRIPT_KEYS = ("segments", "utterances", "transcribed_at", "names_error")  # What each version holds
HIT_LEAD, HIT_TAIL = 6, 40  # Characters shown before and after a search hit
_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}(-[0-9]+)?$")


class NotFound(UserError):
    """There is no such session. Its folder may have been removed from outside, which is a supported way to delete one."""

    def __init__(self) -> None:
        super().__init__("記録が見つかりません。記録のフォルダが削除されたか、移されています")


def clock(seconds: float) -> str:
    """Format seconds as MM:SS, or H:MM:SS from one hour on."""
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def timestamp() -> str:
    """Return the local time now, in the form stored in session.json."""
    return datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    """Return a fresh id for a segment or an utterance."""
    return uuid.uuid4().hex[:10]


def new_segment(start: float, end: float) -> dict:
    """Return a segment that is yet to be transcribed."""
    return {"id": new_id(), "start": round(start, 2), "end": round(end, 2), "status": "queued", "error": None,
            "model": None}


class Store:
    """The sessions on disk, cached in memory. Callers get copies; changes go through mutate()."""

    def __init__(self, root: Path = SESSIONS_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self._unreadable: set[str] = set()  # Folders already reported as unreadable (the list is read again and again)
        for path in self.root.iterdir():  # Empty folders a previous delete() could not remove
            if _ID_RE.match(path.name) and path.is_dir():
                try:
                    path.rmdir()  # Fails, and leaves the folder, unless it is empty.
                except OSError:
                    pass

    # ---- Reading ----
    def folder(self, sid: str) -> Path:
        """Return the folder of a session. Raise NotFound unless sid has the form of a session id."""
        if not _ID_RE.match(sid or ""):
            raise NotFound
        return self.root / sid

    def audio_path(self, sid: str) -> Path:
        """Return the path of a session's recording."""
        return self.folder(sid) / AUDIO_NAME

    def get(self, sid: str) -> dict:
        """Return a copy of a session. Raise NotFound if there is none."""
        with self._lock:
            return copy.deepcopy(self._load(sid))

    def list(self, query: str = "") -> list[dict]:
        """Return the sessions, newest first. With a query, only those whose title or utterances contain it.

        A session matched by an utterance carries its first hit: the start (in seconds) of the
        utterance, and the text around the match.
        """
        needle = _fold(query.strip())
        out = []
        with self._lock:
            for path in sorted(self.root.iterdir(), reverse=True):
                if not (path / "session.json").exists():
                    continue
                try:
                    s = self._load(path.name)
                    item = {
                        "id": s["id"], "title": s["title"], "created_at": s["created_at"],
                        "duration": s["duration"], "status": s["status"],
                        "failed": sum(1 for seg in s["segments"] if seg["status"] == "error"),
                    }
                    if needle and needle not in _fold(s["title"] or UNTITLED):
                        hit = next(filter(None, (_find(u, needle) for u in s["utterances"])), None)
                        if hit is None:
                            continue
                        item["hit"] = hit
                except (NotFound, OSError, ValueError, KeyError, TypeError):  # Not a session, or one edited out of shape
                    if path.name not in self._unreadable:
                        self._unreadable.add(path.name)
                        log.warning("skip unreadable session %s", path.name)
                    continue
                out.append(item)
        return out

    # ---- Writing ----
    def create(self, title: str, memo: str, echo_removal: bool, live: bool) -> dict:
        """Create a session that is being recorded.

        echo_removal tells whether the other side is heard through speakers. A later
        re-transcription removes echo according to how the session was recorded.
        live tells whether it is transcribed while it is recorded.
        """
        with self._lock:
            now = datetime.now()
            sid = now.strftime("%Y-%m-%d_%H%M%S")
            n = 1
            while (self.root / sid).exists():
                n += 1
                sid = now.strftime("%Y-%m-%d_%H%M%S") + f"-{n}"
            (self.root / sid).mkdir(parents=True)
            session = {
                "id": sid, "title": title.strip(), "memo": memo.strip(),
                "created_at": now.isoformat(timespec="seconds"), "status": "recording",
                "duration": 0.0, "echo_removal": echo_removal, "live": live, "rev": 0,
                "segments": [], "utterances": [], "transcribed_at": None, "names_error": None,
                "versions": [], "version": 0, "redo_from": None, "summary": None,
            }
            self._cache[sid] = session
            self._save(session)
            return copy.deepcopy(session)

    def mutate(self, sid: str, change: Callable[[dict], object]):
        """Change a session under the lock and save it. Return a copy of what change returned."""
        with self._lock:
            session = self._load(sid)
            result = change(session)
            session["rev"] += 1
            self._save(session)
            return copy.deepcopy(result)

    def delete(self, sid: str) -> None:
        """Delete a session: the recording and the rest first, session.json last.

        If something cannot be deleted on the way, the session stays in the list.
        The emptied folder may be held by a sync client or Explorer. The session is gone
        by then, so that is not a failure; the folder is removed on the next start.
        """
        with self._lock:
            folder = self.folder(sid)
            self._cache.pop(sid, None)
            if not folder.is_dir():  # Already removed from outside: there is nothing left to delete.
                return
            try:
                for path in sorted(folder.iterdir(), key=lambda p: p.name == "session.json"):
                    patiently(lambda p=path: shutil.rmtree(p) if p.is_dir() else p.unlink(), attempts=8)
            except OSError as exc:
                log.exception("cannot delete %s", sid)
                raise UserError("削除できませんでした。ほかのアプリが記録のファイルを使っています") from exc
            try:
                folder.rmdir()
            except OSError:
                log.info("empty folder of %s is in use; it will be removed on next start", sid)

    # ---- Internals ----
    def _load(self, sid: str) -> dict:
        folder = self.folder(sid)
        if not folder.is_dir():
            self._cache.pop(sid, None)
            raise NotFound
        if sid not in self._cache:
            try:
                self._cache[sid] = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise NotFound from None
        return self._cache[sid]

    def _save(self, session: dict) -> None:
        """Write the session out. A failure is not raised: the next save writes what is in memory."""
        folder = self.folder(session["id"])
        try:
            write_text(folder / "session.json", json.dumps(session, ensure_ascii=False, indent=1))
            write_text(folder / "transcript.md", to_markdown(session))
        except OSError:
            log.exception("cannot save session %s (will retry on next change)", session["id"])


def versions_of(session: dict) -> list[dict]:
    """Return every version of the transcript, the shown one included, in the order they were made."""
    v = session["version"]
    shown = {key: session[key] for key in TRANSCRIPT_KEYS}
    return session["versions"][:v] + [shown] + session["versions"][v:]


def show_version(session: dict, versions: list[dict], index: int) -> None:
    """Make versions (every version, in order) the session's, showing the one at index."""
    rest = list(versions)
    session.update(rest.pop(index), versions=rest, version=index)


def _fold(text: str) -> str:
    """Return the form compared in a search: full/half width and upper/lower case are not distinguished."""
    return unicodedata.normalize("NFKC", text).lower()


def _find(utterance: dict, needle: str) -> dict | None:
    """Return where needle occurs in the utterance's text, with the text around it, or None."""
    text = utterance["text"]
    folded = _fold(text)
    at = folded.find(needle)
    if at < 0:
        return None
    if len(folded) != len(text):  # Folding changed the length, so positions are off: show the folded text.
        text = folded
    end = at + len(needle)
    return {"start": utterance["start"], "before": ("…" if at > HIT_LEAD else "") + text[max(0, at - HIT_LEAD):at],
            "match": text[at:end], "after": text[end:end + HIT_TAIL]}


def to_markdown(session: dict) -> str:
    """Render a session as transcript.md."""
    created = datetime.fromisoformat(session["created_at"])
    lines = [f"# {session['title'] or UNTITLED}", ""]
    lines.append(f"- 日時: {created:%Y-%m-%d %H:%M}")
    lines.append(f"- 長さ: {clock(session['duration'])}")
    lines.append("")
    if session.get("memo"):  # The summary and the transcript are built on the memo, so it comes before them.
        lines += ["## メモ", "", session["memo"], ""]
    if session.get("summary") and session["summary"]["text"]:  # A summary that only failed so far has no text.
        lines += ["## 要約", "", _demote(session["summary"]["text"].strip()), ""]
    lines += ["## 文字起こし", ""]
    for u in session["utterances"]:
        lines.append(f"**[{clock(u['start'])}] {u['speaker']}**　{u['text']}")
        lines.append("")
    pending = [seg for seg in session["segments"] if seg["status"] in ("queued", "working", "error", "stopped")]
    if pending:
        lines += [f"> 未完了の区間が {len(pending)} 件あります（アプリから文字起こしを続けられます）。", ""]
    return "\n".join(lines).rstrip() + "\n"


def _demote(markdown: str) -> str:
    """Push the headings of a summary one level down, to fit the outline of the whole transcript."""
    return re.sub(r"^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", markdown, flags=re.M)
