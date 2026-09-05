#!/usr/bin/env python3
"""Configure, inspect and serve OppenSteward-MCP on Windows, macOS and Linux."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, replace

import uvicorn

from oppenproject.auth import Store, configure_owner
from oppenproject.catalog import Catalog
from oppenproject.config import ROOT, Settings
from oppenproject.server import create_app, create_mcp
from oppenproject.tunnel import tunnel_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=str, help="Legacy JSON location; .env is read beside it")
    sub = parser.add_subparsers(dest="command")
    setup = sub.add_parser("configure")
    setup.add_argument("--public-url")
    setup.add_argument("--transport", choices=["http", "stdio"])
    setup.add_argument("--port", type=int)
    setup.add_argument("--scan-root", action="append")
    setup.add_argument("--exclude-root", action="append")
    serve = sub.add_parser("serve")
    serve.add_argument("--transport", choices=["http", "stdio"])
    sub.add_parser("setup", help="Validate .env and initialize OAuth credentials only in HTTP mode")
    tunnel = sub.add_parser("tunnel", help="Use the official tunnel-client with this server over stdio")
    tunnel.add_argument("action", choices=["init", "doctor", "run"])
    tunnel.add_argument("--dry-run", action="store_true")
    sub.add_parser("scan")
    sub.add_parser("rotate-password")
    sub.add_parser("revoke-all")
    args = parser.parse_args()
    from pathlib import Path

    config_path = Path(args.config).expanduser().resolve() if args.config else ROOT / "config.local.json"
    if args.command == "configure":
        values = asdict(Settings.load(config_path))
        for name in ("public_url", "transport", "port"):
            if getattr(args, name) is not None:
                values[name] = getattr(args, name)
        if args.public_url and not args.transport:
            values["transport"] = "http"
        if args.scan_root:
            values["scan_roots"] = args.scan_root
        if args.exclude_root:
            values["exclude_roots"] = args.exclude_root
        settings = Settings(**values)
        config = asdict(settings)
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        config_path.chmod(0o600)
        if settings.transport == "http":
            configure_owner(settings)
            print(f"Configured {settings.resource}; forward to {settings.host}:{settings.port}")
            print(f"Owner login passphrase: {settings.state_dir / 'owner-access.txt'} (local file only)")
        else:
            print("Configured stdio; use tunnel-client or a local MCP client to launch the server.")
        return
    settings = Settings.load(config_path)
    if getattr(args, "transport", None):
        settings = replace(settings, transport=args.transport)
    if args.command == "tunnel":
        return tunnel_command(settings, args.action, config_path, dry_run=args.dry_run)
    if args.command == "setup":
        if settings.transport == "http":
            configure_owner(settings)
            print(f"OAuth ready. Login passphrase file: {settings.state_dir / 'owner-access.txt'}")
        else:
            print("stdio ready. No HTTP listener or app-level OAuth credential is created.")
    elif args.command == "scan":
        catalog = Catalog(settings)
        report = catalog.refresh()
        print(
            json.dumps(
                {"discovery": report, "projects": [p.public() for p in catalog.projects.values()]},
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "rotate-password":
        configure_owner(settings, rotate=True)
        print("Password rotated and all authorizations revoked. Read .runtime/owner-access.txt locally.")
    elif args.command == "revoke-all":
        Store(settings).revoke_all()
        print("All OAuth clients, pending authorizations and tokens revoked.")
    else:
        os.umask(0o077)
        if settings.transport == "stdio":
            catalog = Catalog(settings)
            asyncio.run(create_mcp(settings, catalog).run_stdio_async())
            return
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            access_log=False,
            proxy_headers=False,
            timeout_keep_alive=15,
            limit_concurrency=64,
        )


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
