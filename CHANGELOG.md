# Grim — Changelog

All notable changes to this project are documented here, organized by month.

---

## November 2026

### Added
- Grim replies when someone names him with a real entrance or need: a question, a ping for his take, or a direct address. An @mention is not required. Empty pings and drive-by name-drops get no reply.
- `/panopticon` — Founder-only toggle for the private deletion-report watch (including bulk delete and `/clear`). Defaults on. Run it to turn the watch off or back on. Only the Discord user ID in `FOUNDER_DISCORD_ID` / `GRIM_FOUNDER_ID` (VPS `.env`, never git) can use it. The switch persists under `~/.grim_data` so it survives restarts. When off, Grim does not DM those reports; `/clear` logs in the `/grim_updates` channel are unchanged.
- Deleted-message reports. Grim DMs the user ID in `FOUNDER_DISCORD_ID` (alias `GRIM_FOUNDER_ID`) when a guild message is deleted, including bulk delete: channel, who deleted it when Discord's audit log provides the actor (otherwise "actor unknown"), and the posted content when the message was cached or stored. These reports are not posted to `/grim_updates`, staff, or public channels. If DMs are closed, Grim logs the failure without message content and does not fall back to a server channel. `/panopticon` toggles those DMs on or off without a code change.
- Reply auditor (`reply_auditor.py`). By default it stays out of normal chat. It only steps in if a job or command runs longer than ten minutes, or if Grim hits an error (then a short "broke on my end" line). Optional burst/loop/voice pacing is off unless `GRIM_AUDITOR_PACE_CHAT` / `GRIM_AUDITOR_REVIEW` are on. If the auditor itself fails, Grim replies as before. Events are counts only under `~/.grim_data`. The OPSEC REPORT, DEFCON board, `/grim_opsec`, and heartbeats are unchanged.
- OPSEC REPORT heartbeat. Where `/grim_updates` is already on, Grim posts the report alone at noon and midnight Pacific. No audit, and no new channel. A restart in the first 20 minutes of a slot still posts that slot once.
- OPSEC REPORT (`grim_health.py`). The standing board in the `/grim_updates` channel is titled OPSEC REPORT: DEFCON under the title, then the overall security and mechanical health (1 is worst, 5 is healthy; Discord connectivity is posted, not scored), then what is healthy, what is degraded, counts, the last event, and a quick fix. Grim edits that one message. A fail or checkup posts a separate message in that channel, a fresh report with an AUDIT embed under it. `/grim_opsec` (admin, server-only) pulls the report on demand with no audit. `/grim_auditor` shows the saved event log.
- `/announce` and `/say` — administrator-only, guild-only. Grim posts text, an image, or both in the channel as itself, with no author shown. `/announce` reshapes the text into a monochrome Seclude announcement embed through the existing xAI path; `/say` fixes only spelling, grammar, and spacing and posts plain text. Images are downloaded, stripped of EXIF, GPS, camera, XMP, IPTC, comment, and timestamp metadata, and reuploaded under a neutral name; the preview shows that exact stripped file plus a one-line status such as "Metadata stripped: GPS, device, timestamp". When metadata was removed, a secondary "Post original…" button shows an ephemeral warning listing what the file carries, then posts the untouched original only after a second "Post original" click ("Back" posts nothing). Confirm always posts the stripped file. If an image can't be cleaned, Grim refuses it. The same shared cleaner (`image_clean.py`) runs on the `/quote` card. Both open a private preview of the exact post with Confirm and Cancel for the invoker; nothing is public before Confirm, and Confirm posts once. Pings are silent unless the admin typed them.
- `/zork-map` — a monospace sketch of the current room and the compass links that player has walked in that save. Unvisited exits stay blank. It does not probe the game or replace the panel. Only that player sees it.
- `/zork` — Zork I, II, or III on a private panel. A local Z-machine runs the MIT-licensed story files, so a move never calls xAI. One autosave per player per game lives under `~/.grim_data` and is included in the encrypted backup.
- `/trivia subject` — a private, dismissible ten-question quiz. Grim writes the round through the existing xAI path, shows one four-choice question at a time, and scores the invoking member out of 10. A bad subject or a failed generation stops the round instead of posting a partial quiz.

