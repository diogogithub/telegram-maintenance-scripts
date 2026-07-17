# Telegram Maintenance Scripts

A small Telethon-based toolkit for creating local Telegram archives and carefully cleaning up a long-lived account.

It can export dialogs, messages, media, Saved Messages, and contacts; render backed-up chats as HTML; and help remove old or stuck conversations after they have been archived.

> [!WARNING]
> Some commands can delete message history or leave chats. Keep verified backups before using cleanup commands.

## Features

- Export a CSV index of all dialogs.
- Back up messages from all current dialogs.
- Back up Saved Messages separately.
- Optionally download message media and files.
- Export Telegram contacts with IDs, names, usernames, phone numbers when available, account flags, presence metadata, full-user details, and current avatars.
- Count media-bearing messages per archived chat.
- Render an archived chat as a standalone HTML transcript.
- Interactively clean old, low-file-count chats.
- Retry operations interrupted by Telegram flood limits.
- Attempt removal of legacy ghost or stuck chats by numeric ID.

## Requirements

- Python 3.10 or newer
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org/)
- A Telegram user account; these tools are not intended for bot accounts

## Installation

```bash
git clone https://github.com/diogogithub/telegram-maintenance-scripts.git
cd telegram-maintenance-scripts

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash_here
# TELEGRAM_SESSION_NAME=telegram_maintenance
```

On first use, Telethon asks for your phone number and login code and creates a local `.session` file. Treat that file as a credential and never publish it.

## Commands

The main maintenance suite uses:

```bash
python tg_maintenance.py <command> [options]
```

Available commands:

| Command | Purpose |
|---|---|
| `index-dialogs` | Export a CSV index of dialogs |
| `backup-remaining` | Back up current dialogs, excluding Saved Messages by default |
| `backup-saved` | Back up Saved Messages |
| `count-files` | Count media-bearing messages and update each chat's `meta.json` |
| `chat-to-html` | Render one archived chat as HTML |
| `cleanup-interactive` | Review and remove old, backed-up, low-file-count chats |
| `retry-failed` | Retry deletions previously interrupted by errors or flood waits |
| `force-delete-ghosts` | Attempt to remove legacy stuck chats by ID |

Contact backup is provided as a separate focused command:

```bash
python tg_contacts_backup.py [options]
```

## Export contacts and current avatars

```bash
python tg_contacts_backup.py
```

The command uses Telegram's contact and full-user methods and creates a timestamped directory:

```text
telegram_contacts_20260717_190000/
├── contacts.csv
├── contacts.json
├── manifest.json
└── avatars/
    ├── 123456_Alice_Example.jpg
    └── 987654_Bob_Example.jpg
```

`contacts.csv` is convenient for inspection and recovery. `contacts.json` preserves substantially more information, including the raw Telethon `User` and `UserFull` structures when Telegram makes them available.

The export includes, when available:

- numeric user ID and access hash;
- first name, last name, display name, primary username, and additional usernames;
- phone number and language code;
- contact, mutual-contact, close-friend, bot, verified, premium, restricted, scam, fake, and deleted flags;
- presence/status type and associated timestamp;
- bio, birthday, and common-chat count from full-user metadata;
- current profile-photo metadata and a downloaded active avatar.

Useful options:

```bash
# Choose the destination
python tg_contacts_backup.py --output-dir my_contacts_backup

# Export metadata without profile photos
python tg_contacts_backup.py --skip-avatars

# Avoid the per-contact full-user request
python tg_contacts_backup.py --skip-full-details

# Download smaller current avatars
python tg_contacts_backup.py --small-avatars
```

Telegram privacy settings and account state determine which fields can be retrieved. An unavailable field is left empty, and individual lookup or avatar failures are recorded without aborting the rest of the export. Telegram flood waits are respected automatically.

> [!IMPORTANT]
> A contacts archive can contain phone numbers, biographies, usernames, presence information, and photographs. Store it on encrypted media and do not commit it to Git.

## Index dialogs

```bash
python tg_maintenance.py index-dialogs
```

This creates a file such as `telegram_dialog_index_20260717_190000.csv` with dialog title, type, ID, access hash, administrative flags, public link, member count, and last-message date.

## Back up dialogs

```bash
python tg_maintenance.py backup-remaining
```

By default, the command excludes Saved Messages and writes a directory such as:

```text
telegram_archive_remaining_2026-07-17/
└── Some_Group_123456789/
    ├── meta.json
    └── messages.jsonl
```

Include media files:

```bash
python tg_maintenance.py backup-remaining --download-media
```

Choose the archive directory or include Saved Messages:

```bash
python tg_maintenance.py backup-remaining \
  --archive-dir telegram_archive_full_2026-07-17 \
  --include-saved \
  --download-media
```

Each line in `messages.jsonl` is one normalized Telethon message object. When media is downloaded successfully, the object receives a `_local_media` path relative to the chat directory.

## Back up Saved Messages

```bash
python tg_maintenance.py backup-saved --download-media
```

A dedicated export is useful when Saved Messages is used as an inbox, bookmark collection, or file store.

## Count files in an archive

```bash
python tg_maintenance.py count-files \
  --archive-root telegram_archive_remaining_2026-07-17
```

The command counts messages with a non-null `media` field and writes `total_messages` and `file_count` into each chat's `meta.json`.

## Render a chat as HTML

```bash
python tg_maintenance.py chat-to-html \
  --archive-root telegram_archive_remaining_2026-07-17 \
  --chat-id 123456789
```

Use `--output` to choose another output file. Locally downloaded media is linked from the transcript when `_local_media` is present.

## Interactive cleanup

First create and verify backups, then index the current dialogs and count files. Run cleanup with conservative thresholds:

```bash
python tg_maintenance.py cleanup-interactive \
  --archive-root telegram_archive_remaining_2026-07-17 \
  --csv telegram_dialog_index_20260717_190000.csv \
  --cutoff 2025-01-01T00:00:00+00:00 \
  --file-limit 10 \
  --failed-json cleanup_failed_deletes.json
```

Only chats that are older than the cutoff, have a matching backup directory and metadata, and contain fewer than the configured number of media-bearing messages are offered for deletion or leaving. Every action still requires interactive confirmation.

Retry recorded failures:

```bash
python tg_maintenance.py retry-failed \
  --failed-json cleanup_failed_deletes.json
```

## Force-delete a stuck chat

```bash
python tg_maintenance.py force-delete-ghosts --ids 132006505
```

Dry-run the operation first:

```bash
python tg_maintenance.py force-delete-ghosts --ids 132006505 --test-mode
```

This command attempts low-level history and legacy-group operations. Use it only for chats that cannot be removed normally.

## Safety and privacy

- Verify archives before deleting or leaving anything.
- Keep `.env`, Telethon `.session` files, exported contacts, and message archives private.
- Prefer encrypted storage for backups.
- Remember that deleting a direct-message history with `revoke=True` may affect the other participant where Telegram permits it.
- Telegram may impose flood waits; do not try to bypass them.
- This project is independent and is not affiliated with Telegram or Telethon.

## Development

Basic checks run in GitHub Actions for supported Python versions. Locally:

```bash
python -m py_compile tg_maintenance.py tg_contacts_backup.py
TELEGRAM_API_ID=1 TELEGRAM_API_HASH=test python tg_maintenance.py --help
python tg_contacts_backup.py --help
```

Contributions, fixes, additional archive formats, and carefully scoped maintenance features are welcome.

## License

MIT. See [LICENSE](LICENSE).
