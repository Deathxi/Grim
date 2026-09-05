# Grim Discord Bot

## Overview
Grim is a Discord bot for Seclude & Affiliates. Built with Python and discord.py with xAI's Grok API integration.

## Project Structure
- `main.py` - Main bot file with commands and event handlers
- `newsfeed_data.json` - Persistent storage for active news feeds (keyed by feed UUID)
- `ghostwrite_live_data.json` - Persistent storage for ghostwrite live schedules
- `livetweet_data.json` - Persistent storage for live tweet monitors
- `nftwatch_data.json` - Persistent storage for NFT collection watches

## Commands
- `/server` - View Grim's server info: identity, structure, language signals, emojis, and banner
- `/quote` - Quote the latest non-Grim message; right-click a specific message and use Apps → Quote, or reply to it with `!quote`
- `/haiku` - Generate an inspirational haiku
- `/meme` - Generate creative meme captions
- `/rizz` - Get pickup lines for the brave
- `/grim` - Chat with Grim AI assistant
- `/livetweet <username>` - Monitor live tweets from an X account
- `/ghostwrite <username> <topic>` - Generate tweet drafts in someone's style
- `/ghostwrite_live <interval> <username> <topic>` - Scheduled ghostwriting
- `/newsfeed <interval> <topic>` - Start a live news feed (supports multiple instances)
- `/newsfeed_edit` - Edit the interval of an active news feed without disrupting flow
- `/newsfeed_cancel` - Cancel active news feeds via dropdown selector
- `/newsfeed_status` - View status dashboard with active feeds and next run times
- `/nftwatch <link>` - Watch an OpenSea collection for live new listings
- `/nftwatch_cancel` - Cancel active NFT watches via dropdown selector
- `/stats [user]` - View a member's all-time stats: messages sent, time in voice channels, ping, join date
- `/remindme <when> <text>` - Get a personal DM reminder; accepts a duration (e.g. `10m`, `2h`, `1d3h30m`) or an exact GMT date/time (e.g. `07/06/2026 17:00`)
- `/remindmes` - View your active personal reminders and their IDs
- `/remindme_cancel <id>` - Cancel one of your active personal reminders
- `/welcome_on` - Enable new member welcome messages in the current channel
- `/welcome_off` - Disable new member welcome messages
- `/members` - Open the staff-only, dismissible member history directory
- `/memberlog ENABLE|DISABLE` - Toggle departure notifications in the current private staff channel (channels visible to `@everyone` are rejected)
- `/language [preference]` - View or set your per-member reply language; use `Spanish`, `Romanian`, `German`, `Italian`, `Portuguese`, `Polish`, `Russian`, `Ukrainian`, `Greek`, `Japanese`, `Chinese`, `Korean`, `Arabic`, `Hindi`, or `Auto` to match each message
- `/grim_language [language]` - Backwards-compatible alias for `/language`
- `/grim_translate <language> <text>` - Translate text into any requested language using a common name or ISO language code
- `/creator` - Meet the creator of Grim
- `/help_grim` - Show available commands

## Features
- Multiple concurrent newsfeed instances per server/channel
- Discord Select Menu UI for managing active feeds
- Image embedding for news posts with fallback system
- Full UUID-based feed tracking for collision-free operation
- Auto-recovery: Health monitor restarts crashed background tasks every 5 minutes
- Status dashboard showing all background task health and feed schedules
- Live NFT listing monitor with image, price, token ID, and rarity data from OpenSea API
- Multilingual text replies automatically match the language of each member's message in every server; an optional per-member preference overrides auto-detection
- Explicit text translation into any requested language; no voice or speech features are included
- Persistent, server-scoped member identity and lifecycle history with staff-only paginated profile review and configurable departure cards; it stores no message contents in the tracker
- VPS-only weekly encrypted backups of `~/.grim_data` to a separate private GitHub repository; see `BACKUP_RESTORE.md`
- `/grim_updates` channels receive deployment patch notes and after-reports for unexpected process or Discord connection outages; normal update restarts are excluded

## Setup
1. Add your `DISCORD_TOKEN` as a secret
2. Add your `XAI_API_KEY` as a secret for AI features
3. Add `OPENSEA_API_KEY` as a secret for NFT watch features (free from opensea.io)
4. (Optional) Add `PEXELS_API_KEY` for dynamic news images
4. Run the bot using the workflow

## Dependencies
- discord.py - Discord API wrapper
- openai - OpenAI-compatible client for xAI API
- KLIPY and GIPHY - conversational reaction GIF search with cooldowns and serious-topic exclusions
- aiohttp - Async HTTP requests
- python-dotenv - Environment variable management
- tweepy - Twitter/X API wrapper

## Developer Notes
**REMINDER:** When adding new time-based or scheduled commands:
1. Add the task to `health_monitor()` function to enable auto-restart if it crashes
2. Add an `after_loop` handler to log unexpected stops
3. Update `/newsfeed_status` command to display the new task's status

## VPS backups
`backup_grim_data.py` is intentionally separate from the bot and reads only
`~/.grim_data`. `setup_vps.sh` can install its root-only systemd timer when
given a dedicated fine-grained GitHub token and an offline recovery
passphrase. It never stores backup credentials in the repository or Grim's
`.env`; see `BACKUP_RESTORE.md` for setup and restore steps.

## Recent Changes
- February 2026: /nftwatch command for live OpenSea listing monitoring with image/price/rarity embeds
- January 2026: Auto-recovery health monitor and /newsfeed_status dashboard
- January 2026: Multiple newsfeed instances with selective cancellation via Discord Select Menu
- January 2026: Image embedding for newsfeed with validation and fallback system
- November 2025: Initial bot setup with basic commands
