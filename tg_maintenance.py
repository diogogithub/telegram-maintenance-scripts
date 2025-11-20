#!/usr/bin/env python3
"""
Telegram Maintenance Suite (backup + cleanup)

Subcommands:

  index-dialogs       -> Export CSV listing of all dialogs
  backup-remaining    -> Backup all remaining dialogs (except Saved Messages)
  backup-saved        -> Backup Saved Messages (optionally with media)
  count-files         -> Count media files per chat in an archive & write meta.json
  chat-to-html        -> Render a backed up chat as a standalone HTML file
  cleanup-interactive -> Interactive cleanup of old, low-file-count chats
  retry-failed        -> Retry deletions from cleanup_failed_deletes.json
  force-delete-ghosts -> Force-delete ghost/stuck chats by id

All commands share the same Telethon session & .env config.
"""

import argparse
import csv
import glob
import html
import json
import os
import re
import time
from datetime import datetime, date, timezone

from dotenv import load_dotenv
import pandas as pd
from telethon import TelegramClient, types
from telethon.tl import functions
from telethon.errors import UserNotParticipantError, FloodWaitError


# ======================================================================
# ENV + CLIENT
# ======================================================================

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "changeme")
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "telegram_maintenance")

if not API_ID or API_HASH == "changeme":
    raise RuntimeError("Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


# ======================================================================
# COMMON HELPERS
# ======================================================================

def safe_name(title: str, chat_id: int) -> str:
    """Filesystem-safe folder name"""
    if not title:
        title = f"chat_{chat_id}"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", title)
    return f"{name[:60]}_{chat_id}"


def normalize(obj):
    """
    Recursively convert objects so they are JSON-serializable:
    - datetime -> ISO string
    - bytes -> hex string
    - dict/list -> walk recursively
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalize(v) for v in obj]
    return obj


def find_latest_dialog_csv(pattern="telegram_dialog_index_*.csv") -> str | None:
    """Return most recent dialog index CSV matching pattern, or None."""
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def find_chat_folder_by_id(archive_root: str, chat_id: int) -> str:
    """
    Given an archive root and a chat id, find the per-chat folder created by safe_name().
    We match folders whose name ends with _<chat_id>.
    """
    suffix = f"_{chat_id}"
    candidates = [
        d
        for d in os.listdir(archive_root)
        if d.endswith(suffix) and os.path.isdir(os.path.join(archive_root, d))
    ]
    if not candidates:
        raise RuntimeError(f"No chat folder ending with '{suffix}' found in {archive_root}")
    if len(candidates) > 1:
        print(f"[WARN] Multiple chat folders match id={chat_id}, using first: {candidates[0]}")
    return os.path.join(archive_root, candidates[0])


# ======================================================================
# COMMAND: index-dialogs
# ======================================================================

async def cmd_index_dialogs(args):
    """Export a CSV index of all dialogs (similar to your phase1)."""
    filename = args.output
    if filename is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"telegram_dialog_index_{stamp}.csv"

    dialogs = await client.get_dialogs()

    fieldnames = [
        "title",
        "chat_type",
        "id",
        "access_hash",
        "is_me",
        "is_bot",
        "is_private",
        "is_admin",
        "invite_link",
        "members_count",
        "last_message_date",
    ]

    print(f"Writing dialog index to {filename}")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for d in dialogs:
            entity = d.entity
            row = {k: "" for k in fieldnames}

            # Title
            if hasattr(entity, "title") and entity.title:
                title = entity.title
            elif isinstance(entity, types.User):
                title = entity.first_name or entity.username or f"user_{entity.id}"
            else:
                title = f"chat_{entity.id}"
            row["title"] = title

            # ID / access hash
            row["id"] = getattr(entity, "id", "")
            row["access_hash"] = getattr(entity, "access_hash", "")

            # Type
            if isinstance(entity, types.ChatForbidden):
                chat_type = "ChatForbidden"
            elif isinstance(entity, types.User):
                chat_type = "dm"
            elif isinstance(entity, types.Chat):
                chat_type = "group"
            elif isinstance(entity, types.Channel):
                chat_type = "supergroup" if entity.megagroup else "channel"
            else:
                chat_type = type(entity).__name__
            row["chat_type"] = chat_type

            # Flags
            is_me = isinstance(entity, types.User) and bool(getattr(entity, "is_self", False))
            is_bot = isinstance(entity, types.User) and bool(getattr(entity, "bot", False))
            is_private = isinstance(entity, types.User) or (
                isinstance(entity, types.Channel) and not getattr(entity, "username", None)
            )
            is_admin = False
            if isinstance(entity, (types.Chat, types.Channel)):
                is_admin = bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))

            row["is_me"] = is_me
            row["is_bot"] = is_bot
            row["is_private"] = is_private
            row["is_admin"] = is_admin

            # Invite link (public username -> t.me/username)
            invite_link = ""
            if isinstance(entity, (types.Chat, types.Channel)) and getattr(entity, "username", None):
                invite_link = f"https://t.me/{entity.username}"
            row["invite_link"] = invite_link

            # Members count (best-effort)
            members_count = ""
            try:
                if isinstance(entity, types.Channel):
                    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
                    members_count = getattr(full.full_chat, "participants_count", "")
                elif isinstance(entity, types.Chat):
                    full = await client(functions.messages.GetFullChatRequest(chat_id=entity.id))
                    members_count = getattr(full.full_chat, "participants_count", "")
            except Exception:
                pass
            row["members_count"] = members_count

            # Last message date
            last_msg = d.message
            if last_msg is not None and last_msg.date is not None:
                dt = last_msg.date
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                row["last_message_date"] = dt.isoformat()
            else:
                row["last_message_date"] = ""

            writer.writerow(row)

    print("Done.")


# ======================================================================
# COMMAND: backup-remaining / backup-saved
# ======================================================================

async def backup_dialog(entity, title: str, archive_dir: str, *, download_media: bool = False, media_subdir: str = "media"):
    """
    Backup a single dialog into archive_dir / <safe_folder>.

    - Always writes messages.jsonl (normalized Telethon messages).
    - If download_media=True, also downloads media into a media/ subfolder and
      annotates each JSON line with a "_local_media" relative path when present.
    """
    chat_id = entity.id
    folder_name = safe_name(title, chat_id)
    folder_path = os.path.join(archive_dir, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    meta_path = os.path.join(folder_path, "meta.json")
    export_file = os.path.join(folder_path, "messages.jsonl")

    meta = {
        "title": title,
        "id": chat_id,
        "export_timestamp": datetime.utcnow().isoformat() + "Z",
        "entity_type": type(entity).__name__,
    }

    for attr in ["username", "access_hash", "megagroup", "broadcast", "gigagroup"]:
        if hasattr(entity, attr):
            meta[attr] = getattr(entity, attr)

    meta = normalize(meta)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    media_root = None
    if download_media:
        media_root = os.path.join(folder_path, media_subdir)
        os.makedirs(media_root, exist_ok=True)

    print(f"  Exporting messages from {title} -> {export_file}")
    count = 0
    media_count = 0
    with open(export_file, "w", encoding="utf-8") as f:
        async for msg in client.iter_messages(entity, reverse=True):
            data = normalize(msg.to_dict())

            # Optional media download
            media_path_rel = None
            if download_media and getattr(msg, "media", None):
                try:
                    # When file is a directory, Telethon chooses a filename.
                    media_path = await msg.download_media(file=media_root)
                    if media_path:
                        media_path_rel = os.path.relpath(media_path, folder_path)
                        media_count += 1
                except Exception as e:
                    print(f"    [WARN] Failed to download media for msg {getattr(msg, 'id', '?')}: {e}")

            if media_path_rel:
                # Attach relative path so chat-to-html (and other tooling) can find it.
                data["_local_media"] = media_path_rel

            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            count += 1

    print(f"  Done: {count} messages; {media_count} media files")


async def cmd_backup_remaining(args):
    archive_dir = args.archive_dir
    if archive_dir is None:
        archive_dir = f"telegram_archive_remaining_{date.today().isoformat()}"
    os.makedirs(archive_dir, exist_ok=True)

    exclude_saved = not args.include_saved

    dialogs = await client.get_dialogs()
    print(f"\nBacking up remaining dialogs into: {archive_dir}\n")

    for dialog in dialogs:
        entity = dialog.entity

        # ChatForbidden -> can't access, skip
        if isinstance(entity, types.ChatForbidden):
            print(f"[SKIP FORBIDDEN] {getattr(entity, 'title', 'Unknown')} (id={entity.id})")
            continue

        # Exclude Saved Messages if desired
        if exclude_saved and dialog.is_user and getattr(entity, "is_self", False):
            print("[SKIP SAVED MESSAGES] Saved Messages")
            continue

        if hasattr(entity, "title") and entity.title:
            title = entity.title
        elif isinstance(entity, types.User):
            title = entity.first_name or entity.username or f"user_{entity.id}"
        else:
            title = f"chat_{entity.id}"

        print(f"\nBacking up {title} (id={entity.id}, type={type(entity).__name__})")
        try:
            await backup_dialog(entity, title, archive_dir, download_media=args.download_media)
        except Exception as e:
            print(f"[ERROR] Could not backup {title}: {e}")

    print("\nFinished backing up all remaining chats.")


async def cmd_backup_saved(args):
    """
    Dedicated Saved Messages backup.

    This is basically backup-remaining but targeting only your 'Saved Messages'
    dialog, with an optional media download.
    """
    archive_dir = args.archive_dir
    if archive_dir is None:
        archive_dir = f"telegram_saved_messages_{date.today().isoformat()}"
    os.makedirs(archive_dir, exist_ok=True)

    me = await client.get_me()
    title = "Saved Messages"

    print(f"\nBacking up Saved Messages (id={me.id}) into: {archive_dir}\n")
    try:
        await backup_dialog(me, title, archive_dir, download_media=args.download_media)
    except Exception as e:
        print(f"[ERROR] Could not backup Saved Messages: {e}")
        return

    print("\nFinished backing up Saved Messages.")


# ======================================================================
# COMMAND: count-files
# ======================================================================

def cmd_count_files(args):
    archive_root = args.archive_root
    if not os.path.isdir(archive_root):
        raise RuntimeError(f"Archive root '{archive_root}' does not exist")

    print(f"Scanning archive root: {archive_root}\n")

    total_chats = 0
    for entry in sorted(os.listdir(archive_root)):
        chat_folder = os.path.join(archive_root, entry)
        if not os.path.isdir(chat_folder):
            continue

        msg_path = os.path.join(chat_folder, "messages.jsonl")
        if not os.path.isfile(msg_path):
            print(f"[SKIP] No messages.jsonl in {entry}")
            continue

        total = 0
        file_count = 0

        with open(msg_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if data.get("media"):
                    file_count += 1

        meta_path = os.path.join(chat_folder, "meta.json")
        meta = {}
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as mf:
                    meta = json.load(mf)
            except Exception:
                meta = {}

        meta["total_messages"] = total
        meta["file_count"] = file_count

        with open(meta_path, "w", encoding="utf-8") as mf:
            json.dump(meta, mf, ensure_ascii=False, indent=2)

        print(f"[META] {entry}: total_messages={total}, file_count={file_count}")
        total_chats += 1

    print(f"\nUpdated meta for {total_chats} chat folders.")


# ======================================================================
# COMMAND: chat-to-html
# ======================================================================

def render_message_html(msg: dict) -> str:
    """
    Render a single normalized Telethon message dict into HTML.
    Uses 'out' to distinguish direction and '_local_media' when present.
    """
    msg_id = msg.get("id", "")
    date_str = msg.get("date", "")
    text = msg.get("message") or ""

    # Basic sender label
    if msg.get("out"):
        sender = "Me"
        direction_class = "out"
    else:
        sender = "Them"
        direction_class = "in"

    text_html = html.escape(text).replace("\n", "<br>")

    media_html = ""
    local_media = msg.get("_local_media")
    if local_media:
        media_html = f'<div class="media"><a href="{html.escape(local_media)}">[media]</a></div>'

    # If no text and no media, skip
    if not text_html and not media_html:
        return ""

    return f"""
    <div class="bubble {direction_class}" id="m{msg_id}">
      <div class="meta">{html.escape(sender)} · {html.escape(str(date_str))} · #{msg_id}</div>
      <div class="body">{text_html}</div>
      {media_html}
    </div>
    """.strip()


def cmd_chat_to_html(args):
    archive_root = args.archive_root
    chat_id = args.chat_id
    output = args.output

    if not os.path.isdir(archive_root):
        raise RuntimeError(f"Archive root '{archive_root}' does not exist")

    chat_folder = find_chat_folder_by_id(archive_root, chat_id)

    meta_path = os.path.join(chat_folder, "meta.json")
    msg_path = os.path.join(chat_folder, "messages.jsonl")
    if not os.path.isfile(msg_path):
        raise RuntimeError(f"No messages.jsonl found in {chat_folder}")

    title = f"chat_{chat_id}"
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                title = meta.get("title", title)
        except Exception:
            pass

    if output is None:
        output = os.path.join(chat_folder, "chat.html")

    print(f"Rendering chat '{title}' (id={chat_id}) from {msg_path} -> {output}")

    count = 0
    with open(msg_path, "r", encoding="utf-8") as src, open(output, "w", encoding="utf-8") as dst:
        dst.write(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #ece5dd;
      margin: 0;
      padding: 1rem;
    }}
    .container {{
      max-width: 960px;
      margin: 0 auto;
      background: #d2dbdc;
      padding: 1rem;
      border-radius: 8px;
      box-shadow: 0 0 4px rgba(0,0,0,0.1);
    }}
    .bubble {{
      max-width: 70%;
      margin: 0.25rem 0;
      padding: 0.5rem 0.75rem;
      border-radius: 10px;
      background: #fff;
      box-shadow: 0 1px 1px rgba(0,0,0,0.05);
    }}
    .bubble.out {{
      margin-left: auto;
      background: #dcf8c6;
    }}
    .bubble.in {{
      margin-right: auto;
      background: #ffffff;
    }}
    .meta {{
      font-size: 0.7rem;
      color: #555;
      margin-bottom: 0.25rem;
    }}
    .body {{
      white-space: pre-wrap;
      font-size: 0.9rem;
    }}
    .media {{
      margin-top: 0.25rem;
      font-size: 0.8rem;
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>{html.escape(title)}</h1>
""")
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            html_chunk = render_message_html(msg)
            if not html_chunk:
                continue
            dst.write(html_chunk + "\n")
            count += 1

        dst.write("""
  </div>
</body>
</html>
""")

    print(f"Rendered {count} messages to HTML.")


