#!/usr/bin/env python3
"""Configure, inspect and serve OppenProject. Use the project's Python virtual environment."""

import argparse
import json
import os
from dataclasses import asdict

import uvicorn

from oppenproject.auth import Store, configure_owner
from oppenproject.catalog import Catalog
from oppenproject.config import ROOT, Settings
from oppenproject.server import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    setup = sub.add_parser("configure")
    setup.add_argument("--public-url", required=True)
    setup.add_argument("--port", type=int, default=8766)
    setup.add_argument("--scan-root", action="append")
    setup.add_argument("--exclude-root", action="append")
    sub.add_parser("serve")
    sub.add_parser("scan")
    sub.add_parser("rotate-password")
    sub.add_parser("revoke-all")
    args = parser.parse_args()
    config_path = ROOT / "config.local.json"
    if args.command == "configure":
        values = json.loads(config_path.read_text()) if config_path.exists() else {}
        values.update(public_url=args.public_url, port=args.port)
        if args.scan_root:
            values["scan_roots"] = args.scan_root
        if args.exclude_root:
            values["exclude_roots"] = args.exclude_root
        settings = Settings(**values)
        config = asdict(settings)
        config["state_dir"], config["skill_root"] = str(settings.state_dir), str(settings.skill_root)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        config_path.chmod(0o600)
        configure_owner(settings)
        print(f"Configured {settings.resource}; forward to {settings.host}:{settings.port}")
        print(f"Owner login passphrase: {settings.state_dir / 'owner-access.txt'} (local file only)")
        return
    settings = Settings.load()
    if args.command == "scan":
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
    main()