### Fixed
- `/grim_updates` deploy cards keep normal patch notes and real filenames on ordinary deploys. Filename hiding applies only when that deploy is explicitly labeled CLASSIFIED.
- Live-search replies still show blue source links, but Discord no longer unfurls those URLs into a stack of rich embeds under the message. Casual Grim messages still unfurl as usual.

### Changed
- `/panopticon` Discord command-picker description is `CLASSIFIED`. The Founder-only ID gate and on/off behavior are unchanged.
- OPSEC REPORT posts live Discord connectivity (online or disconnected, plus gateway ping when known) instead of the line "Connectivity is not part of this score." DEFCON scoring is unchanged. A receipt line at the bottom of the report shows the check time to the second in Pacific time; it is not on the XSI credit line. `/grim_opsec` and the noon/midnight Pacific heartbeats use the same template.
- @Grim live-searches only when the member is asking about facts, information, or method (roles, news, "is this true", lookups, how-to, causal/medical "can X cause Y"). Greetings, quotes, vibes, and personality stay on the chat path. Internet and live-search capability asks are bot state, not news: Grim says the tool is wired and proves it with a live sourced hit (empty/fail stays the capability line, never the news-miss line). The search is web-only unless the ask needs X, uses a short prompt and a fast `grok-4.3` payload (`reasoning.effort` none, 12s HTTP / 15s wait, 280 tokens), and does not retry a second round trip on web-only. An empty search is one Discord message of two lines: exactly `Live Search has zero info on this`, a newline, then Grim in regular demeanor as a casual wrap (no invented sources or training fill). Later pings in that channel talk normally and do not repeat the miss unless the member explicitly asks to search again.
- Grim's mention replies keep their substance in every mood. A cheap local check (`substance.py`) catches an empty reply, bare reaction words, stock lines, a dodge of a direct question, or a polished echo of the member's own message. A failed reply retries once with a stronger instruction, then falls back to a short honest line. Small talk stays short and casual. Restating is now an introspection tool for someone working something through, never the default when Grim has nothing to say. No facts are added to fill a gap. Live-sourced answers stay on their own path and are not rewritten by this check.
- `/say` and `/announce` acknowledge the command before anything else, then check the permission, download and clean the image, and ask xAI. A denial, a rejected input, or an unexpected failure now arrives as a private follow-up instead of leaving Discord on "Sending command…".
- A death on the Zork panel shows the grue line and "you have died" once, then the room you wake in once. The move is not echoed. Another reply that names more than one room keeps the current room, then the newest outcome. TERMINAL still lists every move.
- `/zork` HUD keeps the status line first, then the current room description, then the newest reply. A failed move leaves that room in place. Entering another room replaces it instead of stacking the old one.
- `/zork` opens on HUD: the latest message, with the room, score, and moves still on the panel. `/zork-ui` in that thread switches that player to TERMINAL, the full move list, and back. The choice is kept per player per game.
- `/trivia` questions sit in a tighter monochrome box. The round ends on one debrief: the score, each miss with the pick and the right answer, a short note on that answer, and a Grim lore line. Intellect and data boxes use the historical credit line, now reading `Powered by XSI`.
- Generated replies are checked locally before Grim shows them: a leaked trivia answer, empty or duplicate choices, a reply that repeats the user as fact, or an obvious self-contradiction. A clean reply makes no extra model call. A failure gets one xAI rewrite of that item, then it is dropped if it still fails.
- Casual @Grim talk is exempt from that check. Compliments, reactions, and follow-ups that are not asking for facts are sent as written. A factual question, lookup, "is this true", or trivia-like mention is still checked locally, rewritten once if it fails, and kept as the original reply if the rewrite also fails.
- A conversational @mention still gets the model reply when the shared external AI limit is full, and it never receives a failure line. A real miss on an accuracy ask says which miss it was: traffic, a crowded channel, or an empty model reply.
- Before Grim speaks, the prompt asks what the moment entails and how to be useful. The answer can be empathy, a direct answer, humor, transparency, teaching, or presence. A restate is for teaching. The shape still varies, and it is not a checklist or an X-post cadence. That note is on chat, commands, trivia notes, news, ghostwritten tweets, and chimes. Relayed livetweets, Reddit, and NFT posts are unchanged.

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
