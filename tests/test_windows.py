"""Native Windows regressions. Skipped elsewhere; never counted as macOS validation."""

import os
import subprocess
from pathlib import Path

import pytest

from oppenproject.catalog import AccessDenied, Catalog, open_beneath

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires native Windows file handles and ACLs")


def test_junction_is_not_discovered_or_read(settings, tmp_path):
    root = Path(settings.scan_roots[0])
    project = next(root.iterdir())
    original = project / ".oppen-project-steward/Memory/entries"
    (original / "M-0001.md").unlink()
    original.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "M-0001.md").write_text("PRIVATE_CONTENT", encoding="utf-8")
    subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(original), str(outside)], check=True, capture_output=True
    )
    try:
        catalog = Catalog(settings)
        catalog.refresh()
        selected = next(iter(catalog.projects.values()))
        with pytest.raises(AccessDenied):
            catalog.read_file(selected.id, ".oppen-project-steward/Memory/entries/M-0001.md")
        assert not catalog.search("PRIVATE_CONTENT")["results"]
    finally:
        original.rmdir()  # Remove the junction itself, not its target.


def test_ancestor_and_file_cannot_be_replaced_during_read(settings):
    project = next(Path(settings.scan_roots[0]).iterdir())
    with open_beneath(project, ".oppen-project-steward/registry.md") as fd:
        assert os.read(fd, 1024)
        with pytest.raises(OSError):
            project.rename(project.with_name("replaced"))
        with pytest.raises(OSError):
            (project / ".oppen-project-steward/registry.md").write_text("changed", encoding="utf-8")


def test_runtime_dacl_only_grants_current_user(settings):
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        current = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    for path in [
        settings.state_dir,
        settings.state_dir / "owner-access.txt",
        settings.state_dir / "oauth.sqlite3",
    ]:
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        acl = descriptor.GetSecurityDescriptorDacl()
        assert acl.GetAceCount() == 1
        assert acl.GetAce(0)[2] == current
