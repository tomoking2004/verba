"""Entry point of Verba (recording, transcription, summaries).

Starts the local API server and opens its UI in a native window (WebView2).
Launched with pythonw.exe there is no console (the shortcuts do so).
build.bat turns it into a standalone exe, which runs on a PC without Python and can be carried on a USB drive.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import http.client
import json
import logging
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler

sys.dont_write_bytecode = True  # Running from source must not leave __pycache__ in the repository.
from verba.config import APP_NAME, LOG_PATH, PORT, WEBVIEW_DIR  # noqa: E402

log = logging.getLogger("app")


def setup_logging() -> None:
    """Log to app.log in the data folder, uncaught exceptions included (those of threads too)."""
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    sys.excepthook = lambda *exc: log.critical("uncaught", exc_info=exc)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:  # A thread that exits on purpose, as the default hook treats it.
            return
        name = args.thread.name if args.thread else "?"
        log.critical("uncaught in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook


def alert(message: str) -> None:
    """Show a message box (used before there is a window)."""
    ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)


GOLDEN = (1 + 5 ** 0.5) / 2  # Width-to-height ratio of the window
# Default and minimum width (logical pixels). The height is the width over the golden ratio.
WIDTH, MIN_WIDTH = 1200, 880


def work_area() -> tuple[float, float]:
    """Return the size of the screen minus the taskbar, in logical pixels.

    The value is the same whether or not the process is DPI-aware.
    """
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    hdc = ctypes.windll.user32.GetDC(None)
    dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88) or 96  # LOGPIXELSX
    ctypes.windll.user32.ReleaseDC(None, hdc)
    return (rect.right - rect.left) * 96 / dpi, (rect.bottom - rect.top) * 96 / dpi


def window_size() -> tuple[int, int]:
    """Return a golden-ratio size that fits in 90% of the work area, shrinking down to the minimum on a small screen."""
    area_w, area_h = work_area()
    width = WIDTH
    if area_w and area_h:  # If the work area is unknown, keep the default width.
        width = max(MIN_WIDTH, min(width, area_w * .9, area_h * .9 * GOLDEN))
    return round(width), round(width / GOLDEN)


# Colours of the title bar and the window border (a COLORREF is 0x00BBGGRR). They match
# --side (background) and --ink-2 (text) in ui/style.css.
CAPTION_COLOR, CAPTION_TEXT = 0x00100D0D, 0x00ACB7BD
BACKGROUND = "#131316"  # What the window shows until the page paints: --bg in ui/style.css
CLOSE_WAIT = 3  # Seconds the closing waits for the page to save its edits and ask to quit


def darken_title_bar(window) -> None:
    """Make the title bar the same black as the UI.

    Windows 11 takes the exact colour; Windows 10 gets a dark title bar.
    """
    try:
        hwnd = ctypes.wintypes.HWND(window.native.Handle.ToInt64())
        # 20: DWMWA_USE_IMMERSIVE_DARK_MODE, 34: BORDER_COLOR, 35: CAPTION_COLOR, 36: TEXT_COLOR
        for attribute, value in ((20, 1), (34, CAPTION_COLOR), (35, CAPTION_COLOR), (36, CAPTION_TEXT)):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(ctypes.c_int(value)), 4)
    except Exception:  # noqa: BLE001  Purely cosmetic, so a failure does not stop the launch.
        log.exception("cannot darken the title bar")


def port_in_use() -> bool:
    """Whether some program is already serving on the port."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def port_held_by_app() -> bool:
    """Whether the program on the port is another instance of this app: it answers /api/config in the app's shape."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
    try:
        conn.request("GET", "/api/config")
        res = conn.getresponse()
        data = json.loads(res.read(1_000_000).decode("utf-8"))
        return res.status == 200 and isinstance(data, dict) and {"settings", "models", "sessions_dir"} <= data.keys()
    except (OSError, http.client.HTTPException, ValueError):  # No answer in time, or not this app's answer.
        return False
    finally:
        conn.close()


PORT_TAKEN = f"{APP_NAME} を起動できませんでした。ほかのアプリが、{APP_NAME} の使うポートを使っています。"


def port_message() -> str:
    """Return what to tell the user when the port is taken (the port number goes to the log only)."""
    if port_held_by_app():
        log.info("port %s is held by another instance", PORT)
        return f"{APP_NAME} はすでに起動しています。タスクバーのウィンドウを開いてください。"
    log.warning("port %s is held by another program", PORT)
    return PORT_TAKEN


def main() -> None:
    """Run the app until its window is closed."""
    setup_logging()
    if port_in_use():
        alert(port_message())
        return

    from verba.controller import Controller
    from verba.server import make_server

    controller = Controller()
    window = None
    quitting = threading.Event()
    quit_lock = threading.Lock()

    def quit_app() -> None:
        """Stop recording, then close the window.

        The page asks for it once its edits are saved; ask_page_to_quit does, if the page does not answer.
        """
        with quit_lock:
            if quitting.is_set():
                return
            quitting.set()
        try:
            controller.stop()
        except Exception:  # noqa: BLE001  stop() has ended the session; the app must still close.
            log.exception("the recording did not stop cleanly")
        finally:
            if window is not None:
                threading.Thread(target=window.destroy, daemon=True).start()

    try:
        server = make_server(controller, PORT, on_quit=quit_app)
    except OSError:  # Nothing answers on the port, yet it cannot be bound (reserved by Windows, for one).
        log.exception("cannot bind port %s", PORT)
        controller.shutdown()
        alert(PORT_TAKEN)
        return
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    log.info("serving %s", url)

    try:
        import webview
    except ImportError:
        import webbrowser
        webbrowser.open(url)
        threading.Event().wait()
        return

    def ask_page_to_quit() -> None:
        """Hand the closing to the page, which saves an edit in progress and then calls /api/quit.

        While recording the page asks first, so the recording is not stopped by accident. Otherwise a page
        that cannot answer (not loaded, or stuck) does not keep the window open.
        """
        recording = controller.state()["recording"]
        function = "confirmQuit" if recording else "saveAndQuit"
        try:
            asked = window.evaluate_js(f"window.{function} ? (void {function}(), true) : false")
        except Exception:  # noqa: BLE001  Whatever went wrong, the app must still close.
            log.exception("cannot ask the page to quit")
            asked = False
        if not recording and not (asked and quitting.wait(CLOSE_WAIT)):
            log.warning("the page did not answer the closing: quitting without it")
            quit_app()

    def on_closing() -> bool:
        # The first request is held back and passed to the page; the window closes when quit_app destroys it.
        if quitting.is_set():
            return True
        threading.Thread(target=ask_page_to_quit, name="closing", daemon=True).start()
        return False

    width, height = window_size()
    window = webview.create_window(APP_NAME, url, width=width, height=height,
                                   min_size=(MIN_WIDTH, round(MIN_WIDTH / GOLDEN)), background_color=BACKGROUND)
    window.events.closing += on_closing
    window.events.before_show += lambda: darken_title_bar(window)
    try:
        # The web view's storage goes in the data folder too; nothing is written on the PC (%APPDATA%).
        webview.start(private_mode=False, storage_path=str(WEBVIEW_DIR))
    finally:
        log.info("shutting down")
        controller.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
