import base64
import os
from pathlib import Path

import pytest

from oppenproject.catalog import STEWARD, AccessDenied, Catalog, open_beneath

from .conftest import rpc, token

MEMORY_ENTRY = ".oppen-project-steward/Memory/entries/M-0001.md"


@pytest.fixture
def catalog(settings):
    catalog = Catalog(settings)
    assert catalog.refresh()["projects_found"] == 1
    return catalog


def first(catalog):
    return next(iter(catalog.projects.values()))


def test_discovers_both_skills_nested_legacy_and_excludes_fake_markers(catalog, settings):
    root = Path(settings.scan_roots[0])
    for name, marker in [
        ("r-v3", "<!-- stepwise-r-project:v3 -->"),
        ("r-v2", "<!-- stepwise-r-project:v2 -->"),
        ("legacy", STEWARD),
        ("fake", "In documentation: " + STEWARD),
    ]:
        project = root / name
        project.mkdir()
        (project / "project.md").write_text(marker, encoding="utf-8")
    nested = Path(first(catalog).root) / "nested"
    nested.mkdir()
    (nested / "project.md").write_text("<!-- stepwise-r-project:v3 -->", encoding="utf-8")
    catalog.refresh()
    assert len(catalog.projects) == 5
    assert {p.version for p in catalog.projects.values()} == {"v3", "v2", "legacy-layout"}
    assert "fake" not in {p.name for p in catalog.projects.values()}


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        "a/../../secret",
        "a\\..\\x",
        "a\x00b",
        "C:/Windows/win.ini",
        "project.md:private-stream",
        ".env",
        ".env.production",
        ".git/config",
        ".runtime/oauth.sqlite3",
        "private.pem",
        "config.local.json",
        ".aws/credentials",
    ],
)
def test_rejects_escape_and_credential_paths(catalog, path):
    with pytest.raises(AccessDenied):
        catalog.read_file(first(catalog).id, path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_links_and_special_files(catalog, tmp_path, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are POSIX-only")
    project = first(catalog)
    root = Path(project.root)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    # Use an ALLOWED path so these checks exercise descriptor safety, not just the allowlist.
    entry = root / MEMORY_ENTRY
    entry.unlink()
    if kind == "symlink":
        try:
            entry.symlink_to(outside)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                pytest.skip("Windows requires permission to create symlinks")
            raise
    elif kind == "hardlink":
        os.link(outside, entry)
    else:
        os.mkfifo(entry)
    with pytest.raises(AccessDenied):
        catalog.read_file(project.id, MEMORY_ENTRY)
    assert not catalog.list_files(project.id, ".oppen-project-steward/Memory/entries")["entries"]


def test_root_replacement(catalog):
    project = first(catalog)
    root = Path(project.root)
    moved = root.with_name("moved")
    root.rename(moved)
    root.mkdir()
    (root / ".oppen-project-steward").mkdir()
    (root / ".oppen-project-steward/registry.md").write_text(STEWARD, encoding="utf-8")
    with pytest.raises(AccessDenied):
        catalog.read_file(project.id, project.registry)
    with (
        pytest.raises(AccessDenied),
        open_beneath(root, directory=True, expected_root=(project.device, project.inode)),
    ):
        pass


def test_parent_symlink(catalog, tmp_path):
    project = first(catalog)
    root = Path(project.root)
    entry = root / MEMORY_ENTRY
    entry.unlink()
    entry.parent.rmdir()
    try:
        entry.parent.symlink_to(tmp_path, target_is_directory=True)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows requires permission to create symlinks")
        raise
    with pytest.raises(AccessDenied):
        catalog.read_file(project.id, MEMORY_ENTRY)


def test_large_governance_text_chunks_are_exact_and_search_is_bounded(catalog):
    project = first(catalog)
    assert "alpha" in catalog.fetch(catalog.search("alpha")["results"][0]["id"])["content"]
    data = ("# Consequential decision\n治理正文\n" * 24000).encode()
    (Path(project.root) / MEMORY_ENTRY).write_bytes(data)
    chunks, offset = [], 0
    while offset is not None:
        result = catalog.read_file(project.id, MEMORY_ENTRY, offset, 262144, "base64")
        chunks.append(base64.b64decode(result["content"]))
        offset = result["next_offset"]
    assert b"".join(chunks) == data
    assert catalog.search("M-0001.md")["results"]
    assert catalog.search(".md", limit=1)["truncated"]
    assert not catalog.search("治理正文")["results"]  # Large content is not searched.


def test_removed_marker_and_excluded_root_fail_closed(catalog, settings):
    project = first(catalog)
    marker = Path(project.root) / project.registry
    marker.unlink()
    with pytest.raises(AccessDenied):
        catalog.list_files(project.id)
    marker.write_text(STEWARD, encoding="utf-8")
    settings.exclude_roots = [project.root]
    with pytest.raises(AccessDenied):
        catalog.read_file(project.id, "notes.md")
    assert catalog.refresh()["projects_found"] == 0


def test_partial_scan_is_reported(catalog, settings):
    settings.max_scan_dirs = 1
    assert catalog.refresh()["status"] == "partial"
    assert catalog.report["bounded"]
    while catalog.pending:
        catalog.refresh()
    assert catalog.report["status"] == "complete"
    assert len(catalog.projects) == 1


def test_end_to_end_mcp_search_fetch_and_path_denial(client):
    bearer = token(client)["access_token"]
    result = rpc(client, bearer, "tools/call", {"name": "search", "arguments": {"query": "alpha"}})["result"]
    assert not result.get("isError"), result
    found = result["structuredContent"]["results"][0]
    fetched = rpc(client, bearer, "tools/call", {"name": "fetch", "arguments": {"id": found["id"]}})["result"]
    assert "检索 alpha" in fetched["structuredContent"]["text"]
    denied = rpc(
        client,
        bearer,
        "tools/call",
        {"name": "read_file", "arguments": {"project_id": found["project_id"], "path": "../outside"}},
    )["result"]
    assert denied["isError"]
    response = client.get(found["url"], headers={"Authorization": "Bearer " + bearer})
    assert response.status_code == 200
    assert "检索 alpha" in response.json()["content"]


@pytest.mark.parametrize("layout", ["steward", "legacy", "r-v3", "r-v2"])
def test_governance_allowlist_for_all_layouts(settings, layout):
    root = Path(settings.scan_roots[0]) / layout
    root.mkdir()
    skill = "stepwise-r-project" if layout.startswith("r-") else "oppen-project-steward"
    prefix = ".oppen-project-steward/" if layout == "steward" else ""
    registry = prefix + "registry.md" if prefix else "project.md"
    (root / registry).parent.mkdir(exist_ok=True)
    marker = f"<!-- {skill}:v{'2' if layout == 'r-v2' else '3'} -->"
    (root / registry).write_text(marker + "\n[Referenced data](Data/raw.csv)\n", encoding="utf-8")
    allowed = {registry}
    for system, letter, title in [("Memory", "M", "Decision Memory"), ("Attention", "A", "Human Attention")]:
        index = prefix + system + "/index.md"
        entry = prefix + system + f"/entries/{letter}-0001.md"
        (root / entry).parent.mkdir(parents=True)
        header = f"# {title}\n" if layout.startswith("r-") else f"<!-- {skill}:{system.lower()}-index -->\n"
        (root / index).write_text(
            header + f"| {letter}-0001 | decision | entries/{letter}-0001.md |\n", encoding="utf-8"
        )
        (root / entry).write_text("GOVERNANCE_DECISION_CONTENT", encoding="utf-8")
        if layout != "r-v2":
            allowed.update({index, entry})

    blocked = [
        "Data/raw.csv",
        "data/patients.csv",
        "R/analysis.R",
        "src/main.py",
        "Results/table.md",
        "Deliverables/report.pdf",
        "README.md",
        "Definitions/cohort.md",
        "notes.md",
        "photo.png",
        "data.rds",
        prefix + "Audit/Runs/stage/current/result.csv",
        prefix + "Audit/Functions/audit_model.Rmd",
        prefix + "Audit/Contracts/audit_contract.md",
        prefix + ".managed-state.json",
        prefix + "Memory/entries/M-9999.md",
        prefix + "Memory/raw.csv",
        prefix + "Memory/summary.md",
        prefix + "Attention/entries/A-9999.md",
        prefix + "Attention/entries/A-0001.csv",
    ]
    if layout == "steward":
        blocked += ["project.md", "Memory/index.md", "Attention/index.md"]
    for path in blocked:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("PRIVATE_DATA_NEVER_EXPOSE", encoding="utf-8")
    if layout == "r-v2":
        blocked += ["Memory/index.md", "Memory/entries/M-0001.md", "Attention/index.md"]
    catalog = Catalog(settings)
    catalog.refresh()
    project = next(p for p in catalog.projects.values() if p.root == str(root))
    seen, queue = set(), ["."]
    while queue:
        for entry in catalog.list_files(project.id, queue.pop())["entries"]:
            if entry["kind"] == "directory":
                assert "size" not in entry and "modified" not in entry
                queue.append(entry["path"])
            else:
                seen.add(entry["path"])
                assert catalog.read_file(project.id, entry["path"])["content"]
    assert seen == allowed
    for path in blocked:
        file_id = project.id + ":" + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
        for encoding in ["utf-8", "base64"]:
            with pytest.raises(AccessDenied):
                catalog.read_file(project.id, path, encoding=encoding)
        with pytest.raises(AccessDenied):
            catalog.fetch(file_id)
    for directory in ["Data", "R", "Results", prefix + "Audit", prefix + "Audit/Contracts"]:
        with pytest.raises(AccessDenied):
            catalog.list_files(project.id, directory)
    assert not catalog.search("PRIVATE_DATA_NEVER_EXPOSE", project.id)["results"]
    assert not catalog.search("raw.csv", project.id, glob="Data/*")["results"]


def test_index_changes_and_malformed_indices_fail_closed(catalog):
    project = first(catalog)
    index = Path(project.root) / ".oppen-project-steward/Memory/index.md"
    assert catalog.read_file(project.id, MEMORY_ENTRY)
    index.write_text("<!-- oppen-project-steward:memory-index -->\n_None registered._\n", encoding="utf-8")
    with pytest.raises(AccessDenied):
        catalog.read_file(project.id, MEMORY_ENTRY)
    index.write_text("PRIVATE_DATA_WITHOUT_A_GOVERNANCE_MARKER", encoding="utf-8")
    with pytest.raises(AccessDenied):
        catalog.read_file(project.id, ".oppen-project-steward/Memory/index.md")
    assert not catalog.search("PRIVATE_DATA_WITHOUT_A_GOVERNANCE_MARKER")["results"]


def test_authenticated_tools_and_downloads_cannot_bypass_allowlist(client):
    bearer = token(client)["access_token"]
    catalog = client.app.state.catalog
    project = first(catalog)
    headers = {"Authorization": "Bearer " + bearer}
    blocked = ["notes.md", "Data/raw.csv", ".oppen-project-steward/Audit/Contracts/audit_file-access.md"]
    for path in blocked:
        target = Path(project.root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("MUST_NOT_REACH_GPT", encoding="utf-8")
        file_id = project.id + ":" + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
        for name, arguments in [
            ("read_file", {"project_id": project.id, "path": path}),
            ("read_file", {"project_id": project.id, "path": path, "encoding": "base64", "offset": 1}),
            ("fetch", {"id": file_id}),
        ]:
            result = rpc(client, bearer, "tools/call", {"name": name, "arguments": arguments})["result"]
            assert result["isError"] and "MUST_NOT_REACH_GPT" not in str(result)
        response = client.get(f"/files/{project.id}/{path}?encoding=base64", headers=headers)
        assert response.status_code == 400 and "MUST_NOT_REACH_GPT" not in response.text
    for name, arguments in [
        ("search", {"query": "MUST_NOT_REACH_GPT"}),
        ("list_files", {"project_id": project.id}),
        ("project_overview", {"project_id": project.id}),
    ]:
        result = rpc(client, bearer, "tools/call", {"name": name, "arguments": arguments})["result"]
        assert not result.get("isError") and "MUST_NOT_REACH_GPT" not in str(result)
    catalog.report["errors"] = [{"path": "/private/data/patient-name", "error": "unreadable"}]
    result = rpc(client, bearer, "tools/call", {"name": "list_projects"})["result"]
    assert "patient-name" not in str(result)
    assert "errors" not in result["structuredContent"]["discovery"]
