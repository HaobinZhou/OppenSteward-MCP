import json
from dataclasses import asdict, replace

import pytest

from oppenproject.catalog import Catalog
from oppenproject.config import Settings, runtime_environment
from oppenproject.server import create_app, create_mcp


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for key in os.environ:
        if key.startswith("OPPEN_") or key == "CONTROL_PLANE_API_KEY":
            monkeypatch.delenv(key)


def test_fresh_default_and_legacy_http(tmp_path):
    config = tmp_path / "config.local.json"
    assert Settings.load(config).transport == "stdio"
    assert Settings.load(config).state_dir == tmp_path / ".runtime"
    config.write_text(json.dumps({"public_url": "https://projects.example.com"}), encoding="utf-8")
    assert Settings.load(config).transport == "http"
    assert not (tmp_path / ".runtime").exists()


def test_env_precedence_paths_and_no_shell_interpolation(tmp_path, monkeypatch):
    config = tmp_path / "config.local.json"
    config.write_text('{"port": 8880}', encoding="utf-8")
    (tmp_path / ".env").write_text(
        'OPPEN_PORT=8881\nOPPEN_TRANSPORT=stdio\nOPPEN_SCAN_ROOTS=["./项目"]\n'
        'OPPEN_EXCLUDE_ROOTS=["./项目/private"]\nOPPEN_STATE_DIR=state\nOPPEN_SKILL_ROOT=skills\n'
        "CONTROL_PLANE_API_KEY='fixture-${HOME}-$(echo literal)'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPPEN_PORT", "8882")
    result = Settings.load(config)
    assert result.port == 8882 and result.transport == "stdio"
    assert result.scan_roots == [str(tmp_path / "项目")]
    assert result.exclude_roots == [str(tmp_path / "项目/private")]
    assert result.state_dir == tmp_path / "state" and result.skill_root == tmp_path / "skills"
    assert runtime_environment(tmp_path)["CONTROL_PLANE_API_KEY"] == "fixture-${HOME}-$(echo literal)"
    assert "fixture-" not in json.dumps(asdict(result), default=str)


@pytest.mark.parametrize(
    "setting,value",
    [
        ("PORT", "garbage"),
        ("PORT", "80"),
        ("TRANSPORT", "noauth-http"),
        ("HOST", "0.0.0.0"),
        ("PUBLIC_URL", "http://public.example.com"),
        ("PUBLIC_URL", "https://example.com/mcp"),
        ("SCAN_ROOTS", "[]"),
        ("SCAN_ROOTS", "[1]"),
        ("SCAN_ROOTS", '"/tmp"'),
        ("STATE_DIR", ""),
        ("TUNNEL_PROFILE", "../outside"),
    ],
)
def test_invalid_environment_is_rejected(tmp_path, monkeypatch, setting, value):
    monkeypatch.setenv("OPPEN_" + setting, value)
    with pytest.raises(ValueError):
        Settings.load(tmp_path / "config.local.json")


def test_http_cannot_start_without_auth_and_stdio_cannot_mount_http(tmp_path):
    stdio = Settings.load(tmp_path / "config.local.json")
    with pytest.raises(ValueError):
        create_app(stdio)
    http = replace(stdio, transport="http")
    with pytest.raises(ValueError):
        create_mcp(http, Catalog(http))


def test_missing_skills_do_not_prevent_project_discovery(tmp_path):
    settings = Settings(skill_root=tmp_path)
    with pytest.raises(ValueError, match="not installed"):
        settings.skill_guide("oppen-project-steward")
    with pytest.raises(ValueError, match="Unknown skill"):
        settings.skill_guide("../../.env")
