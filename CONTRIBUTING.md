# Contributing

Please use an issue or pull request for a concrete bug or change. Include the
operating system, Python version, transport, expected behavior and a minimal
reproduction using synthetic files. Never include a real `.env`, owner password,
OAuth database, project data or runtime key.

The current behavioral definitions are registered README sections; update the
existing owner when changing a contract. The governance-only allowlist applies
to every transport and read surface. Add regression coverage for a changed
authorization, path or transport boundary. External skill sources are not
vendored; use your own installed skill to refresh Steward audits and registries.

## Local checks

```sh
uv sync --locked --dev
uv run ruff check .
uv run pytest -q
```

Run from the repository root. Tests use temporary projects and temporary OAuth
credentials. Test configuration must not inherit a deployment's `OPPEN_*` values.
Windows tests need a local filesystem supporting file IDs, hard links and ACLs;
symlink tests may require Developer Mode or permission to create symlinks.
Platform-specific tests skip with an explicit reason when unavailable. A skip
is not evidence of platform verification. Only macOS has been tested; Windows
and Linux remain unverified until their actual test results are recorded.

## Optional browser regression

Install Node.js and Playwright outside this repository or into an ignored local
directory. For example, run `npm install --prefix .runtime/browser playwright`
then `npx --prefix .runtime/browser playwright install chromium`.

On macOS/Linux:

```sh
OPPENPROJECT_PLAYWRIGHT="$PWD/.runtime/browser/node_modules/playwright" uv run pytest tests/test_browser.py -q
```

On Windows PowerShell:

```powershell
$env:OPPENPROJECT_PLAYWRIGHT = "$PWD/.runtime/browser/node_modules/playwright"
uv run pytest tests/test_browser.py -q
```

The browser test starts two temporary loopback servers and runs the registered
HTTP protocol verifier. It does not contact ChatGPT or use a deployed owner's
password. The real Secure MCP Tunnel account connection needs an independently
authorized workspace and runtime key; local stdio tests cannot verify it.

## Release review

Verify the lockfile, README platform status, current contract audits, and ignored
deployment state before publishing. Git owns release history; do not copy old
outputs into dated folders. Keep the existing `v0.1.0` freeze intact. Public
identity is OppenSteward-MCP; the internal `oppenproject` module and existing
macOS service label remain stable for compatibility.
