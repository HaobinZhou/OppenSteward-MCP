from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "OppenSteward-MCP"
ENV_FIELDS = {
    "TRANSPORT": "transport",
    "PUBLIC_URL": "public_url",
    "HOST": "host",
    "PORT": "port",
    "SCAN_ROOTS": "scan_roots",
    "EXCLUDE_ROOTS": "exclude_roots",
    "STATE_DIR": "state_dir",
    "SKILL_ROOT": "skill_root",
    "SCAN_INTERVAL": "scan_interval",
    "SCAN_SECONDS": "scan_seconds",
    "MAX_SCAN_DIRS": "max_scan_dirs",
    "EXTRA_REDIRECT_URIS": "extra_redirect_uris",
    "TUNNEL_ID": "tunnel_id",
    "TUNNEL_PROFILE": "tunnel_profile",
    "TUNNEL_CLIENT": "tunnel_client",
}
INTEGER_FIELDS = {"port", "scan_interval", "scan_seconds", "max_scan_dirs"}
LIST_FIELDS = {"scan_roots", "exclude_roots", "extra_redirect_uris"}


def runtime_environment(directory: Path = ROOT) -> dict[str, str]:
    """Read exactly this checkout's .env, without shell evaluation or interpolation."""
    values = {k: v for k, v in dotenv_values(directory / ".env", interpolate=False).items() if v is not None}
    return {**values, **os.environ}


@dataclass
class Settings:
    transport: str = "http"
    public_url: str = "http://127.0.0.1:8766"
    host: str = "127.0.0.1"
    port: int = 8766
    scan_roots: list[str] = field(default_factory=lambda: [str(Path.home())])
    exclude_roots: list[str] = field(default_factory=list)
    state_dir: Path = ROOT / ".runtime"
    skill_root: Path | None = None
    scan_interval: int = 300
    scan_seconds: int = 90
    max_scan_dirs: int = 500_000
    extra_redirect_uris: list[str] = field(default_factory=list)
    tunnel_id: str = ""
    tunnel_profile: str = "oppen-steward"
    tunnel_client: str = "tunnel-client"

    def __post_init__(self):
        if self.transport not in {"http", "stdio"}:
            raise ValueError("OPPEN_TRANSPORT must be http or stdio")
        self.public_url = self.public_url.rstrip("/")
        u = urlsplit(self.public_url)
        local = u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}
        if not (u.scheme == "https" or local) or not u.hostname:
            raise ValueError("public_url must be HTTPS (HTTP is allowed only on loopback for local tests)")
        if u.path or u.query or u.fragment or u.username or u.password:
            raise ValueError("Use a dedicated origin for public_url, without a path, query or credentials")
        if self.host not in {"127.0.0.1", "::1"}:
            raise ValueError("Bind only to loopback; a proxy may forward to this listener")
        if not 1024 <= self.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        if not self.state_dir:
            raise ValueError("state_dir must not be empty")
        self.state_dir = Path(self.state_dir).expanduser().resolve()
        self.skill_root = Path(self.skill_root).expanduser().resolve() if self.skill_root else None
        for name in LIST_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError(f"{name} must be a JSON array of nonempty strings")
        if not self.scan_roots:
            raise ValueError("Configure at least one scan root")
        self.scan_roots = [str(Path(p).expanduser().resolve()) for p in self.scan_roots]
        self.exclude_roots = [str(Path(p).expanduser().resolve()) for p in self.exclude_roots]
        if self.scan_interval < 10 or self.scan_seconds < 1 or self.max_scan_dirs < 1:
            raise ValueError("Invalid scan limits")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.tunnel_profile):
            raise ValueError("Invalid tunnel profile name")

    def skill_guide(self, skill: str) -> Path:
        if skill not in {"oppen-project-steward", "stepwise-r-project"}:
            raise ValueError("Unknown skill")
        roots = (
            [self.skill_root]
            if self.skill_root
            else [Path.home() / ".codex/skills", Path.home() / ".agents/skills"]
        )
        for root in roots:
            path = root / skill / "SKILL.md"
            if path.is_file():
                return path
        raise ValueError(
            "Skill guide not installed; configure OPPEN_SKILL_ROOT. Project discovery still works."
        )

    @property
    def resource(self):
        return self.public_url + "/mcp"

    @property
    def secure_cookie(self):
        return self.public_url.startswith("https://")

    @classmethod
    def load(cls, path: Path = ROOT / "config.local.json"):
        path = Path(path).expanduser().resolve()
        values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"transport": "stdio"}
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Config must be an object containing documented settings only")
        env = runtime_environment(path.parent)
        for suffix, name in ENV_FIELDS.items():
            raw = env.get("OPPEN_" + suffix)
            if raw is None:
                continue
            try:
                values[name] = (
                    int(raw) if name in INTEGER_FIELDS else json.loads(raw) if name in LIST_FIELDS else raw
                )
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Invalid OPPEN_{suffix}; expected an integer or JSON array as documented"
                ) from e
        # Resolve paths against the selected config directory, independent of caller cwd.
        for name in ("state_dir", "skill_root"):
            value = values.get(name, ".runtime" if name == "state_dir" else None)
            if value:
                p = Path(value).expanduser()
                values[name] = str(p if p.is_absolute() else path.parent / p)
            else:
                values[name] = None
        for name in ("scan_roots", "exclude_roots"):
            if name in values and isinstance(values[name], list):
                values[name] = [
                    str(Path(p).expanduser() if Path(p).expanduser().is_absolute() else path.parent / p)
                    if isinstance(p, str)
                    else p
                    for p in values[name]
                ]
        return cls(**values)
