import asyncio
import base64
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from oppenproject.auth import DISCUSSION_READ, DISCUSSION_WRITE, SCOPE, OAuthProvider
from oppenproject.catalog import AccessDenied, Catalog
from oppenproject.discussion import Directory, Discussions
from oppenproject.server import create_app, create_mcp

from .conftest import CALLBACK, begin, consent_form, rpc, token


@pytest.fixture
def discussion(settings):
    settings.discussion_mode = "write"
    catalog = Catalog(settings)
    catalog.refresh()
    return Discussions(catalog), next(iter(catalog.projects))


def create(store, pid, key="test-request-0001", **kwargs):
    return store.write(
        pid,
        topic="部署方式讨论",
        content="自由正文\n```r\nx <- 1\n```\n",
        description="讨论本机与网页的部署",
        request_id=key,
        **kwargs,
    )


def edit(store, pid, doc, content="追加后的完整正文", key="test-request-0002"):
    return store.write(
        pid,
        discussion_id=doc["id"],
        content=content,
        description="继续讨论部署",
        expected_revision=doc["revision"],
        request_id=key,
    )


@pytest.mark.parametrize("skill", ["steward", "r"])
def test_create_edit_and_index_without_governance_changes(discussion, skill):
    store, pid = discussion
    project = store.catalog.projects[pid]
    root = Path(project.root)
    if skill == "r":
        (root / project.registry).unlink()
        (root / "project.md").write_text("<!-- stepwise-r-project:v3 -->\n", encoding="utf-8")
        store.catalog.refresh()
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert store.list(pid)["documents"] == []
    first = create(store, pid)
    assert first["id"] == "D-000001"
    assert first["path"].startswith(
        ".oppen-project-steward/Discussion/" if skill == "steward" else "Discussion/"
    )
    assert create(store, pid)["replayed"]
    read = store.read(pid, first["id"])
    assert read["content"].startswith("自由正文")
    updated = edit(store, pid, read)
    assert updated["path"] == first["path"] and updated["revision"] != first["revision"]
    assert edit(store, pid, read)["replayed"]
    assert "继续讨论部署" in store.read(pid, "index")["content"]
    assert create(store, pid, key="test-request-0003")["id"] == "D-000002"
    assert len(store.list(pid)["documents"]) == 2
    assert all((root / p).read_bytes() == data for p, data in before.items())
    assert not (root / ".git").exists()


def test_conflicts_local_edit_and_retry_identity(discussion):
    store, pid = discussion
    first = create(store, pid)
    edit(store, pid, first)
    with pytest.raises(ValueError, match="changed since read"):
        edit(store, pid, first, key="stale-request-0001")
    with pytest.raises(ValueError, match="different arguments"):
        edit(store, pid, first, content="wrong retry")
    fresh = store.read(pid, first["id"])
    path = Path(store.catalog.projects[pid].root) / first["path"]
    path.write_text("本机刚修改的内容", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since read"):
        edit(store, pid, fresh, key="stale-request-0002")
    assert path.read_text(encoding="utf-8") == "本机刚修改的内容"


def test_concurrent_creates_and_edits_across_instances(discussion):
    store, pid = discussion
    other = Discussions(store.catalog)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda n: create(other if n % 2 else store, pid, key=f"parallel-create-{n:03d}"), range(8)
            )
        )
    assert len({r["id"] for r in results}) == 8
    first = results[0]

    def update(n):
        try:
            edit(other if n % 2 else store, pid, first, content=str(n), key=f"parallel-update-{n:03d}")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(update, range(4))) == 1


def test_parallel_processes_do_not_reuse_ids(discussion):
    store, pid = discussion
    program = """
import json, sys
from pathlib import Path
from oppenproject.catalog import Catalog
from oppenproject.config import Settings
from oppenproject.discussion import Discussions
catalog = Catalog(Settings(transport='stdio', scan_roots=[sys.argv[1]],
                          state_dir=Path(sys.argv[2]), discussion_mode='write'))
catalog.refresh()
result = Discussions(catalog).write(sys.argv[3], topic='并发讨论', content='text',
                                    description='text', request_id=sys.argv[4])
print(json.dumps(result))
"""

    def launch(n):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                store.settings.scan_roots[0],
                str(store.settings.state_dir),
                pid,
                f"multi-process-{n:04d}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        return json.loads(result.stdout)["id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(launch, range(4))) == {f"D-{n:06d}" for n in range(1, 5)}


def test_replaced_directory_does_not_replay_old_write(discussion):
    store, pid = discussion
    first = create(store, pid)
    directory = (Path(store.catalog.projects[pid].root) / first["path"]).parent
    directory.rename(directory.with_name("local-moved-discussion"))
    with pytest.raises(ValueError, match="directory was replaced"):
        create(store, pid)
    assert not list(directory.glob("D-*.md"))
    assert create(store, pid, key="fresh-request-0001")["id"] == "D-000002"


