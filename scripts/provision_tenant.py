#!/usr/bin/env python3
"""Interactive Phase-4 provisioning; bot tokens are read with hidden input."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Database.connection import connect
from Database.migrate import migrate
from MasterBot.telegram_api import TelegramGetMeVerifier
from Provisioning.handoff import write_secret_handoff
from Provisioning.service import TenantProvisioner
from Shared.crypto import FernetTokenCipher
from Shared.redaction import configure_safe_logging
from Shared.settings import load_from_env


def _positive_id(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Owner Telegram ID must be a positive integer") from exc
    if value <= 0:
        raise ValueError("Owner Telegram ID must be a positive integer")
    return value


async def _run(args: argparse.Namespace) -> Path:
    env_path = Path(args.env_file).resolve()
    load_dotenv(env_path)
    settings = load_from_env()
    database_path = Path(settings.database_path)
    if not database_path.is_absolute():
        database_path = env_path.parent / database_path
    migrate(str(database_path))
    conn = connect(str(database_path))
    try:
        provisioner = TenantProvisioner(
            conn,
            master_admin_id=settings.master_admin_id,
            cipher=FernetTokenCipher(settings.token_encryption_key),
            bot_verifier=TelegramGetMeVerifier(),
        )
        name = input("Tenant name: ").strip()
        slug = input("Tenant slug (a-z, 0-9, hyphen): ").strip()
        owner_id = _positive_id(input("Owner Telegram numeric ID: "))
        admin_token = getpass.getpass("Tenant AdminBot token (hidden): ").strip()
        user_token = getpass.getpass("Tenant UserBot token (hidden): ").strip()
        try:
            result = await provisioner.provision(
                settings.master_admin_id,
                name=name,
                slug=slug,
                owner_telegram_id=owner_id,
                admin_token=admin_token,
                user_token=user_token,
            )
        finally:
            del admin_token
            del user_token
        output_dir = Path(args.handoff_dir)
        if not output_dir.is_absolute():
            output_dir = ROOT / output_dir
        path = write_secret_handoff(result, output_dir)
        print(f"Tenant #{result.tenant_id} provisioned successfully.")
        print(f"One-time webhook handoff written to: {path}")
        print("Configure the webhooks and securely delete that file.")
        return path
    finally:
        conn.close()


def main() -> int:
    configure_safe_logging()
    parser = argparse.ArgumentParser(
        description="Provision one White-Label tenant without exposing bot tokens"
    )
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--handoff-dir", default="runtime/provisioning")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        # Fixed output avoids credentials embedded in third-party exceptions.
        print(f"Provisioning failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
