"""
desktop_app.py – Windows desktop launcher for the BloFin Trading Bot.

Usage
─────
    python desktop_app.py

What it does
────────────
1. Reads credentials.env from the repo root (or the directory next to this
   script when packaged as an .exe) and injects the variables into the
   environment before starting the Flask backend.
2. Spawns the Flask backend (backend/app.py) as a child process.
3. Waits for the backend to become ready (up to 30 s).
4. Opens a native desktop window via pywebview that displays the full web UI
   at http://localhost:5000.  All existing buttons (Start Bot, Stop Bot,
   Clear Log) work exactly as in a browser.
5. When the window is closed the backend process is terminated automatically.

Packaging as a standalone .exe
───────────────────────────────
    Run build_exe.bat  (requires PyInstaller)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import requests
import webview

# ── Paths ─────────────────────────────────────────────────────────────────────

# APP_DIR points to bundled app resources:
# - source run: repository root (this file's folder)
# - PyInstaller onefile: temporary extraction folder (_MEIPASS)
APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# Ensure bundled resources are importable when running as a frozen executable.
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# USER_DIR points to where the launcher lives:
# - source run: repository root
# - PyInstaller exe: directory containing the .exe
USER_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))

BACKEND_SCRIPT = os.path.join(APP_DIR, "backend", "app.py")
CREDENTIALS_FILE = os.path.join(USER_DIR, "credentials.env")
BACKEND_URL = "http://localhost:5000"


def _resolve_python() -> str:
    """Return the best Python interpreter to spawn the backend with.

    Priority:
    1. The project's own .venv (works when running from source).
    2. sys.executable (fallback for non-standard local setups).
    """
    if not getattr(sys, "frozen", False):
        venv_python = os.path.join(APP_DIR, ".venv", "Scripts", "python.exe")
        if os.path.isfile(venv_python):
            return venv_python
    return sys.executable

LOADING_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100vh;
      gap: 20px;
    }
    .logo { font-size: 2rem; }
    h2 { font-weight: 400; font-size: 1.2rem; color: #8b949e; }
    .spinner {
      width: 40px; height: 40px;
      border: 3px solid #30363d;
      border-top-color: #238636;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="logo">🪙</div>
  <h2>Starting BloFin Trading Bot…</h2>
  <div class="spinner"></div>
</body>
</html>
"""

ERROR_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100vh;
      gap: 16px;
      text-align: center;
      padding: 40px;
    }}
    .icon {{ font-size: 2rem; }}
    h2 {{ color: #f85149; font-weight: 500; }}
    p {{ color: #8b949e; max-width: 480px; line-height: 1.6; }}
    code {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 4px;
      padding: 2px 6px;
      font-size: 0.85rem;
      color: #79c0ff;
    }}
  </style>
</head>
<body>
  <div class="icon">⚠️</div>
  <h2>Backend failed to start</h2>
  <p>{msg}</p>
  <p>Make sure <code>credentials.env</code> exists in the app folder and all
  dependencies are installed (<code>pip install -r backend/requirements.txt</code>).</p>
</body>
</html>
"""

# ── Backend management ────────────────────────────────────────────────────────

_backend_proc: subprocess.Popen | None = None
_backend_thread: threading.Thread | None = None
_backend_error: str | None = None


def _load_credentials() -> dict[str, str]:
    """Parse credentials.env into a dict (KEY=VALUE pairs, comments ignored)."""
    env: dict[str, str] = {}
    if not os.path.exists(CREDENTIALS_FILE):
        return env
    with open(CREDENTIALS_FILE, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


def _run_embedded_backend() -> None:
    """Run the backend inside this process (used in frozen executable mode)."""
    global _backend_error
    try:
        backend_dir = os.path.join(APP_DIR, "backend")
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)

        from backend import app as backend_app_module
        from backend import database as db

        db.init_db()
        backend_app_module.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
    except Exception as exc:  # pragma: no cover - only exercised in packaged app runtime
        _backend_error = str(exc)


def _is_backend_running() -> bool:
    """Return True if a backend is already responding on port 5000."""
    try:
        r = requests.get(f"{BACKEND_URL}/api/status", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def start_backend() -> subprocess.Popen | None:
    """Launch the Flask backend (thread in frozen mode, subprocess in source mode).

    If a backend is already listening on port 5000 it is reused – no new
    process is spawned.  This prevents duplicate instances when the desktop
    app is opened more than once.
    """
    global _backend_proc, _backend_thread

    if _is_backend_running():
        print("INFO: Backend already running on port 5000 – reusing existing instance.", flush=True)
        return None

    credentials = _load_credentials()
    env = os.environ.copy()
    env.update(credentials)

    if getattr(sys, "frozen", False):
        os.environ.update(credentials)
        _backend_thread = threading.Thread(target=_run_embedded_backend, daemon=True)
        _backend_thread.start()
        return None

    _backend_proc = subprocess.Popen(
        [_resolve_python(), BACKEND_SCRIPT],
        cwd=os.path.join(APP_DIR, "backend"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,  # hide console window on Windows
    )
    return _backend_proc


def wait_for_backend(timeout: int = 30) -> bool:
    """Poll /api/status until the server responds or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BACKEND_URL}/api/status", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def stop_backend() -> None:
    """Terminate the backend subprocess if it is still running."""
    if _backend_proc and _backend_proc.poll() is None:
        _backend_proc.terminate()
        try:
            _backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _backend_proc.kill()

    # Belt-and-suspenders: kill any lingering process still bound to port 5000.
    # On Windows, pywebview.start() occasionally returns without the subprocess
    # having been fully shut down, leaving a zombie Flask server on the port.
    try:
        import subprocess as _sp
        result = _sp.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if ":5000" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid and pid != os.getpid():
                    _sp.run(["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True, timeout=5)
    except Exception:
        pass


# ── Desktop window ────────────────────────────────────────────────────────────

def _boot_and_load(window: webview.Window) -> None:
    """
    Run in a background thread:
    1. Start the Flask backend.
    2. Wait for it to be ready.
    3. Navigate the window to the app or show an error page.
    """
    start_backend()

    if wait_for_backend():
        window.load_url(BACKEND_URL)
    else:
        proc_died = _backend_proc and _backend_proc.poll() is not None
        if _backend_error:
            msg = f"The backend crashed during startup: {_backend_error}"
        elif proc_died:
            msg = (
                "The backend process exited unexpectedly. "
                "Check that all Python dependencies are installed."
            )
        else:
            msg = "The backend did not respond within 30 seconds."
        window.load_html(ERROR_HTML.format(msg=msg))


def main() -> None:
    window = webview.create_window(
        title="BloFin Trading Bot",
        html=LOADING_HTML,
        width=1280,
        height=860,
        min_size=(900, 620),
        background_color="#0d1117",
    )

    threading.Thread(target=_boot_and_load, args=(window,), daemon=True).start()

    webview.start(debug=False)

    # Window has been closed – clean up the backend.
    stop_backend()


if __name__ == "__main__":
    main()
