import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from oppenproject.catalog import STEWARD, AccessDenied, Catalog
from oppenproject.discussion import Discussions
from oppenproject.server import discovery_lifespan

from .conftest import register_paths, rpc


def make_project(path):
    (path / ".oppen-project-steward").mkdir(parents=True)
    (path / ".oppen-project-steward/registry.md").write_text(STEWARD, encoding="utf-8")
    return path


def test_only_explicit_roots_no_directory_enumeration(settings, monkeypatch):
    parent = settings.projects_file.parent / "projects"
    hidden = make_project(parent / "not-registered")
    register_paths(settings, parent, replace=True)
    monkeypatch.setattr(os, "scandir", lambda *a, **k: pytest.fail("Project discovery must never enumerate"))
    catalog = Catalog(settings)
    assert catalog.refresh()["projects_found"] == 0
    register_paths(settings, hidden)
    assert catalog.refresh()["projects_found"] == 1
    assert next(iter(catalog.projects.values())).root == str(hidden)


def test_unchanged_config_never_reopens_projects_or_body(settings):
    catalog = Catalog(settings)
    catalog.refresh()
    with (
        patch("oppenproject.catalog.identify", side_effect=AssertionError("Repeated project access")),
        patch.object(catalog, "registered_roots", side_effect=AssertionError("Repeated config read")),
    ):
        for _ in range(20):
            assert catalog.refresh()["projects_found"] == 1


