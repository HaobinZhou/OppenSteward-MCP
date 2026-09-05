"""Thin launcher for OpenAI's official tunnel-client; never implement a second tunnel."""

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ROOT, Settings, runtime_environment


def tunnel_arguments(settings: Settings, action: str, config_path: Path) -> list[str]:
    if settings.transport != "stdio":
        raise ValueError(
            "Tunnel launcher requires OPPEN_TRANSPORT=stdio; existing HTTP/OAuth remains separate"
        )
    if action not in {"init", "doctor", "run"}:
        raise ValueError("Unknown tunnel action")
    args = [settings.tunnel_client, action]
    if action == "init":
        if not re.fullmatch(r"tunnel_[0-9a-f]{32}", settings.tunnel_id):
            raise ValueError("Set OPPEN_TUNNEL_ID to the tunnel ID from OpenAI Platform")
        # tunnel-client parses shell-style quotes itself on every OS, then uses
        # exec.Command(argv...). No shell runs this string, including on Windows.
        command = shlex.join(
            [
                sys.executable,
                str(ROOT / "run.py"),
                "--config",
                str(config_path),
                "serve",
                "--transport",
                "stdio",
            ]
        )
        args += [
            "--sample",
            "sample_mcp_stdio_local",
            "--tunnel-id",
            settings.tunnel_id,
            "--mcp-command",
            command,
        ]
    args += ["--profile", settings.tunnel_profile]
    if action == "doctor":
        args += ["--explain"]
    return args


def tunnel_command(settings: Settings, action: str, config_path: Path, *, dry_run=False):
    args = tunnel_arguments(settings, action, config_path)
    if dry_run:
        print(shlex.join(args))
        return
    env = runtime_environment(config_path.parent)
    if not shutil.which(settings.tunnel_client, path=env.get("PATH")):
        raise ValueError(
            "Install official tunnel-client first or set OPPEN_TUNNEL_CLIENT to its local binary"
        )
    if action in {"doctor", "run"} and not env.get("CONTROL_PLANE_API_KEY"):
        raise ValueError(
            "Set CONTROL_PLANE_API_KEY for the official tunnel-client in .env or the environment"
        )
    # Key is supplied only through the subprocess environment, never command arguments.
    result = subprocess.run(args, env=env, cwd=ROOT)
    if result.returncode:
        raise SystemExit(result.returncode)