def test_high_water_survives_restart_local_delete_and_missing_index(discussion):
    store, pid = discussion
    first = create(store, pid)
    path = Path(store.catalog.projects[pid].root) / first["path"]
    path.unlink()
    path.with_name("index.md").unlink()
    restarted = Discussions(store.catalog)
    assert create(restarted, pid, key="second-request-0001")["id"] == "D-000002"
    # The generated index can recover the counter if the runtime directory is replaced.
    store.settings.state_dir = store.settings.state_dir.with_name("fresh-runtime")
    assert create(Discussions(store.catalog), pid, key="third-request-0001")["id"] == "D-000003"


def test_interrupted_index_write_retries_without_duplicate(discussion, monkeypatch):
    store, pid = discussion
    original = Directory.replace

    def fail_index(self, name, data, expected):
        if name == "index.md":
            raise OSError("simulated disk failure")
        return original(self, name, data, expected)

    monkeypatch.setattr(Directory, "replace", fail_index)
    with pytest.raises(ValueError, match="Write interrupted"):
        create(store, pid)
    assert len(store.list(pid)["documents"]) == 1
    monkeypatch.setattr(Directory, "replace", original)
    assert create(Discussions(store.catalog), pid)["id"] == "D-000001"
    assert len(store.list(pid)["documents"]) == 1
    assert "D-000001" in store.read(pid, "index")["content"]


@pytest.mark.parametrize("topic", ["../Data/raw", "C:private", "a\\b", "a\0b", "方案_final", "x" * 181])
def test_reject_paths_and_revision_names(discussion, topic):
    store, pid = discussion
    with pytest.raises(ValueError):
        store.write(pid, topic=topic, content="text", description="text", request_id="invalid-topic-0001")
    assert store.list(pid)["total"] == 0


def test_no_general_file_writing_or_old_read_bypass(discussion):
    store, pid = discussion
    first = create(store, pid)
    for target in ["index", "../registry.md", "D-000001__topic.md", "/etc/passwd", "D-000999"]:
        with pytest.raises(ValueError):
            edit(store, pid, {"id": target, "revision": first["revision"]}, key="bad-target-00001")
    with pytest.raises(AccessDenied):
        store.catalog.read_file(pid, first["path"])
    assert not store.catalog.search("自由正文")["results"]
    encoded = base64.urlsafe_b64encode(first["path"].encode()).decode().rstrip("=")
    with pytest.raises(AccessDenied):
        store.catalog.fetch(pid + ":" + encoded)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture; Windows junction tested separately")
@pytest.mark.parametrize("target", ["directory", "index", "document", "hardlink", "fifo"])
def test_links_and_special_files_cannot_escape(discussion, tmp_path, target):
    store, pid = discussion
    root = Path(store.catalog.projects[pid].root)
    directory = root / ".oppen-project-steward/Discussion"
    outside = tmp_path / "private"
    outside.mkdir()
    secret = outside / "private.md"
    secret.write_text("PRIVATE", encoding="utf-8")
    if target == "directory":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        path = directory / ("index.md" if target == "index" else "D-000001__既有讨论.md")
        if target == "hardlink":
            os.link(secret, path)
        elif target == "fifo":
            os.mkfifo(path)
        else:
            path.symlink_to(secret)
    with pytest.raises((AccessDenied, OSError)):
        create(store, pid)
    assert secret.read_text(encoding="utf-8") == "PRIVATE"
    assert sorted(p.name for p in outside.iterdir()) == ["private.md"]


def test_exclusions_modes_legacy_and_size(discussion):
    store, pid = discussion
    root = Path(store.catalog.projects[pid].root)
    store.settings.exclude_roots = [str(root / ".oppen-project-steward/Discussion")]
    with pytest.raises(AccessDenied):
        create(store, pid)
    store.settings.exclude_roots = []
    for mode in ["read", "off"]:
        store.settings.discussion_mode = mode
        with pytest.raises(AccessDenied):
            create(store, pid)
    store.settings.discussion_mode = "write"
    with pytest.raises(ValueError, match="256 KiB"):
        store.write(
            pid, topic="讨论", content="中" * 100000, description="text", request_id="oversize-request-1"
        )
    (root / ".oppen-project-steward/registry.md").unlink()
    (root / "project.md").write_text("<!-- stepwise-r-project:v2 -->\n", encoding="utf-8")
    store.catalog.refresh()
    with pytest.raises(AccessDenied, match="v3"):
        create(store, pid)


def grant(client, scopes):
    registered = client.post(
        "/register",
        json={
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(scopes),
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert registered.status_code == 201, registered.text
    credentials = registered.json()
    response, verifier = begin(client, credentials, scope=" ".join(scopes))
    page = client.get(response.headers["location"])
    assert ("新建和编辑" in page.text) == (DISCUSSION_WRITE in scopes)
    form = consent_form(client, response)
    settings = client.app.state.provider.settings
    form.update(password=(settings.state_dir / "owner-access.txt").read_text().strip(), decision="allow")
    consent = client.post("/consent", data=form, headers={"Origin": settings.public_url})
    assert consent.status_code == 303
    code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": credentials["client_id"],
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": CALLBACK,
            "resource": settings.resource,
        },
    )
    assert response.status_code == 200, response.text
    return credentials, response.json()


