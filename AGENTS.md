# Grim — Agent notes

Cursor Agent is the development scribe for Grim. Prefer this file plus `README.md`,
`OPERATIONS_SECURITY.md`, and `BACKUP_RESTORE.md` over any legacy host-specific project notes.

## Overview
Grim is a Discord bot for Seclude & Affiliates. Built with Python and discord.py
with xAI's Grok API integration. Live runtime is the VPS deploy path
(`.github/workflows/deploy.yml`); secrets live in the VPS `.env` and GitHub Actions.

## Project structure
- `main.py` — bot commands and event handlers
- Runtime JSON state — under `~/.grim_data/` (not the git workspace)
- `OPERATIONS_SECURITY.md` — ops and incident SOP
- `BACKUP_RESTORE.md` — encrypted weekly backup / restore

## Developer notes
When adding new time-based or scheduled commands:
1. Wire the task into `health_monitor()` so it can auto-restart if it crashes
2. Add an `after_loop` handler to log unexpected stops
3. Update `/newsfeed_status` (or the relevant status surface) to show the new task

## VPS backups
`backup_grim_data.py` is separate from the bot and reads only `~/.grim_data`.
`setup_vps.sh` can install its root-only systemd timer when given a dedicated
fine-grained GitHub token and an offline recovery passphrase. Never store backup
credentials in the repository or Grim's `.env`.
