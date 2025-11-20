# Telegram Maintenance Suite (Backup + Cleanup)

A small **maintenance toolkit** around [Telethon](https://github.com/LonamiWebs/Telethon) to:

- 📋 Export a CSV index of all your dialogs  
- 💾 Backup all messages from remaining dialogs (except Saved Messages by default)  
- 💾📥 Backup **Saved Messages** (with optional media/files)  
- 📦 Optionally download media/files for each chat into a local folder  
- 📊 Count media files per chat in an archive  
- 🗂️ Render a backed-up chat as a standalone **HTML transcript**  
- 🧹 Interactively cleanup old/lightweight chats you've already backed up  
- 🔁 Retry deletions that hit flood limits  
- 👻 Force-delete ghost / stuck chats that Telegram Desktop won't let you remove  

This is basically the toolbox I used to **backup and clean a long-lived Telegram account**.

---

## ⚙️ Requirements

- Python **3.10+**
- A Telegram API ID and hash from [my.telegram.org](https://my.telegram.org/)
- A basic understanding that this script can **delete chats** if you ask it to 🙂

---

## 🧰 Setup

```bash
git clone <this-repo-url> telegram-maintenance-suite
cd telegram-maintenance-suite

python -m venv venv
source venv/bin/activate  # on Windows: venv\Scripts\activate

pip install -r requirements.txt
````

Create your `.env` file:

```bash
cp .env.example .env
```

Edit `.env` and fill:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash_here
# optional, to re-use an existing Telethon session:
# TELEGRAM_SESSION_NAME=telegram_maintenance
```

On first run, Telethon will ask for your phone number and login code, and create a `.session` file.

---

## 🧭 Commands Overview

All commands are run via:

```bash
python tg_maintenance.py <command> [options...]
```

Available commands:

* `index-dialogs` - export CSV index of dialogs
* `backup-remaining` - backup all current dialogs (except Saved Messages by default)
* `backup-saved` - backup your **Saved Messages** dialog
* `count-files` - count media files in an archive and write `meta.json`
* `chat-to-html` - render a backed-up chat as a standalone HTML file
* `cleanup-interactive` - interactively delete/leave backed-up, old, low-file chats
* `retry-failed` - retry deletions that hit flood limits
* `force-delete-ghosts` - manually purge ghost chats by numeric ID

---

## 1. Index all dialogs (optional but recommended)

```bash
python tg_maintenance.py index-dialogs
```

This creates a CSV named something like:

```text
telegram_dialog_index_20251118_123456.csv
```

Columns include: `title`, `chat_type`, `id`, `access_hash`, `is_me`, `is_bot`, `is_private`, `is_admin`, `invite_link`, `members_count`, `last_message_date`.

You can later pass this CSV into `cleanup-interactive`.

---

## 2. Backup all remaining dialogs

This backs up **everything currently visible** in your account (except Saved Messages by default) into a timestamped archive folder:

```bash
python tg_maintenance.py backup-remaining
```

By default, it creates:

```text
telegram_archive_remaining_<YYYY-MM-DD>/
  Some_Group_123456789/
    meta.json
    messages.jsonl
  Some_Channel_987654321/
    meta.json
    messages.jsonl
  ...
```

You can customize the archive directory:

```bash
python tg_maintenance.py backup-remaining \
  --archive-dir telegram_archive_full_2025-11-18
```

To also include Saved Messages in this "everything" backup:

```bash
python tg_maintenance.py backup-remaining --include-saved
```

### 2.1 Also download media/files during backup

Both `backup-remaining` and `backup-saved` support a flag to download media/files:

```bash
python tg_maintenance.py backup-remaining \
  --archive-dir telegram_archive_remaining_2025-11-18 \
  --download-media
```

With `--download-media`:

```text
telegram_archive_remaining_2025-11-18/
  Some_Group_123456789/
    meta.json
    messages.jsonl
    media/
      <downloaded files...>
```

Each `messages.jsonl` line is still **one JSON object per line**, in chronological order, but if a message had media and the download succeeded, the JSON object also gets an extra field:

```json
{
  "...": "...",
  "media": { ... },
  "_local_media": "media/photo_123.jpg"
}
```

That `_local_media` relative path is used by `chat-to-html` (and any other tooling you write) to link to the downloaded file.

`meta.json` contains basic metadata plus `total_messages`/`file_count` once you run `count-files`.

---

## 2b. Backup Saved Messages (with optional media)

There is a dedicated command for **Saved Messages**, which backs up your self-chat only:

```bash
python tg_maintenance.py backup-saved
```

You can choose the archive directory:

```bash
python tg_maintenance.py backup-saved \
  --archive-dir telegram_saved_messages_2025-11-18
```

And you can also download media/files from Saved Messages:

```bash
python tg_maintenance.py backup-saved \
  --archive-dir telegram_saved_messages_2025-11-18 \
  --download-media
```

The folder structure is similar:

```text
telegram_saved_messages_2025-11-18/
  Saved_Messages_<your_user_id>/
    meta.json
    messages.jsonl
    media/          # only if --download-media was used
      <downloaded files...>
```

This is particularly handy if you use Saved Messages as a personal **inbox / bookmark / file stash** and want a full local copy.

---

## 3. Count files per chat in an archive

After you've backed up, run:

```bash
python tg_maintenance.py count-files --archive-root telegram_archive_remaining_2025-11-18
```

This:

* Scans every `messages.jsonl`
* Counts messages with a non-null `media` field
* Writes/updates a `meta.json` inside each chat folder with:

```json
{
  "title": "...",
  "id": 123456789,
  "export_timestamp": "...",
  "entity_type": "Channel",
  "total_messages": 1234,
  "file_count": 42
}
```

You can then use `file_count` to avoid deleting **file-heavy** chats.

---

## 4. Interactive cleanup of backed-up old chats

Once you're happy with your backups, you can interactively clean up **older** chats that:

* Have been inactive since a cutoff date
* Have fewer than N media files
* Have a backup folder + `meta.json`

Example:

```bash
python tg_maintenance.py cleanup-interactive \
  --archive-root telegram_archive_remaining_2025-11-18 \
  --csv telegram_dialog_index_20251118_123456.csv \
  --cutoff 2025-01-01T00:00:00+00:00 \
  --file-limit 10 \
  --failed-json cleanup_failed_deletes.json
```

For each chat matching the filters, you'll see a prompt like:

```text
Chat: Some Old Group
Type: group
ID:   123456789
Last message: 2022-03-15 20:51:50+00:00
Backup folder: telegram_archive_remaining_2025-11-18/Some_Old_Group_123456789
Files: 2  |  Total messages: 75
------------------------------------------------------------
Leave group 'Some Old Group'? [y/N]:
```

* `y` → actually delete/leave
* anything else → skip

Rate-limited deletions (flood wait) are recorded in `cleanup_failed_deletes.json` so you can retry them later.

---

## 5. Retry deletions that hit flood limits

If `cleanup-interactive` reported flood waits and wrote `cleanup_failed_deletes.json`, you can retry them more slowly:

```bash
python tg_maintenance.py retry-failed --failed-json cleanup_failed_deletes.json
```

This:

* Reloads each failed chat ID
* Tries to delete/leave again
* Handles `FloodWaitError` by waiting the specified number of seconds and retrying

---

## 6. Force-delete ghost / stuck chats

Some very old legacy groups can get **stuck** in Telegram clients (e.g. show as "Hound" group with no way to delete in the UI).

You can surgically target these ghosts by **numeric ID**:

```bash
python tg_maintenance.py force-delete-ghosts --ids 132006505 212817379
```

To dry-run first:

```bash
python tg_maintenance.py force-delete-ghosts --ids 132006505 --test-mode
```

This will:

* Try clearing history (`DeleteHistoryRequest`)
* Try removing yourself from the group (`DeleteChatUserRequest`)
* Respect flood limits

Use carefully; this is meant for chats you **cannot** remove via the normal Telegram UI.

---

## 7. Export a chat archive to HTML

Once you have a backup (from `backup-remaining` or `backup-saved`), you can turn any chat into a **self-contained HTML transcript**.

```bash
python tg_maintenance.py chat-to-html \
  --archive-root telegram_archive_remaining_2025-11-18 \
  --chat-id 123456789
```

This:

* Locates the per-chat folder whose name ends with `_<chat-id>` (e.g. `Some_Group_123456789`)
* Reads `messages.jsonl`
* Writes `chat.html` inside that folder by default (or wherever you point `--output`)

Example with custom output path:

```bash
python tg_maintenance.py chat-to-html \
  --archive-root telegram_saved_messages_2025-11-18 \
  --chat-id 123456789 \
  --output saved_messages.html
```

The HTML:

* Renders messages as chat-style **bubbles** (`Me` vs `Them`)
* Uses timestamps and message IDs from the backup
* If you used `--download-media`, any message with a `_local_media` field will show a local `[media]` link to that file (e.g. an image, document, etc.)

You can open the resulting `chat.html` in any browser, or print to PDF if you want a "frozen" transcript.

---

## 🛡️ Safety tips

* Always make backups (`backup-remaining` / `backup-saved`) **before** aggressive cleanup.
* Use `cleanup-interactive` conservatively at first (e.g. very old cutoff date + low file_limit).
* Keep your archives somewhere safe (e.g. encrypted disk / backup drive).
* You can re-run `index-dialogs` and keep a CSV snapshot of your **post-cleanup** state.
* If you care about attachments, remember to use `--download-media` during backups.

---

## 🔧 Development notes

* Written with [Telethon](https://github.com/LonamiWebs/Telethon)
* Tested with Python 3.10+ and recent Telethon versions
* Single-file script (`tg_maintenance.py`) for portability, but you can split it into modules if you prefer
* `chat-to-html` deliberately keeps HTML/CSS minimal so you can easily tweak the look

PRs / tweaks / feature ideas are welcome.
If you find a better heuristic for "ghost" chats or want more backup formats / viewers, extend away. 🙂
