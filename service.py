#!/usr/bin/env python3
"""Run the MCP service under the current macOS user's launchd session."""

import argparse
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from oppenproject.config import ROOT, Settings

LABEL = "com.oppen.project-mcp"


def detached(settings, command):
    """Start in the invoking application's existing filesystem permission context."""
    state = settings.state_dir / "process.json"
    recorded = json.loads(state.read_text()) if state.exists() else {}
    pid = recorded.get("pid")
    running = False
    if pid:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
        running = result.returncode == 0 and str(ROOT / "run.py") + " serve" in result.stdout
    if command == "status":
        print(
            json.dumps(
                {
                    "running": running,
                    "pid": pid if running else None,
                    "listen": f"{settings.host}:{settings.port}",
                }
            )
        )
        return
    if command in {"stop", "restart"} and running:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            raise SystemExit("Server is still shutting down; retry after the active scan finishes")
        state.unlink(missing_ok=True)
        running = False
    if command == "stop":
        return
    if running:
        print("OppenProject is already running; use restart after changing configuration.")
        return
    with (settings.state_dir / "server.log").open("ab") as log:
        process = subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "run.py"), "serve"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    state.write_text(json.dumps({"pid": process.pid}) + "\n")
    state.chmod(0o600)
    time.sleep(1)
    if process.poll() is not None:
        state.unlink(missing_ok=True)
        raise SystemExit("Server did not start; inspect .runtime/server.log")
    print(f"OppenProject started (PID {process.pid}) at {settings.host}:{settings.port}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "install", "uninstall"])
    args = parser.parse_args()
    settings = Settings.load()
    if args.command in {"start", "stop", "restart", "status"}:
        return detached(settings, args.command)
    domain = f"gui/{os.getuid()}"
    label = f"{domain}/{LABEL}"
    target = settings.state_dir / "launchd.plist"
    installed = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    if args.command == "uninstall":
        subprocess.run(["launchctl", "bootout", label], check=False)
        installed.unlink(missing_ok=True)
        return
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), str(ROOT / "run.py"), "serve"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(settings.state_dir / "server.log"),
        "StandardErrorPath": str(settings.state_dir / "server.log"),
    }
    target.write_bytes(plistlib.dumps(payload))
    target.chmod(0o600)
    if args.command == "install":
        installed.parent.mkdir(parents=True, exist_ok=True)
        if (
            installed.exists()
            and plistlib.loads(installed.read_bytes()).get("ProgramArguments") != payload["ProgramArguments"]
        ):
            raise SystemExit("Existing launch agent points to another service; refusing to overwrite")
        installed.write_bytes(target.read_bytes())
        installed.chmod(0o600)
        target = installed
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"OppenProject started at {settings.host}:{settings.port}; MCP: {settings.resource}")


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("This service manager is macOS-only; use run.py serve on other POSIX systems")
    main()
