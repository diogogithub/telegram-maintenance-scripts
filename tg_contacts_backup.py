#!/usr/bin/env python3
"""Export Telegram contacts, full user metadata, and current profile photos."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError


CSV_FIELDS = [
    "id",
    "access_hash",
    "first_name",
    "last_name",
    "display_name",
    "username",
    "usernames",
    "phone",
    "lang_code",
    "status",
    "status_timestamp",
    "contact",
    "mutual_contact",
    "close_friend",
    "bot",
    "verified",
    "premium",
    "restricted",
    "scam",
    "fake",
    "deleted",
    "about",
    "birthday",
    "common_chats_count",
    "photo_id",
    "avatar_path",
    "details_error",
    "avatar_error",
]


def normalize(value: Any) -> Any:
    """Convert Telethon values into JSON-serializable Python values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize(item) for item in value]
    if hasattr(value, "to_dict"):
        return normalize(value.to_dict())
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_name(user: types.User) -> str:
    name = " ".join(
        part.strip()
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if part and part.strip()
    )
    return name or getattr(user, "username", None) or f"user_{user.id}"


def safe_filename(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return (cleaned[:80] or fallback).strip("._") or fallback


def status_fields(user: types.User) -> tuple[str, str]:
    status = getattr(user, "status", None)
    if status is None:
        return "", ""

    timestamp = getattr(status, "expires", None) or getattr(status, "was_online", None)
    return type(status).__name__, normalize(timestamp) if timestamp else ""


def usernames_for(user: types.User) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    primary = getattr(user, "username", None)
    if primary:
        result.append({"username": primary, "active": True, "primary": True})

    for item in getattr(user, "usernames", None) or []:
        entry = normalize(item)
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        if not username:
            continue
        entry["primary"] = username == primary
        if not any(existing.get("username") == username for existing in result):
            result.append(entry)
    return result


async def with_flood_wait(operation, label: str):
    """Run an async Telegram operation, respecting Telegram flood waits."""
    while True:
        try:
            return await operation()
        except FloodWaitError as exc:
            wait_seconds = max(1, int(exc.seconds))
            print(f"[FLOOD WAIT] {label}: sleeping for {wait_seconds}s")
            await asyncio.sleep(wait_seconds)


async def export_contacts(args: argparse.Namespace) -> Path:
    load_dotenv()

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "changeme")
    session_name = os.getenv("TELEGRAM_SESSION_NAME", "telegram_maintenance")
    if not api_id or api_hash == "changeme":
        raise RuntimeError("Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")

    output_dir = Path(args.output_dir or f"telegram_contacts_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    output_dir.mkdir(parents=True, exist_ok=True)
    avatars_dir = output_dir / "avatars"
    if not args.skip_avatars:
        avatars_dir.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(session_name, api_id, api_hash)
    async with client:
        me = await client.get_me()
        contacts_result = await with_flood_wait(
            lambda: client(functions.contacts.GetContactsRequest(hash=0)),
            "fetch contacts",
        )

        users = [user for user in getattr(contacts_result, "users", []) if isinstance(user, types.User)]
        contact_ids = {
            getattr(contact, "user_id", None)
            for contact in getattr(contacts_result, "contacts", [])
        }
        users = [user for user in users if user.id in contact_ids]
        users.sort(key=lambda user: (display_name(user).casefold(), user.id))

        records: list[dict[str, Any]] = []
        total = len(users)
        print(f"Exporting {total} Telegram contacts to {output_dir}")

        for index, user in enumerate(users, start=1):
            name = display_name(user)
            print(f"[{index}/{total}] {name} (id={user.id})")
            status_name, status_timestamp = status_fields(user)
            photo = getattr(user, "photo", None)

            record: dict[str, Any] = {
                "id": user.id,
                "access_hash": getattr(user, "access_hash", None),
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "display_name": name,
                "username": getattr(user, "username", None),
                "usernames": usernames_for(user),
                "phone": getattr(user, "phone", None),
                "lang_code": getattr(user, "lang_code", None),
                "status": status_name,
                "status_timestamp": status_timestamp,
                "contact": bool(getattr(user, "contact", False)),
                "mutual_contact": bool(getattr(user, "mutual_contact", False)),
                "close_friend": bool(getattr(user, "close_friend", False)),
                "bot": bool(getattr(user, "bot", False)),
                "verified": bool(getattr(user, "verified", False)),
                "premium": bool(getattr(user, "premium", False)),
                "restricted": bool(getattr(user, "restricted", False)),
                "scam": bool(getattr(user, "scam", False)),
                "fake": bool(getattr(user, "fake", False)),
                "deleted": bool(getattr(user, "deleted", False)),
                "photo_id": getattr(photo, "photo_id", None),
                "avatar_path": None,
                "details_error": None,
                "avatar_error": None,
                "user": normalize(user),
                "full_user": None,
            }

            if not args.skip_full_details:
                try:
                    full = await with_flood_wait(
                        lambda user=user: client(functions.users.GetFullUserRequest(user)),
                        f"fetch full details for {user.id}",
                    )
                    record["full_user"] = normalize(getattr(full, "full_user", None))
                except Exception as exc:
                    record["details_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"  [WARN] Full details unavailable: {exc}")

            full_user = record.get("full_user") or {}
            if isinstance(full_user, dict):
                record["about"] = full_user.get("about")
                record["birthday"] = full_user.get("birthday")
                record["common_chats_count"] = full_user.get("common_chats_count")
            else:
                record["about"] = None
                record["birthday"] = None
                record["common_chats_count"] = None

            if not args.skip_avatars and photo is not None:
                base = safe_filename(name, f"user_{user.id}")
                requested_path = avatars_dir / f"{user.id}_{base}.jpg"
                try:
                    downloaded = await with_flood_wait(
                        lambda user=user, requested_path=requested_path: client.download_profile_photo(
                            user,
                            file=str(requested_path),
                            download_big=not args.small_avatars,
                        ),
                        f"download avatar for {user.id}",
                    )
                    if downloaded:
                        record["avatar_path"] = Path(os.path.relpath(downloaded, output_dir)).as_posix()
                except Exception as exc:
                    record["avatar_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"  [WARN] Avatar unavailable: {exc}")

            records.append(record)

    payload = {
        "schema_version": 1,
        "exported_at": utc_now(),
        "account": {
            "id": me.id,
            "username": getattr(me, "username", None),
            "display_name": display_name(me),
        },
        "contact_count": len(records),
        "contacts": records,
    }

    json_path = output_dir / "contacts.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    csv_path = output_dir / "contacts.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            row = {field: record.get(field) for field in CSV_FIELDS}
            row["usernames"] = json.dumps(row["usernames"], ensure_ascii=False)
            if isinstance(row["birthday"], (dict, list)):
                row["birthday"] = json.dumps(row["birthday"], ensure_ascii=False)
            writer.writerow(row)

    manifest = {
        "schema_version": 1,
        "exported_at": payload["exported_at"],
        "contact_count": len(records),
        "files": {
            "json": json_path.name,
            "csv": csv_path.name,
            "avatars": None if args.skip_avatars else avatars_dir.name,
        },
        "options": {
            "full_details": not args.skip_full_details,
            "avatars": not args.skip_avatars,
            "avatar_size": "small" if args.small_avatars else "large",
        },
    }
    with (output_dir / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    print(f"Done. Exported {len(records)} contacts.")
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")
    return output_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Telegram contacts, full user details, and current avatars.",
    )
    parser.add_argument(
        "--output-dir",
        help="Destination directory (default: telegram_contacts_<timestamp>)",
    )
    parser.add_argument(
        "--skip-full-details",
        action="store_true",
        help="Skip users.GetFullUserRequest and only export base contact data",
    )
    parser.add_argument(
        "--skip-avatars",
        action="store_true",
        help="Do not download current profile photos",
    )
    parser.add_argument(
        "--small-avatars",
        action="store_true",
        help="Download the small current profile photo instead of the large one",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(export_contacts(args))


if __name__ == "__main__":
    main()
