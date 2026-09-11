from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .config import Settings

# These internal/runtime names are excluded from file access, not traversed for discovery.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".runtime",
    ".Trash",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    ".cache",
    "Caches",
    ".npm",
    ".pnpm-store",
    "site-packages",
    "miniconda3",
    "anaconda3",
    "Cellar",
    "Caskroom",
    "System",
    ".pytest_cache",
    ".ruff_cache",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "AppData",
    "$Recycle.Bin",
    "System Volume Information",
}
SECRET_PARTS = {".ssh", ".aws", ".azure", ".gnupg", ".kube", ".config", ".runtime", ".git"}
SECRET_NAMES = {
    "config.local.json",
    "credentials.json",
    "credentials",
    "secrets.json",
    "secrets.toml",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
}
STEWARD = "<!-- oppen-project-steward:v3 -->"
R_MARKERS = {f"<!-- stepwise-r-project:v{v} -->": f"v{v}" for v in (2, 3)}


class AccessDenied(ValueError):
    pass


def relative_parts(path: str) -> tuple[str, ...]:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or "\\" in path or "\x00" in path or ":" in path:
        raise AccessDenied("Use a project-relative path without '..', backslashes or NUL")
    parts = p.parts
    for part in parts:
        lower = part.lower()
        if (
            lower in SECRET_PARTS
            or lower in SECRET_NAMES
            or lower in SKIP_DIRS
            or (lower.startswith(".env") and lower not in {".env.example", ".env.sample"})
            or lower.endswith((".pem", ".key", ".p12", ".pfx", ".keychain-db"))
        ):
            raise AccessDenied("Credential, repository-internal or runtime path is excluded")
    return parts


@contextmanager
def open_beneath(root: Path, path: str = ".", directory: bool = False, expected_root=None):
    """Open every component relative to an anchored descriptor, never following links.

    The descriptor walk also protects against directory/symlink swaps between validation
    and read. Absolute root ancestors are opened the same way. Only regular files and
    directories are accepted, so devices, sockets and FIFOs cannot block or escape.
    """
    parts = relative_parts(path)
    if os.name == "nt":
        from .windows_fs import open_beneath as windows_open

        try:
            with windows_open(root, parts, directory, expected_root) as fd:
                yield fd
        except OSError as e:
            raise AccessDenied("Path unavailable, replaced, linked or not an ordinary local file") from e
        return
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        root_stat = os.fstat(fd)
        if expected_root is not None and (root_stat.st_dev, root_stat.st_ino) != expected_root:
            raise AccessDenied("Project root was replaced during access")
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            new_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        st = os.fstat(fd)
        valid = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        if not valid:
            raise AccessDenied("Only regular files or directories are supported")
        if not directory and st.st_nlink > 1:
            raise AccessDenied("Hard-linked files are excluded; use an ordinary project file")
        yield fd
    except OSError as e:
        raise AccessDenied("Path unavailable or symlink access denied") from e
    finally:
        os.close(fd)


def marker_text(root: Path, path: str) -> str:
    try:
        with open_beneath(root, path) as fd:
            return os.read(fd, 2 * 1024 * 1024).decode("utf-8", errors="replace")
    except AccessDenied:
        return ""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: str
    skill: str
    version: str
    registry: str
    device: int
    inode: int

    def public(self):
        return {
            **{k: v for k, v in asdict(self).items() if k not in {"device", "inode"}},
            "file_access": "governance-only",
        }


def identify(root: Path) -> Project | None:
    registry = ".oppen-project-steward/registry.md"
    text = marker_text(root, registry)
    skill, version = "oppen-project-steward", "v3"
    if text.splitlines().count(STEWARD) != 1:
        registry = "project.md"
        text = marker_text(root, registry)
        lines = text.splitlines()
        markers = [v for k, v in R_MARKERS.items() if lines.count(k) == 1]
        if len(markers) == 1:
            skill, version = "stepwise-r-project", markers[0]
        elif lines.count(STEWARD) == 1:
            version = "legacy-layout"
        else:
            return None
    try:
        with open_beneath(root, directory=True) as fd:
            st = os.fstat(fd)
    except AccessDenied:
        return None
    pid = hashlib.sha256(str(root).encode()).hexdigest()[:20]
    return Project(pid, root.name, str(root), skill, version, registry, st.st_dev, st.st_ino)


class Catalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.projects: dict[str, Project] = {}
        self.report: dict = {"status": "not_loaded", "mode": "registered", "errors": []}
        self.lock = threading.RLock()
        self.signature = None

    def excluded(self, path: Path):
        return any(
            path.is_relative_to(Path(p))
            for p in self.settings.exclude_roots
            + [
                str(self.settings.state_dir),
            ]
        )

    @staticmethod
    def file_signature(st):
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)

    def registered_roots(self, signature):
        """Read one bounded local configuration file; never enumerate project directories."""
        path = self.settings.projects_file
        with open_beneath(path.parent, path.name) as fd:
            before = os.fstat(fd)
            if self.file_signature(before) != signature or before.st_size > 1_048_576:
                raise ValueError("Project configuration changed or exceeds 1 MiB")
            chunks, remaining = [], 1_048_577
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if not remaining or self.file_signature(os.fstat(fd)) != signature:
                raise ValueError("Project configuration changed or exceeds 1 MiB")

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate configuration key")
                result[key] = value
            return result

        config = json.loads(b"".join(chunks).decode("utf-8-sig"), object_pairs_hook=unique_keys)
        if not isinstance(config, dict) or set(config) != {"projects"}:
            raise ValueError('Expected {"projects": ["/exact/project/root"]}')
        roots = config["projects"]
        if not isinstance(roots, list) or len(roots) > 1000:
            raise ValueError("Register at most 1000 project paths")
        if any(not isinstance(p, str) or not p.strip() or len(p) > 4096 or "\0" in p for p in roots):
            raise ValueError("Project paths must be nonempty strings without NUL")
        normalized = []
        for root in roots:
            expanded = Path(root).expanduser()
            # Lexical normalization preserves symlinks for open_beneath to reject.
            absolute = os.path.abspath(expanded if expanded.is_absolute() else path.parent / expanded)
            if absolute not in normalized:
                normalized.append(absolute)
        return normalized

    def refresh(self, force=False):
        """Reload registrations on file change; force only rechecks these exact roots."""
        with self.lock:
            try:
                signature = self.file_signature(self.settings.projects_file.lstat())
            except FileNotFoundError:
                signature = "missing"
            except OSError:
                signature = "unreadable"
            if signature == self.signature and not force:
                return self.report
            self.signature = signature
            projects, errors = {}, []
            count, status = 0, "ready"
            if signature in ("missing", "unreadable"):
                status = "config_missing" if signature == "missing" else "config_error"
            else:
                try:
                    roots = self.registered_roots(signature)
                    count = len(roots)
                    for root in roots:
                        path = Path(root)
                        project = None if self.excluded(path) else identify(path)
                        if project:
                            projects[project.id] = project
                        else:
                            errors.append({"path": root, "error": "unavailable_unmanaged_or_excluded"})
                    if self.file_signature(self.settings.projects_file.lstat()) != signature:
                        raise ValueError("Project configuration changed during reload")
                    if errors:
                        status = "partial"
                except (OSError, ValueError, RuntimeError):
                    # A missing/broken config never preserves an old authorization list.
                    projects, errors, status = {}, [], "config_error"
            self.projects = projects
            self.report = {
                "mode": "registered",
                "status": status,
                "registered_paths": count,
                "projects_found": len(projects),
                "error_count": len(errors),
                "errors": errors,
                "loaded_at": time.time(),
                "note": (
                    "Only exact roots in the local projects file are exposed; no recursive discovery. "
                    "Edit that file to add/remove projects. Missing or invalid configuration denies access. "
                    "Unavailable registered roots can be retried with refresh_projects."
                ),
            }
            return self.report

    def project(self, project_id: str):
        self.refresh()
        project = self.projects.get(project_id)
        if project is None:
            raise AccessDenied(
                "Project not registered or unavailable; check the local projects file and call list_projects"
            )
        current = identify(Path(project.root))
        if current != project or self.excluded(Path(project.root)):
            raise AccessDenied(
                "Project moved, marker changed, or root was replaced; reload the registered projects"
            )
        return project

    def public_report(self):
        # Do not disclose unavailable/excluded registration paths to remote clients.
        return {k: v for k, v in self.report.items() if k != "errors"}

    def governance_paths(self, project: Project) -> set[str]:
        """Explicit governance allowlist; document links never grant file access.

        v2 Memory contains unstructured historical material, so only its project map
        is exposed. For v3/legacy Steward, entry IDs come from the generated index,
        never from client paths or arbitrary Markdown links. Audit (including contract
        and function audits) is excluded because it can embed data and run output.
        """
        allowed = {project.registry}
        if project.skill == "stepwise-r-project" and project.version == "v2":
            return allowed
        prefix = ".oppen-project-steward/" if project.registry.startswith(".oppen-project-steward/") else ""
        for system, letter in (("Memory", "M"), ("Attention", "A")):
            index = prefix + system + "/index.md"
            if self.excluded(Path(project.root) / index):
                continue
            try:
                with open_beneath(
                    Path(project.root), index, expected_root=(project.device, project.inode)
                ) as fd:
                    if os.fstat(fd).st_size > 2 * 1024 * 1024:
                        continue
                    content = os.read(fd, 2 * 1024 * 1024).decode("utf-8")
                marker = f"<!-- {project.skill}:{system.lower()}-index -->"
                if project.skill == "stepwise-r-project":
                    # The R helper uses a fixed title/table, without a marker comment.
                    marker = "# Decision Memory" if system == "Memory" else "# Human Attention"
                if content.splitlines().count(marker) != 1 or "\x00" in content:
                    continue
            except (AccessDenied, UnicodeError):
                continue
            allowed.add(index)
            for line in content.splitlines():
                if not line.startswith("| ") or not line.endswith(" |"):
                    continue
                cells = [cell.strip() for cell in line.split("|")[1:-1]]
                entry_id = cells[0]
                if re.fullmatch(letter + r"-[0-9]{4,}", entry_id):
                    if project.skill == "stepwise-r-project" and cells[-1] != "entries/" + entry_id + ".md":
                        continue
                    allowed.add(prefix + system + "/entries/" + entry_id + ".md")
        return allowed

    @staticmethod
    def allowed_directories(files: set[str]) -> set[str]:
        return {str(parent) for path in files for parent in PurePosixPath(path).parents}

    @contextmanager
    def opened(self, project_id: str, path: str, directory=False):
        project = self.project(project_id)
        relative_parts(path)
        allowed = self.governance_paths(project)
        if str(PurePosixPath(path)) not in (self.allowed_directories(allowed) if directory else allowed):
            raise AccessDenied(
                "Only the governance registry and indexed Memory/Attention documents are exposed"
            )
        target = Path(project.root) / path
        if self.excluded(target):
            raise AccessDenied("This directory is excluded in the server configuration")
        with open_beneath(
            Path(project.root), path, directory=directory, expected_root=(project.device, project.inode)
        ) as fd:
            yield project, fd

    def list_files(self, project_id: str, path=".", offset=0, limit=200):
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("offset >= 0 and 1 <= limit <= 500 required")
        with self.opened(project_id, path, directory=True) as (project, fd):
            allowed = self.governance_paths(project)
            directories = self.allowed_directories(allowed)
            entries = []
            for name in sorted(os.listdir(Path(project.root) / path if os.name == "nt" else fd)):
                rel = str(PurePosixPath(path) / name)
                try:
                    relative_parts(rel)
                    if rel not in allowed and rel not in directories:
                        continue
                    if self.excluded(Path(project.root) / rel):
                        continue
                    st = (
                        (Path(project.root) / rel).lstat()
                        if os.name == "nt"
                        else os.stat(name, dir_fd=fd, follow_symlinks=False)
                    )
                    if getattr(st, "st_file_attributes", 0) & 0x400:
                        continue
                    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                        continue
                    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                        continue
                    is_directory = stat.S_ISDIR(st.st_mode)
                    if rel not in (directories if is_directory else allowed):
                        continue
                    entries.append(
                        {
                            "path": rel,
                            "kind": "directory" if is_directory else "file",
                            **({} if is_directory else {"size": st.st_size, "modified": st.st_mtime}),
                        }
                    )
                except (AccessDenied, OSError):
                    continue
            return {
                "entries": entries[offset : offset + limit],
                "total": len(entries),
                "next_offset": offset + limit if offset + limit < len(entries) else None,
            }

    def read_file(self, project_id: str, path: str, offset=0, length=65536, encoding="utf-8"):
        if offset < 0 or not 1 <= length <= 262144:
            raise ValueError("offset >= 0 and 1 <= length <= 262144 required")
        if encoding not in {"utf-8", "base64"}:
            raise ValueError("encoding must be utf-8 or base64")
        with self.opened(project_id, path) as (project, fd):
            before = os.fstat(fd)
            # Each request owns a separate descriptor; seek/read is safe on Windows too.
            os.lseek(fd, offset, os.SEEK_SET)
            data = os.read(fd, length)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("File changed during read; retry")
        if encoding == "base64":
            content = base64.b64encode(data).decode("ascii")
        else:
            if b"\x00" in data:
                raise ValueError("Binary file: use encoding='base64' to read exact bytes")
            content = data.decode("utf-8", errors="replace")
        return {
            "project_id": project.id,
            "path": path,
            "content": content,
            "encoding": encoding,
            "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
            "size": before.st_size,
            "modified_ns": before.st_mtime_ns,
            "offset": offset,
            "bytes_read": len(data),
            "sha256_chunk": hashlib.sha256(data).hexdigest(),
            "next_offset": offset + len(data) if offset + len(data) < before.st_size else None,
            "decoding_note": "UTF-8 replaces invalid or split characters; base64 preserves exact bytes.",
        }

    def search(self, query: str, project_id: str | None = None, glob="*", limit=30):
        if not query.strip() or len(query) > 500 or not 1 <= limit <= 100:
            raise ValueError("Provide a query of 1-500 characters and limit 1-100")
        self.refresh()
        projects = [self.project(project_id)] if project_id else list(self.projects.values())
        results = []
        scanned = skipped = 0
        start = time.monotonic()
        truncated = False
        for project in projects:
            stack = ["."]
            while stack:
                directory = stack.pop()
                offset = 0
                while True:
                    if scanned >= 10000 or time.monotonic() - start > 10 or len(results) >= limit:
                        truncated = True
                        break
                    try:
                        page = self.list_files(project.id, directory, offset, 500)
                    except AccessDenied:
                        skipped += 1
                        break
                    for entry in page["entries"]:
                        if len(results) >= limit or time.monotonic() - start > 10 or scanned >= 10000:
                            truncated = True
                            break
                        path = entry["path"]
                        if entry["kind"] == "directory":
                            stack.append(path)
                            continue
                        scanned += 1
                        if not fnmatch.fnmatch(path, glob):
                            continue
                        path_match = query.casefold() in path.casefold()
                        snippets = []
                        if entry["size"] <= 262144:
                            try:
                                content = self.read_file(project.id, path, length=262144)["content"]
                                snippets = [
                                    {"line": i, "text": line[:400]}
                                    for i, line in enumerate(content.splitlines(), 1)
                                    if query.casefold() in line.casefold()
                                ][:3]
                            except (AccessDenied, ValueError):
                                skipped += 1
                        else:
                            skipped += 1
                        if path_match or snippets:
                            file_id = (
                                project.id
                                + ":"
                                + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
                            )
                            results.append(
                                {
                                    "id": file_id,
                                    "title": project.name + "/" + path,
                                    "project_id": project.id,
                                    "path": path,
                                    "snippets": snippets,
                                }
                            )
                    if truncated or page["next_offset"] is None:
                        break
                    offset = page["next_offset"]
                if truncated:
                    break
            if truncated:
                break
        return {
            "results": results,
            "truncated": truncated,
            "files_examined": scanned,
            "content_skipped": skipped,
            "note": "Governance allowlist only. Document references do not grant access to their targets.",
        }

    def fetch(self, file_id: str, offset=0, length=65536):
        try:
            pid, encoded = file_id.split(":", 1)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
                raise ValueError
            path = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeError) as e:
            raise ValueError("Invalid file ID; use an ID returned by search") from e
        return self.read_file(pid, path, offset, length)
