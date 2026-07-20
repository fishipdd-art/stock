"""
macOS launchd service installer.

Installs the Stock Analysis System as a user-level launchd service.
- Auto-starts on user login
- Restarts if it crashes
- Restarts daily at 08:30 to ensure fresh state

Usage:
  python scripts/install_launchd.py install   # install + start
  python scripts/install_launchd.py uninstall # remove + stop
  python scripts/install_launchd.py status    # check status
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PLIST_NAME = "com.user.stock-analysis.plist"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLIST_SRC = PROJECT_ROOT / "scripts" / PLIST_NAME
PLIST_DEST = LAUNCH_AGENTS / PLIST_NAME


def _ensure_python_path():
    """Auto-detect Python interpreter in the venv."""
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def install():
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    if not PLIST_SRC.exists():
        print(f"[ERR] plist not found at {PLIST_SRC}")
        return 1

    # Update python path in plist
    content = PLIST_SRC.read_text()
    py = _ensure_python_path()
    content = content.replace(
        "/Users/liyuhang/.local/bin/python3.11",
        py
    )
    PLIST_DEST.write_text(content)
    print(f"[OK] Installed plist to {PLIST_DEST} (python={py})")

    # Load with launchd
    subprocess.run(["launchctl", "load", str(PLIST_DEST)], check=False)
    print(f"[OK] Loaded into launchd")
    print(f"     Check status: python {sys.argv[0]} status")
    print(f"     Web UI:       http://localhost:8000")
    print(f"     Logs:         {PROJECT_ROOT}/data/logs/launchd.*.log")
    return 0


def uninstall():
    if PLIST_DEST.exists():
        subprocess.run(["launchctl", "unload", str(PLIST_DEST)], check=False)
        PLIST_DEST.unlink()
        print(f"[OK] Unloaded and removed {PLIST_DEST}")
    else:
        print(f"[INFO] {PLIST_DEST} not installed")
    return 0


def status():
    res = subprocess.run(
        ["launchctl", "list", "com.user.stock-analysis"],
        capture_output=True, text=True
    )
    print("launchctl list output:")
    print(res.stdout or "(empty)")
    if "com.user.stock-analysis" in res.stdout:
        print("[OK] Service is loaded")
        return 0
    print("[WARN] Service is not loaded")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: install_launchd.py {install|uninstall|status}")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "install":
        sys.exit(install())
    elif cmd == "uninstall":
        sys.exit(uninstall())
    elif cmd == "status":
        sys.exit(status())
    else:
        print(f"Unknown: {cmd}")
        sys.exit(1)