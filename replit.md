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
- `/info` - Display server status and information
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

## Setup
1. Add your `DISCORD_TOKEN` as a secret
2. Add your `XAI_API_KEY` as a secret for AI features
3. Add `OPENSEA_API_KEY` as a secret for NFT watch features (free from opensea.io)
4. (Optional) Add `PEXELS_API_KEY` for dynamic news images
4. Run the bot using the workflow

## Dependencies
- discord.py - Discord API wrapper
- openai - OpenAI-compatible client for xAI API
- aiohttp - Async HTTP requests
- python-dotenv - Environment variable management
- tweepy - Twitter/X API wrapper

## Developer Notes
**REMINDER:** When adding new time-based or scheduled commands:
1. Add the task to `health_monitor()` function to enable auto-restart if it crashes
2. Add an `after_loop` handler to log unexpected stops
3. Update `/newsfeed_status` command to display the new task's status

## Recent Changes
- February 2026: /nftwatch command for live OpenSea listing monitoring with image/price/rarity embeds
- January 2026: Auto-recovery health monitor and /newsfeed_status dashboard
- January 2026: Multiple newsfeed instances with selective cancellation via Discord Select Menu
- January 2026: Image embedding for newsfeed with validation and fallback system
- November 2025: Initial bot setup with basic commands
