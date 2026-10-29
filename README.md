# Grim

Discord Bot for Seclude & Affiliates

Maintained with [Cursor Agent](https://cursor.com) as Death's development scribe.

## Terms of Service

By inviting or interacting with **Grim** (the Discord bot for Seclude & Affiliates), you agree to these terms. Last updated: November 27, 2025.

### 1. Usage & Permissions
- Grim is for personal, non-commercial use in authorized servers only. No reselling, cloning, or reverse-engineering.
- Respect Discord's ToS and Community Guidelines—we're all shadows in the same void.
- xAI integrations (powered by Grok) are subject to [xAI's terms](https://x.ai/terms). Rate limits apply; don't spam the abyss.

### 2. Content & AI Shenanigans
- Generated responses (text, vibes, or chaos) are experimental—use at your own risk. No liability for existential crises or bad roasts.
- Keep inputs SFW; Grim filters the profane, but you're responsible for your server's sanity.
- Grim stores operational data needed for bot function under the Privacy Policy below.

### 3. Prohibitions
- No harassment, scams, doxxing, or illegal summons. Bans are final; appeals go to the ether.
- Self-promo or spam? Exile to the outer rims.

### 4. Changes & Termination
- We can update these terms or yank Grim anytime. Continued use = acceptance.
- Questions? DM the devs in Seclude— we're listening in the dark.

By proceeding, you enter the pact. Enter if you dare. 🌑

## Privacy Policy
Last updated: October 29, 2026

Grim respects your shadows. Here’s exactly what is collected, where it lives, and how long it stays.

### 1. What We Collect
- **Discord user IDs, usernames, display names, and avatar URLs** — for member history, language preferences, stats, and staff directory views.
- **Server (guild) IDs and channel IDs** — for server-scoped settings, feeds, welcome/updates channels, and schedules.
- **Message content in guild channels Grim can see** — persisted for conversational context, digests, and member profile synthesis (not only when you @Grim).
- **Voice-channel presence timing** — aggregate time-in-voice totals for `/stats`.
- **Command and preference data** — reminders, language preferences, moderation word lists, welcome/member-log settings, feed schedules, and similar bot configuration.
- **Redacted security audit metadata** — action, outcome, actor/server IDs, and non-secret metadata (no message bodies); retained about 90 days.
- **Prompts sent to xAI (Grok)** — when Grim generates an AI reply; handled under [xAI's privacy policy](https://x.ai/legal/privacy-policy).

### 2. How It’s Used
- To operate Grim in authorized servers: replies, schedules, moderation helpers, staff member history, and deployment/outage notices.
- xAI sees prompt/context required to generate a reply. Discord sees what Grim posts back into the server.

### 3. Storage & Backups
- Runtime state lives on the VPS under `~/.grim_data` (outside the git checkout), including SQLite (`chat_history.db`) and JSON config/state files.
- **Member history events** are retained about **730 days**, then pruned.
- **Security audit events** are retained about **90 days**, then pruned.
- Chat history and other learned state persist across restarts until operators delete or restore them.
- Weekly **encrypted** archives of `~/.grim_data` are uploaded to a separate private GitHub repository configured by the operator. Archives exclude `.env`, credential-like filenames, and the backup passphrase. See `BACKUP_RESTORE.md`.
- The live host does **not** push Grim source code or runtime state into the public/source `Deathxi/Grim` repository.

### 4. Sharing
- Never sold.
- Shared only as needed to run the bot: Discord (messages Grim sends), xAI (prompts for AI replies), and the private encrypted-backup repository operators configure.
- Staff in a server may see member-directory and departure cards Grim posts to configured staff channels.

### 5. Deletion & Access
- Leaving a server or kicking Grim does **not** by itself wipe `~/.grim_data` or encrypted backups.
- For erasure or export requests, contact the operator (Deathxi / Seclude staff). Operators can delete or restore runtime files and manage backup retention per `BACKUP_RESTORE.md` and `OPERATIONS_SECURITY.md`.

### 6. Changes
We’ll update this section in the repo if anything changes. Continued use after updates = you’re cool with it.

That’s it. No cookies, no trackers, no advertising profiles. Stay hidden. 🌑

— Deathxi & the Deathly Hollows