def test_atomic_replacement_relative_paths_empty_list_and_removal(settings):
    catalog = Catalog(settings)
    catalog.refresh()
    original = next(iter(catalog.projects.values()))
    second = make_project(settings.projects_file.parent / "第二个项目")
    new_file = settings.projects_file.with_name("replacement.json")
    new_file.write_text(json.dumps({"projects": ["第二个项目", "第二个项目"]}), encoding="utf-8")
    new_file.replace(settings.projects_file)
    with pytest.raises(AccessDenied):
        catalog.read_file(original.id, original.registry)
    assert len(catalog.projects) == 1
    assert next(iter(catalog.projects.values())).root == str(second)
    register_paths(settings, replace=True)
    assert catalog.search("anything")["results"] == []
    assert catalog.report["status"] == "ready" and not catalog.projects


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"projects":[1]}',
        '{"projects":[""]}',
        '{"projects":[],"extra":1}',
        '{"projects":[],"projects":[]}',
        '{"projects":["bad\\u0000path"]}',
        "\ud800",
    ],
)
def test_invalid_config_revokes_access_and_recovers(settings, content):
    catalog = Catalog(settings)
    catalog.refresh()
    old = next(iter(catalog.projects.values()))
    settings.projects_file.write_bytes(content.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(AccessDenied):
        catalog.read_file(old.id, old.registry)
    assert catalog.report["status"] == "config_error" and not catalog.projects
    register_paths(settings, old.root, replace=True)
    assert catalog.read_file(old.id, old.registry)["content"]


def test_missing_oversize_and_unreadable_config_revoke_access(settings):
    catalog = Catalog(settings)
    catalog.refresh()
    old = next(iter(catalog.projects.values()))
    settings.projects_file.unlink()
    with pytest.raises(AccessDenied):
        catalog.project(old.id)
    assert catalog.report["status"] == "config_missing"
    settings.projects_file.write_bytes(b" " * 1_048_577)
    assert catalog.refresh()["status"] == "config_error"
    register_paths(settings, old.root, replace=True)
    assert catalog.refresh()["projects_found"] == 1
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path == settings.projects_file:
            raise PermissionError("fixture")
        return original(path, *args, **kwargs)

    with patch.object(Path, "lstat", denied):
        assert catalog.refresh()["status"] == "config_error" and not catalog.projects


def test_project_limit_and_missing_roots_only_retry_on_request(settings):
    catalog = Catalog(settings)
    absent = settings.projects_file.parent / "later"
    register_paths(settings, absent, replace=True)
    assert catalog.refresh()["status"] == "partial"
    make_project(absent)
    assert not catalog.refresh()["projects_found"]
    assert catalog.refresh(force=True)["projects_found"] == 1
    register_paths(settings, *[str(absent)] * 1001, replace=True)
    assert catalog.refresh()["status"] == "config_error" and not catalog.projects


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_registration_file_must_be_ordinary(settings, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are POSIX-only")
    catalog = Catalog(settings)
    catalog.refresh()
    source = settings.projects_file.with_name("source.json")
    settings.projects_file.rename(source)
    if kind == "symlink":
        try:
            settings.projects_file.symlink_to(source)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                pytest.skip("Windows requires permission to create symlinks")
            raise
    elif kind == "hardlink":
        os.link(source, settings.projects_file)
    else:
        os.mkfifo(settings.projects_file)
    assert catalog.refresh()["status"] == "config_error" and not catalog.projects


def test_reload_race_and_recursive_json_fail_closed(settings):
    catalog = Catalog(settings)
    from oppenproject.catalog import identify

    def replace_during_load(path):
        project = identify(path)
        register_paths(settings, replace=True)
        return project

    with patch("oppenproject.catalog.identify", replace_during_load):
        assert catalog.refresh()["status"] == "config_error" and not catalog.projects
    assert catalog.refresh()["status"] == "ready"
    settings.projects_file.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
    assert catalog.refresh()["status"] == "config_error" and not catalog.projects


def test_http_removal_denies_old_ids_downloads_and_discussion(settings):
    from starlette.testclient import TestClient

    from oppenproject.auth import DISCUSSION_READ, DISCUSSION_WRITE, SCOPE
    from oppenproject.server import create_app

    from .test_discussion import grant

    settings.discussion_mode = "write"
    with TestClient(create_app(settings), base_url=settings.public_url, follow_redirects=False) as client:
        _, access = grant(client, [SCOPE, DISCUSSION_READ, DISCUSSION_WRITE])
        bearer = access["access_token"]
        catalog = client.app.state.catalog
        project = next(iter(catalog.projects.values()))
        store = Discussions(catalog)
        doc = store.write(
            project.id,
            topic="讨论",
            content="unchanged",
            description="fixture",
            request_id="registration-create-1",
        )
        register_paths(settings, replace=True)
        calls = [
            ("project_overview", {"project_id": project.id}),
            ("read_file", {"project_id": project.id, "path": project.registry}),
            ("list_discussions", {"project_id": project.id}),
            (
                "create_discussion",
                {
                    "project_id": project.id,
                    "topic": "讨论",
                    "content": "no",
                    "description": "fixture",
                    "request_id": "registration-create-2",
                },
            ),
            (
                "edit_discussion",
                {
                    "project_id": project.id,
                    "discussion_id": doc["id"],
                    "content": "no",
                    "description": "fixture",
                    "expected_revision": doc["revision"],
                    "request_id": "registration-edit-1",
                },
            ),
        ]
        for name, arguments in calls:
            assert rpc(client, bearer, "tools/call", {"name": name, "arguments": arguments})["result"][
                "isError"
            ]
        result = rpc(client, bearer, "tools/call", {"name": "list_projects"})["result"]["structuredContent"]
        assert result["projects"] == [] and result["discovery"]["mode"] == "registered"
        assert (
            client.get(
                f"/files/{project.id}/{project.registry}", headers={"Authorization": "Bearer " + bearer}
            ).status_code
            == 400
        )
        assert (Path(project.root) / doc["path"]).read_text(encoding="utf-8") == "unchanged"


async def test_background_reload_without_client_requests(settings):
    catalog = Catalog(settings)
    async with discovery_lifespan(catalog):
        assert len(catalog.projects) == 1
        register_paths(settings, replace=True)
        async with asyncio.timeout(5):
            while catalog.projects:
                await asyncio.sleep(0.05)
        assert catalog.report["status"] == "ready"
