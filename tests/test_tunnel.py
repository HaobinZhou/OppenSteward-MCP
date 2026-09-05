import shlex
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from oppenproject.config import ROOT, Settings
from oppenproject.tunnel import tunnel_arguments, tunnel_command

TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"


def test_profile_command_quotes_paths_and_uses_official_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", r"C:\Users\Research Team\Python\python.exe")
    config = tmp_path / "My Team's config.json"
    settings = Settings(transport="stdio", tunnel_id=TUNNEL_ID)
    args = tunnel_arguments(settings, "init", config)
    assert args[:4] == ["tunnel-client", "init", "--sample", "sample_mcp_stdio_local"]
    assert args[args.index("--tunnel-id") + 1] == TUNNEL_ID
    child = shlex.split(args[args.index("--mcp-command") + 1])
    assert child == [
        sys.executable,
        str(ROOT / "run.py"),
        "--config",
        str(config),
        "serve",
        "--transport",
        "stdio",
    ]
    assert tunnel_arguments(settings, "doctor", config)[-1] == "--explain"


def test_key_is_environment_only_and_child_failure_propagates(tmp_path, monkeypatch, capsys):
    key = "non-secret-fixture-key"
    (tmp_path / ".env").write_text("CONTROL_PLANE_API_KEY=" + key, encoding="utf-8")
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    monkeypatch.setattr("oppenproject.tunnel.shutil.which", lambda *a, **k: "/fixture/tunnel-client")
    calls = []

    def launch(args, **kwargs):
        assert key not in str(args)
        assert kwargs["env"]["CONTROL_PLANE_API_KEY"] == key
        assert "shell" not in kwargs
        calls.append(args)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr("oppenproject.tunnel.subprocess.run", launch)
    settings = Settings(transport="stdio", tunnel_id=TUNNEL_ID)
    config = tmp_path / "config.local.json"
    tunnel_command(settings, "init", config, dry_run=True)
    assert key not in capsys.readouterr().out and not calls
    with pytest.raises(SystemExit) as error:
        tunnel_command(settings, "run", config)
    assert error.value.code == 7 and len(calls) == 1


def test_bad_profile_and_missing_client_or_key_fail_clearly(tmp_path, monkeypatch):
    settings = Settings(transport="stdio", tunnel_id="bad")
    config = tmp_path / "config.local.json"
    with pytest.raises(ValueError, match="TUNNEL_ID"):
        tunnel_arguments(settings, "init", config)
    with pytest.raises(ValueError, match="stdio"):
        tunnel_arguments(replace(settings, transport="http"), "run", config)
    monkeypatch.setattr("oppenproject.tunnel.shutil.which", lambda *a, **k: None)
    with pytest.raises(ValueError, match="Install"):
        tunnel_command(settings, "run", config)
    monkeypatch.setattr("oppenproject.tunnel.shutil.which", lambda *a, **k: str(Path("tunnel-client")))
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CONTROL_PLANE_API_KEY"):
        tunnel_command(settings, "run", config)
