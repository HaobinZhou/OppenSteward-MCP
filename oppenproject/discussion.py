"""MCP-owned, flat Discussion documents; never a general-purpose file writer."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .catalog import AccessDenied, open_beneath

MAX_BYTES = 262144
MAX_DOCUMENTS = 1000
NAME = re.compile(r"(D-[0-9]{6})__(.+)\.md")
PROCESS_LOCK = threading.Lock()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def topic_text(topic):
    topic = unicodedata.normalize("NFC", topic.strip())
    if (
        not topic
        or len(topic) > 80
        or len(topic.encode("utf-8")) > 180
        or any(unicodedata.category(c).startswith("C") for c in topic)
        or any(c in topic for c in '/\\:*?"<>|')
        or topic.endswith((".", " "))
        or re.search(r"(?:^|[_ -])(new|final|old|backup|v[0-9]+)$", topic, re.I)
    ):
        raise ValueError("Use a short topic without paths, control characters or revision suffixes")
    return topic


def directory_for(project):
    if project.version != "v3":
        raise AccessDenied("Discussion requires a current v3 layout; legacy projects remain read-only")
    return ".oppen-project-steward/Discussion" if project.skill == "oppen-project-steward" else "Discussion"


class Directory:
    """All names come from fixed index/ID rules, under a held, verified directory."""

    def __init__(self, path, fd):
        self.path, self.fd = path, fd

    def names(self):
        with os.scandir(self.path if os.name == "nt" else self.fd) as entries:
            names = []
            for entry in entries:
                if len(names) >= MAX_DOCUMENTS + 100:
                    raise ValueError("Discussion directory has too many entries")
                names.append(entry.name)
        return names

    def read(self, name, limit=MAX_BYTES):
        # Missing is distinct from unsafe/unreadable: a symlink must never mean 'new file'.
        try:
            st = (
                (self.path / name).lstat()
                if os.name == "nt"
                else os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or getattr(st, "st_file_attributes", 0) & 0x400:
            raise AccessDenied("Discussion files must be ordinary files without links")
        if os.name == "nt":
            from .windows_fs import _open

            fd = _open(self.path / name, directory=False)
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise AccessDenied("Discussion file is linked, special or too large")
            data = source.read(limit + 1)
            after = os.fstat(source.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or len(data) > limit:
                raise ValueError("Discussion changed during read; retry")
        data.decode("utf-8")
        if b"\0" in data:
            raise AccessDenied("Discussion must contain UTF-8 text without NUL")
        return data, after

    def replace(self, name, data, expected):
        # Atomic publication; never truncate an existing file or follow the target.
        temporary = ".mcp-" + secrets.token_hex(16) + ".tmp"
        options = {} if os.name == "nt" else {"dir_fd": self.fd}
        temp_path = self.path / temporary if os.name == "nt" else temporary
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, **options)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            current = self.read(name, 2 * 1024 * 1024 if name == "index.md" else MAX_BYTES)
            if (sha(current[0]) if current else None) != expected:
                raise ValueError("Discussion changed since read; read again before editing")
            target = self.path / name if os.name == "nt" else name
            if expected is None:
                if os.name == "nt":
                    os.rename(temp_path, target)  # Windows rename refuses an existing target.
                else:
                    os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=self.fd)
            elif os.name == "nt":
                os.replace(temp_path, target)
            else:
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            if os.name != "nt":
                os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp_path, **options)
            except FileNotFoundError:
                pass


class Discussions:
    def __init__(self, catalog):
        self.catalog, self.settings = catalog, catalog.settings

    @contextmanager
    def directory(self, project_id, create=False):
        project = self.catalog.project(project_id)
        relative = directory_for(project)
        path = Path(project.root) / relative
        if self.settings.discussion_mode == "off" or self.catalog.excluded(path):
            raise AccessDenied("Discussion access is disabled or excluded")
        parent = str(Path(relative).parent).replace("\\", "/")
        with open_beneath(
            Path(project.root), parent, directory=True, expected_root=(project.device, project.inode)
        ) as parent_fd:
            if create:
                try:
                    if os.name == "nt":
                        path.mkdir(mode=0o700)
                    else:
                        os.mkdir("Discussion", mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            try:
                if os.name == "nt":
                    from .windows_fs import _open

                    fd = _open(path, directory=True)
                else:
                    fd = os.open("Discussion", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                yield project, None
                return
            except OSError as error:
                raise AccessDenied("Discussion directory unavailable or linked") from error
            try:
                yield project, Directory(path, fd)
            finally:
                os.close(fd)

    @contextmanager
    def locked(self):
        """Serialize processes as well as threads, including durable journal commits."""
        root = self.settings.state_dir
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        if os.name == "nt":
            from .windows_fs import make_private

            make_private(root, directory=True)
        with PROCESS_LOCK, (root / "discussion.lock").open("a+b") as lock:
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
                lock.flush()
            start = time.monotonic()
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() - start > 10:
                        raise ValueError("Discussion is busy; retry the same request_id") from None
                    time.sleep(0.05)
            try:
                db = sqlite3.connect(root / "discussion.sqlite3", timeout=10)
                try:
                    db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, value TEXT)")
                    db.commit()
                    (root / "discussion.sqlite3").chmod(0o600)
                    if os.name == "nt":
                        make_private(root / "discussion.sqlite3")
                    yield db
                finally:
                    db.close()
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def state_key(project):
        return f"{project.id}:{project.device}:{project.inode}:{project.skill}:{project.registry}"

    @classmethod
    def state(cls, db, project):
        row = db.execute("SELECT value FROM projects WHERE id=?", (cls.state_key(project),)).fetchone()
        return json.loads(row[0]) if row else {"high_water": 0, "documents": {}, "requests": {}}

    @classmethod
    def save(cls, db, project, state):
        db.execute(
            "INSERT OR REPLACE INTO projects VALUES (?, ?)", (cls.state_key(project), json.dumps(state))
        )
        db.commit()

    def inventory(self, directory, state):
        documents = []
        seen = set()
        for name in sorted(directory.names()):
            match = NAME.fullmatch(name)
            if not match or match[1] == "D-000000":
                continue
            try:
                topic = topic_text(match[2])
            except ValueError:
                continue
            if match[1] in seen:
                raise ValueError("Duplicate Discussion ID; resolve the filenames locally")
            seen.add(match[1])
            if self.catalog.excluded(directory.path / name):
                continue
            value = directory.read(name)
            if value is None:
                raise ValueError("Discussion directory changed; retry")
            data, st = value
            meta = state["documents"].get(match[1], {})
            documents.append(
                {
                    "id": match[1],
                    "filename": name,
                    "topic": topic,
                    "description": meta.get("description", topic),
                    "updated_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                    "revision": sha(data),
                    "size": len(data),
                }
            )
        if len(documents) > MAX_DOCUMENTS:
            raise ValueError("Discussion document limit reached")
        return documents

    def list(self, project_id, offset=0, limit=100):
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("offset >= 0 and limit 1-200 required")
        with self.locked() as db, self.directory(project_id) as (project, directory):
            state = self.state(db, project)
            documents = self.inventory(directory, state) if directory else []
            return {
                "project_id": project.id,
                "directory": directory_for(project),
                "documents": documents[offset : offset + limit],
                "total": len(documents),
                "next_offset": offset + limit if offset + limit < len(documents) else None,
            }

    def read(self, project_id, discussion_id):
        if discussion_id != "index" and not re.fullmatch(r"D-[0-9]{6}", discussion_id):
            raise ValueError("Use a Discussion ID from list_discussions, or 'index'")
        with self.locked() as db, self.directory(project_id) as (project, directory):
            if directory is None:
                raise ValueError("No Discussion documents yet")
            documents = self.inventory(directory, self.state(db, project))
            if discussion_id == "index":
                name = "index.md"
            else:
                doc = next((d for d in documents if d["id"] == discussion_id), None)
                if doc is None:
                    raise AccessDenied("Unknown Discussion ID")
                name = doc["filename"]
            if self.catalog.excluded(directory.path / name):
                raise AccessDenied("Discussion file is excluded")
            value = directory.read(name, 2 * 1024 * 1024 if discussion_id == "index" else MAX_BYTES)
            if value is None:
                raise ValueError("Discussion index is missing; list_discussions still works")
            return {
                "project_id": project.id,
                "id": discussion_id,
                "path": directory_for(project) + "/" + name,
                "content": value[0].decode("utf-8"),
                "revision": sha(value[0]),
            }

    def write(
        self,
        project_id,
        *,
        content,
        description,
        request_id,
        topic=None,
        discussion_id=None,
        expected_revision=None,
    ):
        if self.settings.discussion_mode != "write":
            raise AccessDenied("Discussion writing is disabled; set OPPEN_DISCUSSION_MODE=write locally")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", request_id):
            raise ValueError("Use a unique request_id of 16-100 letters, digits, hyphens or underscores")
        if len(content.encode("utf-8")) > MAX_BYTES or "\0" in content:
            raise ValueError("Discussion content must be UTF-8 text up to 256 KiB, without NUL")
        if (
            not description.strip()
            or len(description) > 400
            or any(unicodedata.category(c).startswith("C") for c in description)
        ):
            raise ValueError("Provide a brief, single-line description of 1-400 characters")
        if discussion_id is None:
            topic = topic_text(topic or "")
        elif not re.fullmatch(r"D-[0-9]{6}", discussion_id) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_revision or ""
        ):
            raise ValueError("Editing requires a Discussion ID and revision from read_discussion")
        fingerprint = sha(
            json.dumps([topic, discussion_id, expected_revision, content, description]).encode()
        )
        with self.locked() as db, self.directory(project_id, create=True) as (project, directory):
            state = self.state(db, project)
            previous = state["requests"].get(request_id)
            if previous:
                st = os.fstat(directory.fd)
                if previous["directory_identity"] != [st.st_dev, st.st_ino]:
                    previous.update(status="conflict")
                    previous.pop("content", None)
                    self.save(db, project, state)
                    raise ValueError("Discussion directory was replaced; read again and use a new request_id")
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("request_id was already used with different arguments")
                if previous["status"] == "done":
                    return {**previous["result"], "replayed": True}
                if previous["status"] == "conflict":
                    raise ValueError("Previous request conflicted; read again and use a new request_id")
            elif any(r["status"] == "pending" for r in state["requests"].values()):
                pending = next(k for k, r in state["requests"].items() if r["status"] == "pending")
                raise ValueError(f"An interrupted write needs request_id {pending} retried first")
            else:
                if len(state["requests"]) >= 10000:
                    raise ValueError("Discussion request ledger is full; local maintenance required")
                documents = self.inventory(directory, state)
                if self.catalog.excluded(directory.path / "index.md"):
                    raise AccessDenied("Discussion index is excluded")
                index = directory.read("index.md", 2 * 1024 * 1024)
                # Also preserve the high-water mark if the server runtime was moved/recreated.
                water = (
                    re.findall(rb"<!-- oppen-mcp:discussion-high-water:([0-9]{6}) -->", index[0])
                    if index
                    else []
                )
                state["high_water"] = max(
                    [
                        state["high_water"],
                        *(int(n) for n in water),
                        *(int(m[1][2:]) for n in directory.names() if (m := NAME.fullmatch(n))),
                    ]
                )
                if discussion_id is None:
                    if len(documents) >= MAX_DOCUMENTS or state["high_water"] >= 999999:
                        raise ValueError("Discussion sequence or document limit reached")
                    state["high_water"] += 1
                    discussion_id = f"D-{state['high_water']:06d}"
                    name = discussion_id + "__" + topic + ".md"
                else:
                    doc = next((d for d in documents if d["id"] == discussion_id), None)
                    if doc is None or doc["revision"] != expected_revision:
                        raise ValueError("Discussion changed since read; read again before editing")
                    name = doc["filename"]
                if self.catalog.excluded(directory.path / name):
                    raise AccessDenied("Discussion file is excluded")
                previous = {
                    "fingerprint": fingerprint,
                    "status": "pending",
                    "id": discussion_id,
                    "filename": name,
                    "before": expected_revision,
                    "content": content,
                    "description": description,
                    "directory_identity": [os.fstat(directory.fd).st_dev, os.fstat(directory.fd).st_ino],
                }
                state["requests"][request_id] = previous
                # Reserve the ID and retry identity durably before touching project files.
                self.save(db, project, state)
            try:
                return self.finish(db, project, directory, state, previous)
            except OSError as error:
                raise ValueError(
                    f"Write interrupted; document may already be saved. Retry request_id {request_id} "
                    "with identical arguments to finish the index."
                ) from error

    def finish(self, db, project, directory, state, operation):
        name, data = operation["filename"], operation["content"].encode("utf-8")
        if any(self.catalog.excluded(directory.path / n) for n in (name, "index.md")):
            raise AccessDenied("Discussion file or index is excluded")
        index = directory.read("index.md", 2 * 1024 * 1024)
        current = directory.read(name)
        revision = sha(current[0]) if current else None
        if revision != sha(data):
            if revision != operation["before"]:
                operation.update(status="conflict")
                operation.pop("content")
                self.save(db, project, state)
                raise ValueError("Discussion changed since read; read again before editing")
            directory.replace(name, data, operation["before"])
        state["documents"][operation["id"]] = {"description": operation["description"]}
        documents = self.inventory(directory, state)
        lines = [
            "# Discussion",
            "",
            f"<!-- oppen-mcp:discussion-high-water:{state['high_water']:06d} -->",
            "",
        ]
        for doc in documents:
            title = html.escape(doc["topic"]).replace("[", "&#91;").replace("]", "&#93;")
            description = html.escape(doc["description"])
            lines.append(
                f"- [{doc['id']} · {title}]({quote(doc['filename'], safe='')}) — "
                f"{description} · {doc['updated_at']}"
            )
        directory.replace("index.md", ("\n".join(lines) + "\n").encode(), sha(index[0]) if index else None)
        result = {
            "project_id": project.id,
            "id": operation["id"],
            "path": directory_for(project) + "/" + name,
            "revision": sha(data),
            "index_updated": True,
            "replayed": False,
        }
        operation.update(status="done", result=result)
        operation.pop("content")
        self.save(db, project, state)
        return result
