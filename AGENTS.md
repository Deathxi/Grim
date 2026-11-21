# Grim — Agent notes

Cursor Agent is the development scribe for Grim. Prefer this file plus `README.md`,
`OPERATIONS_SECURITY.md`, and `BACKUP_RESTORE.md` over any legacy host-specific project notes.

## Overview
Grim is a Discord bot for Seclude & Affiliates. Built with Python and discord.py
with xAI's Grok API integration. Live runtime is the VPS deploy path
(`.github/workflows/deploy.yml`); secrets live in the VPS `.env` and GitHub Actions
(`FOUNDER_DISCORD_ID` for private deletion DMs — name only in git).

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
- `grim_loops.py` — sticky context, repair/confirm, optional nudges, next-step offers, 👍/👎 feedback (same completion; nudges default off)
- `delete_report.py` — founder-only deleted-message DMs (`FOUNDER_DISCORD_ID`);
  `/panopticon` is the Founder-only on/off toggle (default on)
- `substance.py` — per-mood substance rules and the post-check on mention replies
- `requirements.txt` / `pyproject.toml` / `uv.lock` — Python deps (VPS install uses `requirements.txt` into `/root/grim/venv`)
- `systemd/grim.service` — `ExecStart` must use `/root/grim/venv/bin/python3`
- Runtime JSON/SQLite state — under `~/.grim_data/`
- `OPERATIONS_SECURITY.md` — ops and incident SOP
- `BACKUP_RESTORE.md` — encrypted weekly backup / restore
- `HOTFIXES.md` — operator hotfix log (no public card, no version bump)
- `backup_grim_data.py` / `run_grim_backup.sh` / `systemd/` — backup-only path

## Developer notes
When adding new time-based or scheduled commands:
1. Wire the task into `health_monitor()` so it can auto-restart if it crashes
2. Add an `after_loop` handler to log unexpected stops
3. Add the task to the `/status` background task list (or the relevant status surface)

Public `/grim_updates` cards and CHANGELOG are the default: real filenames
and real patch notes. The deploy card reads those notes from the checkout's
CHANGELOG for the merge that just shipped, including when the optional GitHub
token is missing. The V2.x → V3.00 `/grim_updates` card is a first-person memento of V2,
posted the instant that jump is noticed. From V3.01 on, deploys use the
regular public changelog card again.

Hotfixes are the old stealth-updates path, renamed. Apply them only when the
merge commit is labeled `HOTFIX:` / `[HOTFIX]` / `this is HOTFIX`. A hotfix
does not bump the version and does not post a public `/grim_updates` card;
add the notes to `HOTFIXES.md` instead. Do not infer a hotfix from filenames,
feature area, or a CLASSIFIED label. `[REDACTED]` Discord cards stay off.
Ordinary deploys keep real filenames and public patch notes.

Do not reintroduce Replit scaffolding or any “auto-sync source to GitHub” helper.
