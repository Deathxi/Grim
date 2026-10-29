# Grim — Changelog

All notable changes to this project are documented here, organized by month.

---

## October 2026

### Changed
- Repo hygiene: current dependency pins (`discord.py` 2.7.1 + `davey` for voice, `openai` 2.54.0 staying on 2.x, `aiohttp`/`tweepy` patch-minor bumps; `PyNaCl` held at 1.5.x per discord.py voice; Pillow held at 12.3), refreshed `uv.lock`, pinned deploy action to `appleboy/ssh-action` v1.2.5, PR test workflow, and docs aligned to the venv `ExecStart` path.
- Fixed Grim `ExecStart` to use `/root/grim/venv/bin/python3`; setup and deploy install requirements into that venv.
- Second VPS hardening pass: drop public `journalctl` on failed deploy, checkout read-only in `grim.service`, hard-reset deploy with hooks disabled, SSH host fingerprint secret, restricted `authorized_keys`, backup unit refresh, restore-work umask docs, backup history retention note.
- Hardened VPS deploy/setup paths: silent secret prompts, `~/.grim_data` mode `0700`, pinned `appleboy/ssh-action` SHA, deploy fails closed if Grim does not restart, systemd unit hardening, no runtime auto-pip for Pillow.
- Polished the post-Replit tree: removed unused helpers/imports (legacy `generate_reply`, unused news image search/fallback path, dead language/URL helpers), renamed the project package off `repl-nix-workspace`, dropped unused `python-dotenv`, and pointed missing-key messages at the VPS `.env`.
- Hardened the post-Replit path: accurate Privacy Policy for `~/.grim_data` / member history / encrypted backups; deleted Replit-era source sync (`push_to_github.py`, startup Contents:Write sync, GitHub state pull); VPS keeps read-only optional GitHub access for patch notes only; Discord and xAI terms unchanged.
- Peeled Replit host scaffolding (`.replit`, `replit.md`) and pointed agent/ops notes at Cursor (`AGENTS.md`, README credit); Discord and xAI terms unchanged.
- Added secure autonomous web research to normal Grim conversations, including current-topic follow-ups, verified HTTPS citations, current-channel-only context, bounded timeouts, server/user budgets, and redacted audit outcomes.
- Rebuilt `/ascii` to generate a recognizable xAI stencil image and convert its real silhouette into compact character art, with optional themes and curated offline fallbacks.
- Routed `/clear` audit transcripts to each server's `/grim_updates` channel, including the moderator, source channel or forum post, and deleted messages in chronological order; removed source-channel confirmations.
- Added moderator-only `/clear number` for removing up to 100 messages from the current channel or forum post.
- Fixed tagged replies to image messages by upgrading the xAI vision model, fetching uncached reply targets, recognizing Discord image MIME types, and using a bounded, transparent text fallback.
- Stopped lightweight code deployments from generating offline/crash after-reports by classifying heartbeat transitions with source revisions and dropping legacy false-positive reports.
- Allowed substantive conversational replies to continue across multiple Discord messages instead of failing at the character limit.
- Added `/gif topic:` for random KLIPY/GIPHY searches with persistent, per-server duplicate prevention for manual pulls.
- Added restrained conversational reaction GIFs using KLIPY with GIPHY fallback; Grim usually keeps his text and may use GIF-only replies for deliberate jokes.
- Stopped classifying graceful update deployments as outages; after-reports now cover only unexpected process or Discord connection failures.
- Changed rendered quote-card avatars from circular to square while preserving the random quote trigger.
- Render quoted messages as custom image cards with regular 24px text and the member avatar on the right.
- Split the offline after-report introduction into two lines for easier reading.

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
- `/grim_updates` channels now receive offline after-reports when Grim recovers, including the approximate Pacific-time outage window and downtime.
- Replying to a Discord message with `!quote` now quotes that exact message; the existing right-click Apps → Quote action and `/quote` latest-message command remain available.

### Fixed
- Saved `/language` preferences now apply consistently to the creator account as well as every other member; Auto remains the default.
- Random and manual quote embeds now use a larger, bold title treatment, with a bold fallback for quotes too long for Discord's title limit.

---

## June 2026

### Added
- `/support` — Shows contact info (`x@deathi.net`) and a link to the Seclude & Affiliates Discord hub/FAQ. Displays the server icon as a thumbnail once the CDN URL is configured.
- Version counter moved to `~/.grim_data/version_count.txt` (persists across redeploys). *(October 2026: counter stays under `~/.grim_data` only; the live host no longer rewrites or pushes project-root `version.txt`.)*
- `/grim_updates` channel config persistence and SHA tracking for deploy patch notes. *(October 2026: config lives under `~/.grim_data`; Replit-era GitHub Contents:Write sync and `sync_from_github()` were retired.)*
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