def call(client, bearer, name, **arguments):
    return rpc(client, bearer, "tools/call", {"name": name, "arguments": arguments})["result"]


def test_oauth_old_tokens_cannot_read_write_and_new_consent_works(settings):
    # Obtain a real legacy grant before enabling Discussion, preserve it on restart.
    with TestClient(create_app(settings), base_url=settings.public_url, follow_redirects=False) as client:
        old = token(client)
    settings.discussion_mode = "write"
    app = create_app(settings)
    app.state.catalog.refresh()
    pid = next(iter(app.state.catalog.projects))
    with TestClient(app, base_url=settings.public_url, follow_redirects=False) as client:
        assert asyncio.run(OAuthProvider(settings).verify_token(old["access_token"]))
        for tool in ["list_discussions", "read_discussion", "create_discussion", "edit_discussion"]:
            denied = call(client, old["access_token"], tool, project_id=pid)
            assert denied["isError"] and "mcp/www_authenticate" in denied["_meta"]
        credentials, read = grant(client, [SCOPE, DISCUSSION_READ])
        assert not call(client, read["access_token"], "list_discussions", project_id=pid).get("isError")
        assert call(client, read["access_token"], "create_discussion", project_id=pid)["isError"]
        assert (
            client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": credentials["client_id"],
                    "refresh_token": read["refresh_token"],
                    "resource": settings.resource,
                    "scope": " ".join([SCOPE, DISCUSSION_READ, DISCUSSION_WRITE]),
                },
            ).status_code
            == 400
        )
        _, write = grant(client, [SCOPE, DISCUSSION_READ, DISCUSSION_WRITE])
        created = call(
            client,
            write["access_token"],
            "create_discussion",
            project_id=pid,
            topic="部署方式",
            content="讨论正文",
            description="说明",
            request_id="oauth-create-0001",
        )
        assert not created.get("isError"), created
        doc = created["structuredContent"]
        assert not call(
            client,
            write["access_token"],
            "edit_discussion",
            project_id=pid,
            discussion_id=doc["id"],
            expected_revision=doc["revision"],
            content="修改",
            description="说明",
            request_id="oauth-edit-00001",
        ).get("isError")
        assert (
            client.get(
                "/files/" + pid + "/" + doc["path"],
                headers={"Authorization": "Bearer " + write["access_token"]},
            ).status_code
            == 400
        )
        tools = rpc(client, write["access_token"], "tools/list")["result"]["tools"]
        assert len(tools) == 12
        by_name = {t["name"]: t for t in tools}
        assert not by_name["edit_discussion"]["annotations"]["readOnlyHint"]
        assert by_name["edit_discussion"]["annotations"]["destructiveHint"]
        assert by_name["create_discussion"]["securitySchemes"][0]["scopes"] == [
            SCOPE,
            DISCUSSION_READ,
            DISCUSSION_WRITE,
        ]
        assert not {"delete_discussion", "rename_discussion", "write_file"} & set(by_name)


async def test_stdio_write_tools_use_same_boundary(discussion):
    store, pid = discussion
    store.settings.transport = "stdio"
    mcp = create_mcp(store.settings, store.catalog)
    assert len(await mcp.list_tools()) == 12
    assert all(t.securitySchemes == [{"type": "noauth"}] for t in await mcp.list_tools())
    result = await mcp.call_tool(
        "create_discussion",
        {
            "project_id": pid,
            "topic": "本机讨论",
            "content": "free text",
            "description": "text",
            "request_id": "stdio-create-0001",
        },
    )
    assert "D-000001" in str(result)
    store.settings.discussion_mode = "read"
    readonly = create_mcp(store.settings, store.catalog)
    assert {t.name for t in await readonly.list_tools()} & {"create_discussion", "edit_discussion"} == set()


def test_mode_config_is_opt_in(tmp_path, monkeypatch):
    from oppenproject.config import Settings

    for key in os.environ:
        if key.startswith("OPPEN_"):
            monkeypatch.delenv(key)
    config = tmp_path / "config.local.json"
    assert Settings.load(config).discussion_mode == "off"
    for mode in ["read", "write"]:
        (tmp_path / ".env").write_text("OPPEN_DISCUSSION_MODE=" + mode, encoding="utf-8")
        assert Settings.load(config).discussion_mode == mode
    config.write_text(json.dumps({"discussion_mode": "read"}), encoding="utf-8")
    monkeypatch.setenv("OPPEN_DISCUSSION_MODE", "invalid")
    with pytest.raises(ValueError, match="DISCUSSION_MODE"):
        Settings.load(config)
