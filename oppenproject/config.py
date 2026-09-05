from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT.parent / "Academic_skill/academic-skills"


@dataclass
class Settings:
    public_url: str = "http://127.0.0.1:8766"
    host: str = "127.0.0.1"
    port: int = 8766
    scan_roots: list[str] = field(default_factory=lambda: ["/Users", "/Volumes", "/opt", "/usr/local"])
    exclude_roots: list[str] = field(default_factory=list)
    state_dir: Path = ROOT / ".runtime"
    skill_root: Path = SKILLS
    scan_interval: int = 300
    scan_seconds: int = 90
    max_scan_dirs: int = 500_000
    extra_redirect_uris: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.public_url = self.public_url.rstrip("/")
        u = urlsplit(self.public_url)
        local = u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}
        if not (u.scheme == "https" or local) or not u.hostname:
            raise ValueError("public_url must be HTTPS (HTTP is allowed only on loopback for local tests)")
        if u.path or u.query or u.fragment or u.username or u.password:
            raise ValueError("Use a dedicated origin for public_url, without a path, query or credentials")
        if self.host not in {"127.0.0.1", "::1"}:
            raise ValueError("Bind only to loopback; frpc forwards to this listener")
        if not 1024 <= self.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        self.state_dir = Path(self.state_dir).expanduser().resolve()
        self.skill_root = Path(self.skill_root).expanduser().resolve()
        self.scan_roots = [str(Path(p).expanduser().resolve()) for p in self.scan_roots]
        self.exclude_roots = [str(Path(p).expanduser().resolve()) for p in self.exclude_roots]
        if self.scan_interval < 10 or self.scan_seconds < 1 or self.max_scan_dirs < 1:
            raise ValueError("Invalid scan limits")

    @property
    def resource(self):
        return self.public_url + "/mcp"

    @property
    def secure_cookie(self):
        return self.public_url.startswith("https://")

    @classmethod
    def load(cls, path: Path = ROOT / "config.local.json"):
        if not path.is_file():
            raise ValueError("Run .venv/bin/python run.py configure --public-url https://YOUR-DOMAIN first")
        return cls(**json.loads(path.read_text()))
