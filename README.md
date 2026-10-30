# Grim

Discord bot for Seclude & Affiliates.

Python · discord.py · xAI Grok  
Maintained with [Cursor Agent](https://cursor.com).

---

## Overview

Grim runs on a VPS. Runtime state lives under `~/.grim_data` (outside the git checkout). Source deploys through GitHub Actions; the live host does not push code or runtime JSON into this repository.

| Path | Role |
| --- | --- |
| `main.py` | Bot commands and event handlers |
| `requirements.txt` | VPS install pins (`/root/grim/venv`) |
| `systemd/` | Service and weekly backup units |
| `OPERATIONS_SECURITY.md` | Ops and incident SOP |
| `BACKUP_RESTORE.md` | Encrypted backup and restore |
| `AGENTS.md` | Agent / contributor notes |

---

## Terms of Service

By inviting or interacting with Grim, you agree to these terms.  
Last updated: October 29, 2026.

### 1. Usage

- Grim is for personal, non-commercial use in authorized servers only. No reselling, cloning, or reverse-engineering.
- Follow Discord's Terms of Service and Community Guidelines.
- xAI integrations (Grok) are subject to [xAI's terms](https://x.ai/terms). Rate limits apply.

### 2. Content

- Generated responses are experimental; use at your own risk. No liability for outcomes of AI output.
- Keep inputs appropriate for your server. You remain responsible for server content and moderation.
- Operational data is handled under the Privacy Policy below.

### 3. Prohibitions

- No harassment, scams, doxxing, or illegal use. Enforcement decisions are final.
- No spam or unauthorized self-promotion through the bot.

### 4. Changes

- Terms may change; continued use after an update means acceptance.
- Questions: contact the operators in Seclude.

---

## Privacy Policy

Last updated: October 29, 2026.

### 1. What we collect

- Discord user IDs, usernames, display names, and avatar URLs — for member history, language preferences, stats, and staff directory views.
- Server (guild) IDs and channel IDs — for settings, feeds, welcome/updates channels, and schedules.
- Message content in guild channels Grim can see — persisted for conversational context, digests, and member profile synthesis (not only when you @Grim).
- Voice-channel presence timing — aggregate time-in-voice for `/stats`.
- Command and preference data — reminders, language preferences, moderation lists, welcome/member-log settings, feed schedules, and similar configuration.
- Redacted security audit metadata — action, outcome, actor/server IDs, and non-secret metadata (no message bodies); retained about 90 days.
- Prompts sent to xAI (Grok) — when Grim generates an AI reply; handled under [xAI's privacy policy](https://x.ai/legal/privacy-policy).

### 2. How it is used

- To operate Grim in authorized servers: replies, schedules, moderation helpers, staff member history, and deployment/outage notices.
- xAI receives prompt/context required to generate a reply. Discord receives what Grim posts back into the server.

### 3. Storage and backups

- Runtime state lives on the VPS under `~/.grim_data`, including SQLite (`chat_history.db`) and JSON config/state files.
- Member history events are retained about **730 days**, then pruned.
- Security audit events are retained about **90 days**, then pruned.
- Chat history and other learned state persist across restarts until operators delete or restore them.
- Weekly **encrypted** archives of `~/.grim_data` upload to a separate private GitHub repository configured by the operator. Archives exclude `.env`, credential-like filenames, and the backup passphrase. See `BACKUP_RESTORE.md`.
- The live host does **not** push Grim source code or runtime state into the public/source `Deathxi/Grim` repository.

### 4. Sharing

- Never sold.
- Shared only as needed to run the bot: Discord (messages Grim sends), xAI (prompts for AI replies), and the private encrypted-backup repository operators configure.
- Staff in a server may see member-directory and departure cards Grim posts to configured staff channels.

### 5. Deletion and access

- Leaving a server or kicking Grim does **not** by itself wipe `~/.grim_data` or encrypted backups.
- For erasure or export requests, contact the operator (Deathxi / Seclude staff). Operators can delete or restore runtime files and manage backup retention per `BACKUP_RESTORE.md` and `OPERATIONS_SECURITY.md`.

### 6. Changes

This section is updated in the repository when collection or retention changes. Continued use after an update means acceptance.

No cookies, trackers, or advertising profiles.

— Deathxi & the Deathly Hollows
