# Grim — Changelog

All notable changes to this project are documented here, organized by month.

---

## Version 2.0 — August 2026

Version 2.0 begins Grim's security-first operations phase.

### Added
- Explicit 2.0 release baseline with automatic migration from any older 1.xx runtime counter.
- Server-scoped moderation controls, shared authorization checks, redacted security audit events, and abuse limits.
- Atomic runtime-state writes and the `OPERATIONS_SECURITY.md` operations and incident-response SOP.
- Staff-only member history directory with stable Discord-ID tracking, join/leave/rejoin and identity events, compact profile review, and configurable staff-channel departure cards.
- Renamed management commands to `/server`, `/members`, and `/memberlog ENABLE|DISABLE` for clearer server administration.
- Redesigned `/server` as Grim's privacy-aware server info card with banner, emojis, separated text and voice channel totals, and aggregate language signals.

---

## June 2026

### Added
- `/support` — Shows contact info (`x@deathi.net`) and a link to the Seclude & Affiliates Discord hub/FAQ. Displays the server icon as a thumbnail once the CDN URL is configured.
- Version counter moved to `~/.grim_data/version_count.txt` (persists across redeploys). Project-root `version.txt` stays in sync for GitHub visibility.
- `/grim_updates` config now stored in project root and pushed to GitHub on toggle — survives all future redeploys without needing to be re-enabled.
- SHA tracking separated from channel config; SHA only advances on a successful post so failed notifications retry on the next deploy.
- `sync_from_github()` now runs first on every startup — pulls `version.txt` and `updates_data.json` directly from GitHub so version and channel config are always correct regardless of deploy snapshot.
- `/grim_updates` — Toggles auto-posted patch notes in the current channel. On every deploy, Grim fetches new GitHub commits, lists changed files and commit messages, and posts an embed with the new version number.
- `/welcome_on` — Enables welcome messages for new members in the current channel. Sends a dark embed with Grim as author, the new member's username as title, server name as description, and their profile picture as thumbnail.
- `/welcome_off` — Disables welcome messages for the server. Settings persist across restarts via `welcome_data.json`.

- `/remind <subject> <when>` — Set a reminder for a drop or event. Grim sends a day-before ping and a day-of ping in the channel where the reminder was created. Subject can be a URL (auto-embedded by Discord) or plain text.
- `/reminders` — View all your active reminders with their IDs and scheduled dates. Admins see all server reminders.
- `/remind_cancel <id>` — Cancel an active reminder by its ID.
- Background task `check_reminders` runs every minute, fires notifications at the right time, and cleans up completed entries automatically. Wired into the health monitor for auto-restart.

### Fixed
- `/newsfeed_cancel` cancellations now persist across deployments. All bot data files (`newsfeed_data.json`, `ghostwrite_live_data.json`, `livetweet_data.json`, `nftwatch_data.json`, `moderation_data.json`, `reminders_data.json`) are now stored in `~/.grim_data/` — outside the git workspace — so `git pull` during deployments never overwrites them. Existing data is migrated automatically on first startup after the update.

---

## February 2026

### Added
- `/nftwatch <link>` — Monitor an OpenSea collection for live new listings. Posts an embed with image, price, token ID, and rarity data whenever a new item is listed.
- `/nftwatch_cancel` — Cancel active NFT watches via a Discord Select Menu dropdown.
- `nftwatch_data.json` — Persistent storage for active NFT watches.

---

## January 2026

### Added
- Auto-recovery health monitor (`check every 5 minutes`) — automatically restarts any crashed background task (newsfeed, livetweet, ghostwrite, nftwatch).
- `/newsfeed_status` — Status dashboard showing all background task health and active feed schedules with next run times.
- Support for multiple concurrent newsfeed instances per server/channel, tracked by UUID to avoid collisions.
- `/newsfeed_edit` — Edit the posting interval of an active newsfeed without stopping or restarting it.
- `/newsfeed_cancel` — Cancel active feeds via a Discord Select Menu. Shows all feeds for the server with topic and interval.
- Image embedding in newsfeed posts, with URL validation and a text-only fallback.
- Silent auto-delete moderation system:
  - `/mod_add <word>` — Add a banned word (admin only, ephemeral response).
  - `/mod_remove <word>` — Remove a banned word (admin only, ephemeral response).
  - `/mod_list` — List all banned words (admin only, ephemeral response).
  - Messages containing banned words are deleted silently with no notification.
  - `moderation_data.json` — Persistent storage for banned word list.
- `Quote` message context menu command (right-click → Apps → Quote) — posts a stylized embed with the quoted user's large profile picture and their message in curly quotes.

### Fixed
- Random quote embeds changed from 0.01% to 0.1% (approximately 1 in 1,000 eligible messages).
- Random joke messages removed.
- Random security paranoia messages expanded and changed from 0.5% to 0.2% (1 in 500 eligible messages).
- `/newsfeed_cancel`, `/newsfeed_edit`, and `/newsfeed_status` now correctly filter feeds by stored `guild_id` instead of relying on the unreliable `get_channel()` cache lookup.
- `guild_id` is now stored on all new feeds at creation time.
- @ mention replies now work correctly (resolved Message Content Intent configuration).
- `/creator` link updated to `deathi.net`.

---

## November 2025

### Added
- `/language [preference]` — Set or view a preferred language with a simpler command; `/grim_language` remains available as an alias.
- Initial bot setup for Seclude & Affiliates.
- `/server` — Display server status and member information.
- `/haiku` — Generate an AI-written inspirational haiku via xAI Grok.
- `/meme` — Generate creative meme captions.
- `/rizz` — Get AI-generated pickup lines.
- `/grim` — Chat with Grim as an AI assistant (full conversation context).
- `/livetweet <username>` — Monitor live tweets from an X/Twitter account and post them in a channel.
- `/ghostwrite <username> <topic>` — Generate tweet drafts written in someone's style.
- `/ghostwrite_live <interval> <username> <topic>` — Scheduled ghostwriting that posts on a repeating interval.
- `/newsfeed <interval> <topic>` — Start a live AI-generated news feed for any topic.
- `/creator` — Meet the creator of Grim.
- `/help_grim` — Display all available commands.
- xAI Grok API integration for all AI features (`grok-3` for chat, `grok-4-1-fast` with web search for newsfeed).
- Monochromatic dark aesthetic across all embeds (dark charcoal `rgb(18, 18, 18)`).
