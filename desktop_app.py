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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_SCRIPT = os.path.join(BASE_DIR, "backend", "app.py")
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.env")
BACKEND_URL = "http://localhost:5000"

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


def start_backend() -> subprocess.Popen:
    """Launch the Flask backend as a child process."""
    global _backend_proc

    env = os.environ.copy()
    env.update(_load_credentials())

    # When packaged with PyInstaller the real Python interpreter is embedded;
    # sys.executable points to the .exe wrapper, so we use it to spawn the
    # backend script directly – Python will handle it via the bundled runtime.
    _backend_proc = subprocess.Popen(
        [sys.executable, BACKEND_SCRIPT],
        cwd=os.path.join(BASE_DIR, "backend"),
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
    """Terminate the backend process if it is still running."""
    if _backend_proc and _backend_proc.poll() is None:
        _backend_proc.terminate()
        try:
            _backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _backend_proc.kill()


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
        if proc_died:
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
