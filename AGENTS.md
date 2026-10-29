# Grim — Agent notes

Cursor Agent is the development scribe for Grim. Prefer this file plus `README.md`,
`OPERATIONS_SECURITY.md`, and `BACKUP_RESTORE.md` over any legacy host-specific project notes.

## Overview
Grim is a Discord bot for Seclude & Affiliates. Built with Python and discord.py
with xAI's Grok API integration. Live runtime is the VPS deploy path
(`.github/workflows/deploy.yml`); secrets live in the VPS `.env` and GitHub Actions.

## Hard rules for the live host
- Runtime state source of truth: `~/.grim_data` (never the git workspace).
- The VPS must **never** push Grim source or runtime JSON into `Deathxi/Grim`.
- Encrypted weekly backups may upload **only** to the separate private
  `grim-private-backups` repo via `backup_grim_data.py` / systemd timer.
- Optional `GITHUB_PERSONAL_ACCESS_TOKEN` is **read-only** (commit metadata for
  `/grim_updates`). Prefer Contents: Read. Never grant write on the source repo
  to the bot process.
- Backup credentials stay in `/etc/grim-backup.env` + passphrase file — not in
  Grim's `.env` and never in git.

## Project structure
- `main.py` — bot commands and event handlers
- `requirements.txt` / `pyproject.toml` / `uv.lock` — Python deps (VPS install uses `requirements.txt` into `/root/grim/venv`)
- `systemd/grim.service` — `ExecStart` must use `/root/grim/venv/bin/python3`
- Runtime JSON/SQLite state — under `~/.grim_data/`
- `OPERATIONS_SECURITY.md` — ops and incident SOP
- `BACKUP_RESTORE.md` — encrypted weekly backup / restore
- `backup_grim_data.py` / `run_grim_backup.sh` / `systemd/` — backup-only path

## Developer notes
When adding new time-based or scheduled commands:
1. Wire the task into `health_monitor()` so it can auto-restart if it crashes
2. Add an `after_loop` handler to log unexpected stops
3. Update `/newsfeed_status` (or the relevant status surface) to show the new task

Do not reintroduce Replit scaffolding or any “auto-sync source to GitHub” helper.