# ======================================================================
# COMMAND: cleanup-interactive
# ======================================================================

FAILED_DELETES = []  # in-memory container for failed deletes


def record_failed_delete(entity, title: str, error: Exception):
    kind = (
        "user" if isinstance(entity, types.User)
        else "group" if isinstance(entity, types.Chat)
        else "channel" if isinstance(entity, types.Channel)
        else "unknown"
    )

    entry = {
        "id": getattr(entity, "id", None),
        "title": title,
        "kind": kind,
        "entity_type": type(entity).__name__,
        "error": str(error),
    }
    if isinstance(error, FloodWaitError):
        entry["flood_wait_seconds"] = error.seconds

    FAILED_DELETES.append(entry)


async def delete_or_leave_chat(entity, title: str):
    # DMs
    if isinstance(entity, types.User):
        try:
            await client(
                functions.messages.DeleteHistoryRequest(
                    peer=entity,
                    max_id=0,
                    revoke=True,
                )
            )
            print(f"[DELETED DM] {title}")
        except FloodWaitError as e:
            print(f"[FLOOD WAIT DM] {title}: need to wait {e.seconds}s. Recording for later.")
            record_failed_delete(entity, title, e)
        return

    # Legacy group
    if isinstance(entity, types.Chat):
        try:
            await client(
                functions.messages.DeleteChatUserRequest(
                    chat_id=entity.id,
                    user_id="me",
                )
            )
            print(f"[LEFT GROUP] {title}")
        except UserNotParticipantError:
            print(f"[ALREADY LEFT GROUP] {title}")
        except FloodWaitError as e:
            print(f"[FLOOD WAIT GROUP] {title}: need to wait {e.seconds}s. Recording for later.")
            record_failed_delete(entity, title, e)
        return

    # Channel / supergroup
    if isinstance(entity, types.Channel):
        try:
            await client(
                functions.channels.LeaveChannelRequest(channel=entity)
            )
            print(f"[LEFT CHANNEL/SUPERGROUP] {title}")
        except UserNotParticipantError:
            print(f"[ALREADY LEFT CHANNEL/SUPERGROUP] {title}")
        except FloodWaitError as e:
            print(f"[FLOOD WAIT CHANNEL] {title}: need to wait {e.seconds}s. Recording for later.")
            record_failed_delete(entity, title, e)
        return

    print(f"[SKIP DELETE] Unknown entity type for {title} ({type(entity)})")


