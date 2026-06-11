# Grim — Changelog

All notable changes to this project are documented here, organized by month.

---

## June 2026

### Added
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
- `/newsfeed_cancel`, `/newsfeed_edit`, and `/newsfeed_status` now correctly filter feeds by stored `guild_id` instead of relying on the unreliable `get_channel()` cache lookup.
- `guild_id` is now stored on all new feeds at creation time.
- @ mention replies now work correctly (resolved Message Content Intent configuration).
- `/creator` link updated to `deathi.net`.

---

## November 2025

### Added
- Initial bot setup for Seclude & Affiliates.
- `/info` — Display server status and member information.
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
- Monochromatic Tesla/X.com aesthetic across all embeds (dark charcoal `rgb(18, 18, 18)`).