async def cmd_cleanup_interactive(args):
    # CSV path
    csv_path = args.csv
    if csv_path is None:
        csv_path = find_latest_dialog_csv()
        if not csv_path:
            raise RuntimeError("No telegram_dialog_index_*.csv found and no --csv given.")
    if not os.path.isfile(csv_path):
        raise RuntimeError(f"CSV file not found: {csv_path}")

    archive_root = args.archive_root
    if not os.path.isdir(archive_root):
        raise RuntimeError(f"Archive root '{archive_root}' does not exist")

    failed_json_path = args.failed_json

    # Cutoff date
    cutoff_str = args.cutoff
    cutoff_dt = datetime.fromisoformat(cutoff_str)
    if cutoff_dt.tzinfo is None:
        cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)

    file_limit = args.file_limit

    print(f"Using CSV: {csv_path}")
    print(f"Archive root: {archive_root}")
    print(f"Cutoff date (UTC): {cutoff_dt.isoformat()}")
    print(f"File limit: {file_limit}\n")

    df = pd.read_csv(csv_path)
    required_cols = {"chat_type", "last_message_date", "id", "title"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")

    df["last_message_date"] = pd.to_datetime(
        df["last_message_date"], errors="coerce", utc=True
    )

    mask_old = df["last_message_date"].notna() & (df["last_message_date"] < cutoff_dt)
    mask_not_forbidden = ~df["chat_type"].eq("ChatForbidden")
    candidates = df[mask_old & mask_not_forbidden].copy()

    if candidates.empty:
        print("No dialogs older than cutoff date found.")
        return

    interactive_list = []

    for _, row in candidates.iterrows():
        chat_id = int(row["id"])
        title = str(row["title"])
        chat_type_csv = str(row["chat_type"])
        last_date = row["last_message_date"]

        folder_name = safe_name(title, chat_id)
        chat_folder = os.path.join(archive_root, folder_name)
        meta_path = os.path.join(chat_folder, "meta.json")

        if not os.path.isdir(chat_folder):
            print(f"[SKIP NO BACKUP] {title} (id={chat_id}) — no folder '{chat_folder}'")
            continue

        if not os.path.isfile(meta_path):
            print(f"[SKIP NO META] {title} (id={chat_id}) — no meta.json")
            continue

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            print(f"[SKIP BAD META] {title}: could not read meta.json: {e}")
            continue

        file_count = meta.get("file_count")
        total_messages = meta.get("total_messages")

        if file_count is None:
            print(f"[SKIP NO FILE COUNT] {title} (id={chat_id}) - run count-files first?")
            continue

        if file_count >= file_limit:
            print(
                f"[KEEP FILE-HEAVY] {title} (id={chat_id}) - "
                f"file_count={file_count} >= {file_limit}"
            )
            continue

        interactive_list.append(
            {
                "chat_id": chat_id,
                "title": title,
                "chat_type_csv": chat_type_csv,
                "last_date": last_date,
                "chat_folder": chat_folder,
                "file_count": file_count,
                "total_messages": total_messages,
            }
        )

    if not interactive_list:
        print(
            "\nNo conversations matched: old + backed up + "
            f"file_count < {file_limit}. Nothing to do."
        )
        return

    print("\n=== Interactive cleanup candidates ===\n")
    for item in interactive_list:
        print(
            f"- [{item['chat_type_csv']}] {item['title']} "
            f"(id={item['chat_id']}) - last msg {item['last_date']}, "
            f"files={item['file_count']}, total_msgs={item['total_messages']}"
        )

    print(
        "\nFor each conversation below, you'll be asked whether to delete/leave it.\n"
        "Answer 'y' to proceed, anything else to skip.\n"
    )

    for item in interactive_list:
        chat_id = item["chat_id"]
        title = item["title"]
        chat_type_csv = item["chat_type_csv"]
        last_date = item["last_date"]
        file_count = item["file_count"]
        total_messages = item["total_messages"]
        chat_folder = item["chat_folder"]

        print("\n------------------------------------------------------------")
        print(f"Chat: {title}")
        print(f"Type: {chat_type_csv}")
        print(f"ID:   {chat_id}")
        print(f"Last message: {last_date}")
        print(f"Backup folder: {chat_folder}")
        print(f"Files: {file_count}  |  Total messages: {total_messages}")
        print("------------------------------------------------------------")

        try:
            entity = await client.get_entity(chat_id)
        except Exception as e:
            print(f"[SKIP] Could not get live entity for {title} (id={chat_id}): {e}")
            continue

        if not isinstance(entity, (types.User, types.Chat, types.Channel)):
            print(f"[SKIP] Unsupported entity type: {type(entity)}")
            continue

        if isinstance(entity, types.User):
            prompt = f"Delete DM history with '{title}'? [y/N]: "
        elif isinstance(entity, types.Chat):
            prompt = f"Leave group '{title}'? [y/N]: "
        elif isinstance(entity, types.Channel):
            prompt = f"Leave channel/supergroup '{title}'? [y/N]: "
        else:
            prompt = f"Delete/leave '{title}'? [y/N]: "

        answer = input(prompt).strip().lower()
        if answer != "y":
            print("[SKIP] User chose not to delete/leave this chat.")
            continue

        try:
            await delete_or_leave_chat(entity, title)
        except Exception as e:
            print(f"[ERROR DELETE] Error deleting/leaving '{title}': {e}")
            record_failed_delete(entity, title, e)

    if FAILED_DELETES:
        try:
            with open(failed_json_path, "w", encoding="utf-8") as f:
                json.dump(FAILED_DELETES, f, ensure_ascii=False, indent=2)
            print(
                f"\nRecorded {len(FAILED_DELETES)} failed deletions to "
                f"'{failed_json_path}'."
            )
        except Exception as e:
            print(f"\n[ERROR] Could not write {failed_json_path}: {e}")
    else:
        print("\nNo failed deletions recorded.")

    print("\nInteractive cleanup complete.")


# ======================================================================
# COMMAND: retry-failed
# ======================================================================

async def leave_entity_with_retry(entity, title: str):
    while True:
        try:
            if isinstance(entity, types.Channel):
                await client(functions.channels.LeaveChannelRequest(channel=entity))
                print(f"[DONE] Left channel/supergroup '{title}'")
            elif isinstance(entity, types.Chat):
                await client(functions.messages.DeleteChatUserRequest(
                    chat_id=entity.id,
                    user_id="me",
                ))
                print(f"[DONE] Left basic group '{title}'")
            elif isinstance(entity, types.User):
                await client(functions.messages.DeleteHistoryRequest(
                    peer=entity,
                    max_id=0,
                    revoke=True,
                ))
                print(f"[DONE] Deleted DM history '{title}'")
            else:
                print(f"[SKIP] Unsupported entity type {type(entity).__name__} for '{title}'")
            return

        except UserNotParticipantError:
            print(f"[OK] Already not a member of '{title}'")
            return

        except FloodWaitError as e:
            print(f"[FLOOD WAIT] '{title}' -> waiting {e.seconds}s...")
            time.sleep(e.seconds)
            # then retry

        except Exception as e:
            print(f"[ERROR] Could not leave '{title}': {e}")
            return


async def cmd_retry_failed(args):
    failed_json_path = args.failed_json
    if not os.path.isfile(failed_json_path):
        print(f"No failed-deletes JSON found at {failed_json_path}")
        return

    with open(failed_json_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    print(f"Retrying {len(entries)} failed deletions...\n")

    for entry in entries:
        chat_id = entry.get("id")
        title = entry.get("title", f"id={chat_id}")

        print(f"---\nHandling '{title}' (id={chat_id})")

        try:
            entity = await client.get_entity(chat_id)
        except Exception as e:
            print(f"[SKIP] Could not get entity: {e}")
            continue

        await leave_entity_with_retry(entity, title)

    print("\nAll retries complete.")


# ======================================================================
# COMMAND: force-delete-ghosts
# ======================================================================

async def delete_ghost_chat(chat_id: int, test_mode: bool):
    print(f"\n🔍 Attempting to force-delete chat id={chat_id}")

    try:
        entity = await client.get_entity(chat_id)
        print(f"  Found entity: {type(entity).__name__}")
    except Exception:
        entity = chat_id
        print("  Could not load entity normally, using raw chat id")

    # Try DeleteHistory
    try:
        if not test_mode:
            await client(functions.messages.DeleteHistoryRequest(
                peer=entity,
                just_clear=True,
                revoke=True,
                max_id=0,
            ))
        print("  ✔ History cleared (or attempted)")
    except UserNotParticipantError:
        print("  ℹ Already not participant - skipping history clear")
    except FloodWaitError as e:
        print(f"  ⏳ FloodWait: waiting {e.seconds}s")
        time.sleep(e.seconds)
        return await delete_ghost_chat(chat_id, test_mode)
    except Exception as e:
        print(f"  ⚠ Error on history delete: {e}")

    # Try DeleteChatUserRequest (legacy group)
    try:
        if not test_mode:
            await client(functions.messages.DeleteChatUserRequest(
                chat_id=chat_id,
                user_id="me",
            ))
        print("  ✔ Attempted DeleteChatUserRequest")
    except UserNotParticipantError:
        print("  ℹ Not in chat - already gone")
    except FloodWaitError as e:
        print(f"  ⏳ FloodWait: waiting {e.seconds}s")
        time.sleep(e.seconds)
        return await delete_ghost_chat(chat_id, test_mode)
    except Exception as e:
        print(f"  ⚠ Error on delete user: {e}")

    print("  🧹 Attempted full purge")


async def cmd_force_delete_ghosts(args):
    ids = args.ids
    test_mode = args.test_mode

    print("\n⚠ Starting ghost chat cleanup")
    if test_mode:
        print("‼ TEST MODE ENABLED - no deletion will happen")

    for chat_id in ids:
        await delete_ghost_chat(chat_id, test_mode)

    print("\n🏁 Ghost cleanup finished")


# ======================================================================
# ARGPARSE / MAIN
# ======================================================================

def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Telegram maintenance suite (backup + cleanup)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # index-dialogs
    sp = sub.add_parser("index-dialogs", help="Export CSV index of all dialogs")
    sp.add_argument("--output", help="Output CSV filename (default: telegram_dialog_index_<timestamp>.csv)")

    # backup-remaining
    sp = sub.add_parser("backup-remaining", help="Backup all remaining dialogs (except Saved Messages by default)")
    sp.add_argument(
        "--archive-dir",
        help="Archive directory (default: telegram_archive_remaining_<today>)",
    )
    sp.add_argument(
        "--include-saved",
        action="store_true",
        help="Include Saved Messages in backup",
    )
    sp.add_argument(
        "--download-media",
        action="store_true",
        help="Also download media/files per chat into a media/ subfolder",
    )

    # backup-saved
    sp = sub.add_parser("backup-saved", help="Backup Saved Messages (optionally with media)")
    sp.add_argument(
        "--archive-dir",
        help="Archive directory (default: telegram_saved_messages_<today>)",
    )
    sp.add_argument(
        "--download-media",
        action="store_true",
        help="Also download media/files into a media/ subfolder",
    )

    # count-files
    sp = sub.add_parser("count-files", help="Count files in archive & write meta.json per chat")
    sp.add_argument(
        "--archive-root",
        required=True,
        help="Path to archive root (e.g. telegram_archive_remaining_2025-11-18)",
    )

    # chat-to-html
    sp = sub.add_parser("chat-to-html", help="Render a backed up chat into a single HTML file")
    sp.add_argument(
        "--archive-root",
        required=True,
        help="Path to archive root with per-chat folders",
    )
    sp.add_argument(
        "--chat-id",
        type=int,
        required=True,
        help="Chat ID to render (matches folder suffix _<id>)",
    )
    sp.add_argument(
        "--output",
        help="Output HTML filename (default: <chat_folder>/chat.html)",
    )

    # cleanup-interactive
    sp = sub.add_parser("cleanup-interactive", help="Interactive cleanup of old low-file-count chats")
    sp.add_argument(
        "--csv",
        help="Dialog CSV (default: latest telegram_dialog_index_*.csv)",
    )
    sp.add_argument(
        "--archive-root",
        required=True,
        help="Archive root with per-chat folders",
    )
    sp.add_argument(
        "--cutoff",
        default="2025-01-01T00:00:00+00:00",
        help="ISO cutoff datetime (UTC) for last_message_date (default: 2025-01-01T00:00:00+00:00)",
    )
    sp.add_argument(
        "--file-limit",
        type=int,
        default=10,
        help="Only consider chats with fewer than this many files (default: 10)",
    )
    sp.add_argument(
        "--failed-json",
        default="cleanup_failed_deletes.json",
        help="Where to store failed deletions JSON (default: cleanup_failed_deletes.json)",
    )

    # retry-failed
    sp = sub.add_parser("retry-failed", help="Retry deletions stored in cleanup_failed_deletes.json")
    sp.add_argument(
        "--failed-json",
        default="cleanup_failed_deletes.json",
        help="Path to failed deletions JSON (default: cleanup_failed_deletes.json)",
    )

    # force-delete-ghosts
    sp = sub.add_parser("force-delete-ghosts", help="Force-delete ghost/stuck chats by id")
    sp.add_argument(
        "--ids",
        type=int,
        nargs="+",
        required=True,
        help="List of chat IDs to purge",
    )
    sp.add_argument(
        "--test-mode",
        action="store_true",
        help="Don't actually delete, just log actions",
    )

    return p


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    with client:
        if args.command == "index-dialogs":
            client.loop.run_until_complete(cmd_index_dialogs(args))
        elif args.command == "backup-remaining":
            client.loop.run_until_complete(cmd_backup_remaining(args))
        elif args.command == "backup-saved":
            client.loop.run_until_complete(cmd_backup_saved(args))
        elif args.command == "count-files":
            cmd_count_files(args)
        elif args.command == "chat-to-html":
            cmd_chat_to_html(args)
        elif args.command == "cleanup-interactive":
            client.loop.run_until_complete(cmd_cleanup_interactive(args))
        elif args.command == "retry-failed":
            client.loop.run_until_complete(cmd_retry_failed(args))
        elif args.command == "force-delete-ghosts":
            client.loop.run_until_complete(cmd_force_delete_ghosts(args))
        else:
            parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
