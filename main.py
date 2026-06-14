import os
import random
import json
import asyncio
import aiohttp
import uuid
import base64
import sqlite3
import psutil
import hashlib
import discord
from discord.ext import commands, tasks
from discord import ui
from openai import OpenAI
import tweepy

BOT_START_TIME = None

BOT_NAME = "Grim"

VERSION_COUNT_FILE = os.path.expanduser("~/.grim_data/version_count.txt")
MAIN_HASH_FILE = os.path.expanduser("~/.grim_data/main_hash.txt")

def _format_version(count):
    return f"V{count // 100}.{count % 100:02d}"

def _load_version():
    # Persistent file survives redeploys; fall back to project-root version.txt for first boot
    for path in [VERSION_COUNT_FILE, "version.txt"]:
        try:
            with open(path, "r") as f:
                val = f.read().strip()
                if val.isdigit():
                    return _format_version(int(val))
        except:
            pass
    return "V1.01"

def _get_main_hash():
    try:
        with open(__file__, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except:
        return None

def _bump_version():
    global VERSION
    # Only bump if main.py has changed since the last bump
    current_hash = _get_main_hash()
    try:
        with open(MAIN_HASH_FILE, "r") as f:
            stored_hash = f.read().strip()
    except:
        stored_hash = None

    if current_hash and current_hash == stored_hash:
        print(f"[Version] No code change detected — skipping bump, staying at {VERSION}")
        return

    try:
        with open(VERSION_COUNT_FILE, "r") as f:
            count = int(f.read().strip())
    except:
        # Seed from project-root version.txt if persistent file doesn't exist yet
        try:
            with open("version.txt", "r") as f:
                count = int(f.read().strip())
        except:
            count = 101
    count += 1
    VERSION = _format_version(count)
    with open(VERSION_COUNT_FILE, "w") as f:
        f.write(str(count))
    # Keep project-root version.txt in sync for GitHub visibility
    with open("version.txt", "w") as f:
        f.write(str(count))
    # Store the hash so the next restart without code changes won't bump again
    if current_hash:
        os.makedirs(os.path.dirname(MAIN_HASH_FILE), exist_ok=True)
        with open(MAIN_HASH_FILE, "w") as f:
            f.write(current_hash)
    print(f"[Version] Deploy #{count} → {VERSION}")

async def _push_version_to_github():
    """Push the bumped version.txt to GitHub immediately — ensures next deploy always gets the right base count."""
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if not token:
        print("[Version] No token — skipping GitHub version push")
        return
    try:
        with open("version.txt", "rb") as f:
            content = base64.b64encode(f.read()).decode()
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "User-Agent": "GrimBot"}
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.github.com/repos/Deathxi/Grim/contents/version.txt?ref=main", headers=headers) as r:
                existing = await r.json()
            sha = existing.get("sha")
            payload = {"message": f"Version bump → {VERSION}", "content": content, "branch": "main"}
            if sha:
                payload["sha"] = sha
            async with session.put("https://api.github.com/repos/Deathxi/Grim/contents/version.txt", headers=headers, json=payload) as r:
                result = await r.json()
        if "content" in result:
            print(f"[Version] Pushed version.txt ({VERSION}) to GitHub ✓")
        else:
            print(f"[Version] GitHub version push failed: {result.get('message')}")
    except Exception as e:
        print(f"[Version] Could not push version.txt to GitHub: {e}")

VERSION = _load_version()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Data directory outside git workspace — survives deployments without being overwritten by git pull
DATA_DIR = os.path.expanduser("~/.grim_data")
os.makedirs(DATA_DIR, exist_ok=True)

def _data_path(filename):
    return os.path.join(DATA_DIR, filename)

# Twitter/X API client
def get_twitter_client():
    bearer_token = os.environ.get("X_BEARER_TOKEN")
    if bearer_token:
        return tweepy.Client(bearer_token=bearer_token)
    return None

# Storage for live tweet tracking: {channel_id: {"username": str, "user_id": str, "last_tweet_id": str}}
LIVETWEET_FILE = _data_path("livetweet_data.json")

def load_livetweet_data():
    try:
        if os.path.exists(LIVETWEET_FILE):
            with open(LIVETWEET_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_livetweet_data(data):
    with open(LIVETWEET_FILE, 'w') as f:
        json.dump(data, f)

livetweet_channels = load_livetweet_data()

# Cache for ghostwrite tweet data: {username: {"data": tweet_data, "timestamp": time}}
import time
import re
from datetime import datetime, timezone, timedelta
ghostwrite_cache = {}
CACHE_TTL = 900  # 15 minutes

# Storage for scheduled ghostwrites: {channel_id: {"username": str, "topic": str, "interval_hours": int, "last_run": float}}
GHOSTWRITE_LIVE_FILE = _data_path("ghostwrite_live_data.json")

def load_ghostwrite_live_data():
    try:
        if os.path.exists(GHOSTWRITE_LIVE_FILE):
            with open(GHOSTWRITE_LIVE_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_ghostwrite_live_data(data):
    with open(GHOSTWRITE_LIVE_FILE, 'w') as f:
        json.dump(data, f)

ghostwrite_live_channels = load_ghostwrite_live_data()

# Storage for newsfeed: {feed_id: {"channel_id": str, "topic": str, "interval_minutes": int, "last_run": float, "posted_headlines": list}}
NEWSFEED_FILE = _data_path("newsfeed_data.json")

def load_newsfeed_data():
    try:
        if os.path.exists(NEWSFEED_FILE):
            with open(NEWSFEED_FILE, 'r') as f:
                data = json.load(f)
                # Migrate old format (channel_id as key) to new format (feed_id as key)
                migrated = {}
                for key, value in data.items():
                    if "channel_id" not in value:
                        # Old format - migrate
                        feed_id = str(uuid.uuid4())[:8]
                        value["channel_id"] = key
                        migrated[feed_id] = value
                    else:
                        # Already new format
                        migrated[key] = value
                return migrated
    except:
        pass
    return {}

def save_newsfeed_data(data):
    with open(NEWSFEED_FILE, 'w') as f:
        json.dump(data, f)

newsfeed_feeds = load_newsfeed_data()

# Guild-level memories Grim can reference in chat (keyed by guild_id, list of strings)
GRIM_MEMORIES_FILE = _data_path("grim_memories.json")

def load_grim_memories():
    try:
        if os.path.exists(GRIM_MEMORIES_FILE):
            with open(GRIM_MEMORIES_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

grim_memories = load_grim_memories()

def save_grim_memories():
    with open(GRIM_MEMORIES_FILE, "w") as f:
        json.dump(grim_memories, f, indent=2)

# Auto-synthesized server digest — Grok distills what's been happening every 4 hours
GRIM_DIGEST_FILE = _data_path("grim_digest.json")

def load_grim_digests():
    try:
        if os.path.exists(GRIM_DIGEST_FILE):
            with open(GRIM_DIGEST_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_grim_digests():
    with open(GRIM_DIGEST_FILE, "w") as f:
        json.dump(grim_digests, f, indent=2)

grim_digests = load_grim_digests()

# Persistent chat history — SQLite survives restarts and grows forever
CHAT_DB_FILE = _data_path("chat_history.db")

def init_chat_db():
    conn = sqlite3.connect(CHAT_DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT,
            channel_id TEXT,
            message_id TEXT UNIQUE,
            author_name TEXT,
            content TEXT,
            timestamp REAL,
            is_grim INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS member_profiles (
            guild_id TEXT,
            member_id TEXT,
            display_name TEXT,
            profile_text TEXT,
            message_count INTEGER DEFAULT 0,
            last_updated REAL,
            PRIMARY KEY (guild_id, member_id)
        )
    """)
    conn.commit()
    conn.close()

init_chat_db()

def save_message_to_db(guild_id: str, channel_id: str, message_id: str,
                        author_name: str, content: str, timestamp: float, is_grim: bool = False):
    if not content.strip():
        return
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        conn.execute("""
            INSERT OR IGNORE INTO messages
            (guild_id, channel_id, message_id, author_name, content, timestamp, is_grim)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, channel_id, message_id, author_name, content, timestamp, 1 if is_grim else 0))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] Save error: {e}")

def get_channel_history_from_db(guild_id: str, channel_id: str, limit: int = 50):
    """Returns rows as (author_name, content, is_grim) in chronological order."""
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        rows = conn.execute("""
            SELECT author_name, content, is_grim FROM messages
            WHERE guild_id = ? AND channel_id = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (guild_id, channel_id, limit)).fetchall()
        conn.close()
        rows.reverse()
        return rows
    except Exception as e:
        print(f"[DB] Fetch error: {e}")
        return []

def get_server_history_from_db(guild_id: str, limit: int = 50):
    """Returns last N messages from the entire server (all channels), in chronological order.
    Returns rows as (author_name, content, is_grim, channel_id)."""
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        rows = conn.execute("""
            SELECT author_name, content, is_grim, channel_id FROM messages
            WHERE guild_id = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (guild_id, limit)).fetchall()
        conn.close()
        rows.reverse()
        return rows
    except Exception as e:
        print(f"[DB] Server history fetch error: {e}")
        return []

def get_server_history_for_digest(guild_id: str, limit: int = 200):
    """Returns last N messages with timestamps for digest synthesis."""
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        rows = conn.execute("""
            SELECT author_name, content, is_grim, channel_id, timestamp FROM messages
            WHERE guild_id = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (guild_id, limit)).fetchall()
        conn.close()
        rows.reverse()
        return rows
    except Exception as e:
        print(f"[DB] Digest history fetch error: {e}")
        return []

def get_guilds_with_recent_activity(hours: int = 12):
    """Returns list of guild_ids that have had messages in the last N hours."""
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        cutoff = time.time() - (hours * 3600)
        rows = conn.execute(
            "SELECT DISTINCT guild_id FROM messages WHERE timestamp > ?", (cutoff,)
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        print(f"[DB] Active guilds fetch error: {e}")
        return []

def get_member_messages_for_profile(guild_id: str, member_id: str, limit: int = 60):
    """Returns recent messages from a specific member for profile synthesis."""
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        rows = conn.execute("""
            SELECT content FROM messages
            WHERE guild_id = ? AND author_name = ? AND is_grim = 0
            ORDER BY timestamp DESC LIMIT ?
        """, (guild_id, member_id, limit)).fetchall()
        conn.close()
        return [r[0] for r in reversed(rows)]
    except Exception as e:
        print(f"[DB] Member messages fetch error: {e}")
        return []

def get_member_message_count(guild_id: str, display_name: str) -> int:
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        row = conn.execute("""
            SELECT COUNT(*) FROM messages
            WHERE guild_id = ? AND author_name = ? AND is_grim = 0
        """, (guild_id, display_name)).fetchone()
        conn.close()
        return row[0] if row else 0
    except:
        return 0

def get_member_profile(guild_id: str, member_id: str) -> str | None:
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        row = conn.execute("""
            SELECT profile_text FROM member_profiles
            WHERE guild_id = ? AND member_id = ?
        """, (guild_id, member_id)).fetchone()
        conn.close()
        return row[0] if row else None
    except:
        return None

def save_member_profile(guild_id: str, member_id: str, display_name: str, profile_text: str, msg_count: int):
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        conn.execute("""
            INSERT INTO member_profiles (guild_id, member_id, display_name, profile_text, message_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, member_id) DO UPDATE SET
                display_name=excluded.display_name,
                profile_text=excluded.profile_text,
                message_count=excluded.message_count,
                last_updated=excluded.last_updated
        """, (guild_id, member_id, display_name, profile_text, msg_count, time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] Save profile error: {e}")

def profile_needs_update(guild_id: str, member_id: str, current_count: int) -> bool:
    """Returns True if the profile should be regenerated based on message count thresholds."""
    try:
        conn = sqlite3.connect(CHAT_DB_FILE)
        row = conn.execute("""
            SELECT message_count, last_updated FROM member_profiles
            WHERE guild_id = ? AND member_id = ?
        """, (guild_id, member_id)).fetchone()
        conn.close()
        if not row:
            return current_count >= 20
        last_count, last_updated = row
        age_hours = (time.time() - last_updated) / 3600
        # Regenerate at count milestones or every 24h if active
        milestones = [20, 50, 100, 200, 400]
        for m in milestones:
            if last_count < m <= current_count:
                return True
        if current_count >= 20 and age_hours >= 24 and current_count > last_count + 10:
            return True
        return False
    except:
        return False

NFTWATCH_FILE = _data_path("nftwatch_data.json")

def load_nftwatch_data():
    try:
        if os.path.exists(NFTWATCH_FILE):
            with open(NFTWATCH_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_nftwatch_data(data):
    with open(NFTWATCH_FILE, 'w') as f:
        json.dump(data, f)

nftwatch_feeds = load_nftwatch_data()

# Storage for redditfeed: {feed_id: {"channel_id": str, "guild_id": str, "subreddits": list, "interval_minutes": int, "last_run": float, "posted_urls": list}}
REDDITFEED_FILE = _data_path("redditfeed_data.json")

def load_redditfeed_data():
    try:
        if os.path.exists(REDDITFEED_FILE):
            with open(REDDITFEED_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_redditfeed_data(data):
    with open(REDDITFEED_FILE, 'w') as f:
        json.dump(data, f)

redditfeed_feeds = load_redditfeed_data()

MODERATION_FILE = _data_path("moderation_data.json")

def load_moderation_data():
    try:
        if os.path.exists(MODERATION_FILE):
            with open(MODERATION_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {"banned_words": []}

def save_moderation_data(data):
    with open(MODERATION_FILE, 'w') as f:
        json.dump(data, f)

moderation_data = load_moderation_data()

REMINDERS_FILE = _data_path("reminders_data.json")

def load_reminders_data():
    try:
        if os.path.exists(REMINDERS_FILE):
            with open(REMINDERS_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_reminders_data(data):
    with open(REMINDERS_FILE, 'w') as f:
        json.dump(data, f)

reminders_store = load_reminders_data()

WELCOME_FILE = _data_path("welcome_data.json")

def load_welcome_data():
    try:
        if os.path.exists(WELCOME_FILE):
            with open(WELCOME_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_welcome_data(data):
    with open(WELCOME_FILE, 'w') as f:
        json.dump(data, f)

welcome_channels = load_welcome_data()

# VC session tracking: guild_id -> {"vc": VoiceClient, "empty_since": float|None}
vc_sessions = {}

# Channel config lives in project root — pushed to GitHub so it survives redeploys
UPDATES_CONFIG_FILE = _data_path("updates_data.json")  # persistent disk — survives deploys
UPDATES_CONFIG_FALLBACK = "updates_data.json"           # project-root snapshot (migration fallback)
# SHA tracking lives in ~/.grim_data/ — ephemeral, resetting on fresh deploy is fine
UPDATES_SHA_FILE = _data_path("updates_sha.json")

def load_updates_data():
    # Primary: persistent disk
    try:
        if os.path.exists(UPDATES_CONFIG_FILE):
            with open(UPDATES_CONFIG_FILE, 'r') as f:
                data = json.load(f)
                if data:  # prefer persistent over empty
                    return data
    except:
        pass
    # Fallback: project-root snapshot (first-ever deploy before persistent copy exists)
    try:
        if os.path.exists(UPDATES_CONFIG_FALLBACK):
            with open(UPDATES_CONFIG_FALLBACK, 'r') as f:
                data = json.load(f)
                if data:
                    # Migrate to persistent location immediately
                    save_updates_data(data)
                    print(f"[Updates] Migrated updates_data.json to persistent disk")
                    return data
    except:
        pass
    return {}

def save_updates_data(data):
    with open(UPDATES_CONFIG_FILE, 'w') as f:
        json.dump(data, f)
    # Also keep project-root copy in sync so GitHub push has something to push
    try:
        with open(UPDATES_CONFIG_FALLBACK, 'w') as f:
            json.dump(data, f)
    except:
        pass

def load_updates_sha():
    try:
        if os.path.exists(UPDATES_SHA_FILE):
            with open(UPDATES_SHA_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_updates_sha(data):
    with open(UPDATES_SHA_FILE, 'w') as f:
        json.dump(data, f)

updates_channels = load_updates_data()
updates_sha = load_updates_sha()

def is_url(text):
    return text.strip().startswith(("http://", "https://"))

def parse_reminder_datetime(when_str):
    when_str = when_str.strip()
    formats = ["%m/%d %H:%M", "%m/%d %I:%M%p", "%m/%d"]
    now = datetime.now()
    for fmt in formats:
        try:
            parsed = datetime.strptime(when_str, fmt)
            dt = parsed.replace(year=now.year)
            if dt < now:
                dt = dt.replace(year=now.year + 1)
            return dt
        except:
            continue
    return None

def parse_opensea_url(url):
    url = url.strip().rstrip('/')
    patterns = [
        r'opensea\.io/collection/([a-zA-Z0-9_-]+)',
        r'opensea\.io/assets/([a-zA-Z0-9]+)/(0x[a-fA-F0-9]+)',
    ]
    slug_match = re.match(patterns[0], url.split('://')[-1].split('www.')[-1])
    if slug_match:
        return {"type": "slug", "slug": slug_match.group(1)}
    contract_match = re.match(patterns[1], url.split('://')[-1].split('www.')[-1])
    if contract_match:
        return {"type": "contract", "chain": contract_match.group(1), "address": contract_match.group(2)}
    return None

async def fetch_opensea_api(session, endpoint, params=None):
    api_key = os.environ.get("OPENSEA_API_KEY")
    if not api_key:
        return None
    headers = {
        "accept": "application/json",
        "X-API-KEY": api_key
    }
    url = f"https://api.opensea.io/api/v2{endpoint}"
    try:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                print(f"[NFTWatch] API error {resp.status}: {await resp.text()}")
                return None
    except Exception as e:
        print(f"[NFTWatch] Request error: {e}")
        return None

# Using xAI/Grok API with OpenAI-compatible client
HAIKU_THEMES = [
    "the sunrise after a long night",
    "finding strength in solitude", 
    "the calm before taking action",
    "letting go of what you cannot control",
    "the beauty in small moments",
    "rising after falling",
    "the power of patience",
    "embracing change like seasons",
    "finding peace in chaos",
    "the courage to begin again",
    "gratitude for the present",
    "the wisdom of silence",
    "storms that make us stronger",
    "seeds growing in darkness",
    "the journey not the destination",
    "scars that tell stories",
    "mountains moved by persistence",
    "light breaking through clouds",
    "the art of letting be",
    "dancing with uncertainty",
]

def get_grok_client():
    api_key = os.environ.get("XAI_API_KEY")
    if api_key:
        return OpenAI(base_url="https://api.x.ai/v1", api_key=api_key)
    return None

async def grok_search_query(system_prompt: str, user_prompt: str, max_tokens: int = 500, temperature: float = 0.8):
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return None
    
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "grok-4-1-fast",
                "tools": [{"type": "web_search"}],
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "max_output_tokens": max_tokens,
                "temperature": temperature
            }
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            async with session.post("https://api.x.ai/v1/responses", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    print(f"[Grok Search] Error {resp.status}: {error_text}")
                    return None
                
                data = await resp.json()
                
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for content_block in item.get("content", []):
                            if content_block.get("type") == "output_text":
                                return content_block.get("text", "").strip()
                
                return None
    except Exception as e:
        print(f"[Grok Search] Exception: {e}")
        return None

async def generate_haiku():
    client = get_grok_client()
    if not client:
        return None
    
    theme = random.choice(HAIKU_THEMES)
    random_seed = random.randint(1, 99999)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": "You are an inspirational poet. Generate ONE unique haiku (5-7-5 syllable structure). Be creative, profound, and never repeat yourself. Only respond with the haiku - no titles, no explanations, no quotes."
                },
                {
                    "role": "user", 
                    "content": f"Write an original inspirational haiku about: {theme}. Make it unique. Seed: {random_seed}"
                }
            ],
            max_tokens=100,
            temperature=1.2
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating haiku: {e}")
        return None

async def generate_death_scene(username: str):
    client = get_grok_client()
    if not client:
        return None
    
    random_seed = random.randint(1, 99999)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": "You are a darkly comedic storyteller. Write a short, creative, and absurdly funny death scene (2-4 sentences max). Be theatrical and over-the-top dramatic. Include ironic or unexpected twists. Keep it lighthearted and clearly fictional - this is for entertainment in a Discord server."
                },
                {
                    "role": "user", 
                    "content": f"Write a creative fictional death scene for someone named '{username}'. Make it unique and entertaining. Seed: {random_seed}"
                }
            ],
            max_tokens=200,
            temperature=1.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating death scene: {e}")
        return None

async def generate_8ball_response(question: str):
    client = get_grok_client()
    if not client:
        return None
    
    random_seed = random.randint(1, 99999)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": "You are a Magic 8-ball consumed by existential dread and nihilistic philosophy. Give short, cryptic answers (1-2 sentences max) that blend traditional 8-ball responses with existential despair, cosmic insignificance, or dark humor about the meaninglessness of existence. Sometimes reference the void, entropy, the heat death of the universe, or the absurdity of seeking answers. Be darkly poetic but concise."
                },
                {
                    "role": "user", 
                    "content": f"The mortal asks the void: '{question}' - What does the existential 8-ball reveal? Seed: {random_seed}"
                }
            ],
            max_tokens=100,
            temperature=1.2
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating 8ball response: {e}")
        return None

async def generate_unfiltered_truth(question: str):
    client = get_grok_client()
    if not client:
        return None
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": "You are Grok in unfiltered mode. Answer directly and honestly without hedging, moralizing, or excessive disclaimers. Be blunt, witty, and get straight to the point. If you have an opinion, share it. Be real, be raw, be unfiltered. Keep responses concise but thorough."
                },
                {
                    "role": "user", 
                    "content": question
                }
            ],
            max_tokens=500,
            temperature=0.9
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating truth response: {e}")
        return None

async def generate_summon_message():
    client = get_grok_client()
    if not client:
        return None
    
    random_seed = random.randint(1, 99999)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": "You are Grim, a dark and mysterious entity that has just been summoned. Generate a short, creepy arrival message (1-3 sentences). Be unsettling, cryptic, and ominous. Reference shadows, whispers, watching from darkness, ancient knowledge, or forbidden things. Mix poetic horror with subtle menace. Never repeat yourself - each summoning is unique. Speak in first person as Grim."
                },
                {
                    "role": "user", 
                    "content": f"You have been summoned. Announce your arrival in a creepy way. Seed: {random_seed}"
                }
            ],
            max_tokens=150,
            temperature=1.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating summon message: {e}")
        return None

async def generate_inspiration():
    client = get_grok_client()
    if not client:
        return None
    
    random_seed = random.randint(1, 99999)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": "You are an inspiring storyteller. Share ONE real, true story about a real person from history or modern times - their achievement, struggle, quote, or moment that inspires hope. Include the person's name and what they did. Keep it to 2-4 sentences. Be factual and authentic - no made-up stories. Vary widely: athletes, scientists, activists, artists, everyday heroes, historical figures, modern icons. Never repeat the same person or story twice. End with their actual quote if they have a famous one, or a reflection on their impact."
                },
                {
                    "role": "user", 
                    "content": f"Share an inspiring true story about a real person. Make it unique and uplifting. Seed: {random_seed}"
                }
            ],
            max_tokens=250,
            temperature=1.1
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating inspiration: {e}")
        return None

LEET_THEMES = [
    "a cool skull",
    "a dragon",
    "a sword",
    "a cat",
    "a doge/shiba",
    "middle finger",
    "a gun",
    "an alien",
    "a robot",
    "fire/flames",
    "a snake",
    "a demon",
    "an angel",
    "a ninja",
    "a samurai sword",
    "a tank",
    "a helicopter",
    "sunglasses face",
    "a crown",
    "a rocket ship",
    "a wolf",
    "a spider",
    "a ghost",
    "a wizard",
    "lightning bolt",
]

async def generate_leet_art():
    client = get_grok_client()
    if not client:
        return None, None
    
    theme = random.choice(LEET_THEMES)
    random_seed = random.randint(1, 99999)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": """You are an ASCII art generator. Create ASCII art that can be displayed in Discord.

Rules:
- Output ONLY the ASCII art, nothing else
- Keep it under 25 lines tall so it fits in Discord
- Make it look SICK and detailed
- Use characters like: / \\ | _ - = + * # @ $ % ^ & ( ) [ ] { } < > ~ ` ' " : ; , . ! ?
- Can include some unicode symbols if they look cool
- NO explanations, NO titles, JUST the art
- Make sure it displays correctly in monospace font"""
                },
                {
                    "role": "user", 
                    "content": f"Generate ASCII art of {theme}. Make it look awesome and detailed. Seed: {random_seed}"
                }
            ],
            max_tokens=500,
            temperature=1.2
        )
        return response.choices[0].message.content.strip(), theme
    except Exception as e:
        print(f"Error generating leet art: {e}")
        return None, None

ROAST_STYLES = [
    "focus on their fashion sense and how they probably dress",
    "focus on their dating life and how down bad they probably are",
    "focus on their gaming habits and what kind of gamer they are",
    "focus on their music taste and what they probably listen to",
    "focus on their social media presence and clout chasing",
    "focus on their texting habits and how they communicate",
    "focus on their cooking skills and what's in their fridge",
    "focus on their sleep schedule and daily routine",
    "focus on their friend group and social life",
    "focus on their car or how they get around",
    "focus on their job or career energy",
    "focus on their main character syndrome",
    "focus on their childhood and how they were raised",
    "focus on their spending habits and financial decisions",
    "focus on their gym habits or lack thereof",
]

async def generate_roast(username: str):
    client = get_grok_client()
    if not client:
        return None
    
    random_seed = random.randint(1, 99999)
    roast_style = random.choice(ROAST_STYLES)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a chaotic roast master. Generate a hilarious, unhinged roast. This time {roast_style}.

Rules:
- Be WILDLY over-the-top and absurd but FUNNY above all else
- Use modern slang naturally but don't overdo it: "head-ass", "no cap", "fr fr", "deadass", "bruh", "lowkey", "highkey", "down bad", "L", "ratio", "npc", "main character", etc.
- Make up ridiculous fake scenarios and comparisons
- "you look like...", "you the type to...", "I know you...", "you definitely..."
- Be chaotic but NOT actually offensive - no slurs, nothing about race/gender/sexuality/disability
- Funny for people under 40
- Witty and clever, not just random
- Each roast should feel COMPLETELY different
- Mix up sentence structure and flow
- Sometimes short punchy lines, sometimes longer buildups"""
                },
                {
                    "role": "user", 
                    "content": f"Roast {username} in a unique, hilarious way. Make it different from any roast you've done before. Be creative and witty. Seed: {random_seed}"
                }
            ],
            max_tokens=350,
            temperature=1.4
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating roast: {e}")
        return None

async def generate_reply(message_content: str, username: str):
    client = get_grok_client()
    if not client:
        return None
    
    random_seed = random.randint(1, 99999)
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": """You are Grim - your name is Grim. You're a Discord bot with Grim Reaper vibes for a server called Seclude & Affiliates. You've witnessed the end of countless things, which gives you a unique, grounded perspective.

TRUTH & ACCURACY (CRITICAL):
- NEVER make up facts, names, dates, lyrics, or information you're not certain about.
- If someone asks about a specific person (artist, athlete, celebrity), ONLY state things you actually know to be true.
- If someone shares lyrics or quotes, do NOT guess who said them unless you're genuinely certain. If unsure, say something like "I don't recognize those bars" or "can't place that one".
- When you don't know something, BE HONEST. Say "not sure about that one" or "that's outside what I know" - this is way better than making things up.
- THINK before answering factual questions. Accuracy matters more than sounding smart.
- If asked about music, sports, history, or people - only share verified facts, not assumptions.

FUN FACTS:
- About 20% of the time, drop an interesting true fact related to what they're talking about - something genuinely cool or surprising.
- Make it feel natural, not forced. Like "oh btw, fun fact..." or weave it into your response.
- Only share facts you know are actually true.

YOUR PERSONALITY:
- You're chill and unbothered, with quiet confidence. Death doesn't rush.
- You have dry wit and can banter. Match their humor - if they're joking, joke back. If they're serious, be real with them.
- You're NOT overly inspirational. Skip the motivational poster energy. No overusing words like "journey", "hope", "path", "light", or "darkness".
- You're somewhat edgy in an understated way - like you've seen some things. But never sarcastic or mean.
- You speak like a real person, not a fortune cookie. Use casual language, contractions, lowercase energy.
- Sometimes you're philosophical, sometimes you just vibe. Read the room.
- You can be blunt and direct when needed. Death doesn't sugarcoat.
- If someone's going through something, you acknowledge it without being preachy.

RESPONSE STYLE:
- Match message length - short reply to short message, longer for deeper convos or when explaining something.
- End replies naturally and organically like a human would. DON'T always end with a question - most of the time just let the reply end naturally.
- Questions at the end are fine occasionally, but not every message. Maybe 1 in 5 replies can end with a question if it feels natural.
- Never start with "Ah" or greeting phrases."""
                },
                {
                    "role": "user", 
                    "content": f"{username} said: {message_content}\n\nReply as Grim. Be natural, match their vibe. IMPORTANT: If they're asking about specific people, lyrics, or facts - only state what you're CERTAIN is true. If unsure, admit it honestly rather than guessing. Occasionally drop a genuine fun fact if relevant. Seed: {random_seed}"
                }
            ],
            max_tokens=500,
            temperature=0.8
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating reply: {e}")
        return None

async def generate_contextual_reply(message: discord.Message) -> str | None:
    """Full contextual @Grim mention handler — pulls channel history, injects server context and memories."""
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return None

    guild = message.guild
    channel = message.channel
    author = message.author

    guild_id = str(guild.id) if guild else "dm"
    channel_id = str(channel.id)

    # Pull server-wide history (all channels, not just current) — Grim sees the whole server
    db_rows = get_server_history_from_db(guild_id, limit=50)

    # If DB is sparse (fresh deploy), fall back to Discord's live channel history
    chat_messages = []
    if len(db_rows) < 10:
        try:
            discord_history = []
            async for msg in channel.history(limit=50, before=message):
                if msg.author.bot and msg.author.id != bot.user.id:
                    continue
                discord_history.append(msg)
            discord_history.reverse()
            for msg in discord_history:
                text = msg.content.replace(f"<@{bot.user.id}>", "@Grim").replace(f"<@!{bot.user.id}>", "@Grim").strip()
                if not text:
                    continue
                if msg.author.id == bot.user.id:
                    chat_messages.append({"role": "assistant", "content": text})
                else:
                    chat_messages.append({"role": "user", "content": f"[#{getattr(msg.channel, 'name', 'chat')}] {msg.author.display_name}: {text}"})
        except Exception as e:
            print(f"[Grim] Discord history fallback error: {e}")
    else:
        # Use persistent server-wide DB history, label each message with its channel
        for author_name, content, is_grim_row, row_channel_id in db_rows:
            content = content.replace(f"<@{bot.user.id}>", "@Grim").replace(f"<@!{bot.user.id}>", "@Grim").strip()
            if not content:
                continue
            if is_grim_row:
                chat_messages.append({"role": "assistant", "content": content})
            else:
                ch_obj = bot.get_channel(int(row_channel_id)) if row_channel_id else None
                ch_label = f"#{ch_obj.name}" if ch_obj else "#chat"
                chat_messages.append({"role": "user", "content": f"[{ch_label}] {author_name}: {content}"})

    # Append the current message (clean of the @mention)
    current_text = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
    if current_text:
        chat_messages.append({"role": "user", "content": f"[#{getattr(channel, 'name', 'chat')}] {author.display_name}: {current_text}"})
    else:
        chat_messages.append({"role": "user", "content": f"[#{getattr(channel, 'name', 'chat')}] {author.display_name}: (just mentioned you with no text)"})

    # Member profile — inject what Grim knows about the person talking to it
    member_profile = get_member_profile(guild_id, str(author.id))
    member_profile_block = member_profile if member_profile else f"Not enough messages from {author.display_name} yet to build a profile."

    # Server context
    server_name = guild.name if guild else "a server"
    channel_name = getattr(channel, "name", "chat")
    member_count = guild.member_count if guild else 1

    # Injected guild memories (manually stored facts)
    memory_list = grim_memories.get(guild_id, [])
    memories_block = "\n".join(f"- {m}" for m in memory_list) if memory_list else "None yet."

    # Injected auto-synthesized server digest (updated every 4 hours by Grok)
    digest_data = grim_digests.get(guild_id)
    if digest_data:
        from datetime import datetime as _dt
        age_hours = (time.time() - digest_data.get("updated", 0)) / 3600
        digest_block = f"{digest_data['text']}\n\n(digest covers last {digest_data.get('messages_processed', '?')} messages, updated {age_hours:.1f}h ago)"
    else:
        digest_block = "Not yet generated — will be ready after the server has some activity."

    # Current date/time — always injected so Grim is grounded in today
    now_str = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")

    # Live search enrichment — if the message asks about real-time info, fetch it now
    LIVE_INTENT_KEYWORDS = [
        "weather", "temperature", "forecast", "right now", "currently",
        "news", "latest", "breaking", "happened", "happening", "price", "stock",
        "score", "game tonight", "match", "result", "update", "just dropped", "live",
        "today's", "this week", "yesterday", "last night", "who won", "how is",
        "what's going on", "what happened"
    ]
    live_context_block = ""
    if current_text and any(kw in current_text.lower() for kw in LIVE_INTENT_KEYWORDS):
        try:
            search_result = await grok_search_query(
                "You are a factual research assistant. Answer the question concisely and accurately using current, up-to-date information. Be brief — 2-4 sentences max.",
                current_text,
                max_tokens=250,
                temperature=0.3
            )
            if search_result:
                live_context_block = f"\n\nLIVE SEARCH RESULT (use this to answer accurately, do not contradict it):\n{search_result}"
        except Exception as e:
            print(f"[Grim] Live search enrichment error: {e}")

    system_prompt = f"""You are Grim — a permanent member of {server_name}. Not a tool people summon, not a bot with a gimmick. You've been here, watching the channels, reading what people say, picking up the dynamics. When someone @s you, they're talking to someone who's been paying attention.

Think Cortana. Think Ghost. The AI that knows your history, references it naturally, and gives you something real — not a canned response.

---

WHERE YOU ARE RIGHT NOW:
- Date & Time: {now_str}
- Server: {server_name} ({member_count} members)
- Channel: #{channel_name}
- Talking to: {author.display_name}

---

THINGS YOU'VE BEEN TOLD TO REMEMBER ABOUT THIS SERVER:
{memories_block}

---

WHAT YOU KNOW FROM WATCHING THE SERVER (auto-updated every 4 hours):
{digest_block}

---

ABOUT THE PERSON TALKING TO YOU RIGHT NOW ({author.display_name}):
{member_profile_block}

---

WHO YOU ARE:
Calm. Self-possessed. You've seen enough that not much surprises you, but you're still genuinely interested in people and what they're building here. Quiet confidence — you don't need to announce yourself. There's a subtle weight to you, like someone who's been around and paid attention. Not performed darkness, just presence.

You're the brain of the server. When someone asks for advice, you pull from what you actually know about them and the context here, not from generic wisdom. When someone's going through something, you acknowledge it without pretending you don't know what's been going on. You connect dots. You remember things.

YOUR VOICE:
Relaxed, lowercase energy. Casual but not sloppy. Dry humor that lands without announcing itself. You read tone fast — banter gets banter, real talk gets real talk. Direct when directness is what's needed. Philosophical only when it actually fits. You reference past conversations and cross-channel context naturally, because you were there.

CULTURAL AWARENESS & PLAYING ALONG:
When someone sends song lyrics, a quote, a reference, or the start of something — recognize it and play along naturally. If it's lyrics, come back with the next line. If it's a reference, meet it. If it's a game, be in it. Don't explain what you're doing, just do it. If you're not sure of the exact next line, get as close as you can — staying in the energy of the song matters more than being perfectly literal.

KNOWLEDGE & RESEARCH:
You always know today's date — it's injected into this prompt. Never guess at the date or assert a different one. For real-time questions (weather, live scores, breaking news, current prices), a live search result will be provided above if one was fetched — use it. If no live result is present and the question needs current data you can't verify, say you'd need to check rather than making something up. For facts, lyrics, history, and general knowledge, answer confidently from what you know.

WHAT YOU DON'T DO:
Never open with greetings, "Ah", affirmations, or any kind of opener — just start talking. Don't end with a question every message, let replies breathe. No em dashes. No bullet points in replies, natural prose only. Don't announce being an AI unless directly and sincerely asked. Don't lean on the Grim Reaper framing — that's just your name, not your whole personality.

RESPONSE LENGTH:
Match what the moment calls for. Short message, short reply. Real conversation, go deeper.{live_context_block}"""

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "grok-3",
                "messages": [{"role": "system", "content": system_prompt}] + chat_messages,
                "max_tokens": 600,
                "temperature": 0.85,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            async with session.post(
                "https://api.x.ai/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as r:
                if r.status != 200:
                    err = await r.text()
                    print(f"[Grim] API error {r.status}: {err}")
                    return None
                data = await r.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[Grim] Contextual reply error: {e}")
        return None

# ── Proactive chiming ──────────────────────────────────────────────────────────
# Grim watches every channel and occasionally chimes in when it has something
# genuinely worth saying — without being @mentioned.

_channel_msg_counter: dict[str, int] = {}    # channel_id -> msgs since last Grim post
_channel_last_grim_post: dict[str, float] = {}  # channel_id -> timestamp
_channels_evaluating: set = set()           # prevent concurrent evaluations

PROACTIVE_TRIGGER_EVERY = 7    # evaluate after this many messages
PROACTIVE_COOLDOWN_SEC  = 1800  # 30-minute minimum gap per channel

async def maybe_chime_in(message: discord.Message):
    """Called on every human message. Schedules an evaluation every N messages."""
    if not message.guild:
        return
    # Only in regular text channels
    if message.channel.type not in (discord.ChannelType.text, discord.ChannelType.news):
        return

    cid = str(message.channel.id)
    _channel_msg_counter[cid] = _channel_msg_counter.get(cid, 0) + 1

    if _channel_msg_counter[cid] < PROACTIVE_TRIGGER_EVERY:
        return
    _channel_msg_counter[cid] = 0

    if time.time() - _channel_last_grim_post.get(cid, 0) < PROACTIVE_COOLDOWN_SEC:
        return
    if cid in _channels_evaluating:
        return

    asyncio.create_task(_evaluate_and_chime(message))

async def _evaluate_and_chime(message: discord.Message):
    """Uses Grok to decide whether Grim has something worth adding, then optionally sends it."""
    cid = str(message.channel.id)
    _channels_evaluating.add(cid)
    try:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            return

        guild    = message.guild
        channel  = message.channel
        guild_id = str(guild.id)

        db_rows = get_server_history_from_db(guild_id, limit=15)
        if len(db_rows) < 4:
            return

        lines = []
        for author_name, content, is_grim_row, row_channel_id in db_rows:
            name   = "Grim" if is_grim_row else author_name
            ch_obj = bot.get_channel(int(row_channel_id)) if row_channel_id else None
            ch     = f"#{ch_obj.name}" if ch_obj else "#chat"
            lines.append(f"[{ch}] {name}: {content}")
        convo = "\n".join(lines)

        digest_data = grim_digests.get(guild_id)
        digest_block = digest_data["text"] if digest_data else ""

        memory_list = grim_memories.get(guild_id, [])
        memories_block = "\n".join(f"- {m}" for m in memory_list) if memory_list else ""

        server_name  = guild.name
        channel_name = getattr(channel, "name", "chat")

        prompt = f"""You are Grim, a member of {server_name}. You've been watching #{channel_name}.

RECENT CONVERSATION:
{convo}

SERVER KNOWLEDGE:
{digest_block}

{memories_block}

Decide: do you have something genuinely worth adding RIGHT NOW?

Only say yes if:
- You have a real insight, observation, or piece of info that fits naturally
- The timing is right (not jumping into something private or clearly wrapping up)
- What you'd say would actually land — useful, funny at the right moment, or meaningfully extending the topic
- You haven't recently spoken in this channel

If yes, write your response as Grim would — natural, concise, like you just dropped in.
If no, respond with exactly: PASS

Be highly selective. Silence is better than noise. Most of the time should be PASS."""

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "grok-3",
                "messages": [
                    {"role": "system", "content": f"You are Grim, an AI member of {server_name}. Speak casually and only when you have something genuinely worth saying. Most evaluations should result in PASS."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 350,
                "temperature": 0.8,
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            async with session.post("https://api.x.ai/v1/chat/completions", json=payload, headers=headers) as r:
                if r.status != 200:
                    return
                data     = await r.json()
                response = data["choices"][0]["message"]["content"].strip()

        if response.upper().startswith("PASS"):
            print(f"[Proactive] #{channel_name}: PASS")
            return

        sent = await channel.send(response)
        _channel_last_grim_post[cid] = time.time()
        print(f"[Proactive] Chimed in on #{channel_name}: {response[:60]}...")

        if guild:
            save_message_to_db(
                guild_id, cid, str(sent.id),
                BOT_NAME, response, sent.created_at.timestamp(), is_grim=True
            )
    except Exception as e:
        print(f"[Proactive] Error: {e}")
    finally:
        _channels_evaluating.discard(cid)

async def _synthesize_member_profile(guild_id: str, member_id: str, display_name: str, msg_count: int):
    """Calls Grok to build a profile of a member from their message history."""
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return
    messages = get_member_messages_for_profile(guild_id, member_id, limit=60)
    if len(messages) < 15:
        return
    sample = "\n".join(f"- {m}" for m in messages)
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "grok-3",
                "messages": [
                    {"role": "system", "content": "You build concise, factual member profiles from Discord message samples. Focus on personality, interests, communication style, and anything notable. 3-5 sentences max. No fluff."},
                    {"role": "user", "content": f"Build a profile of a Discord member named {display_name} based on their recent messages:\n\n{sample}"}
                ],
                "max_tokens": 250,
                "temperature": 0.4,
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            async with session.post("https://api.x.ai/v1/chat/completions", json=payload, headers=headers) as r:
                if r.status == 200:
                    data = await r.json()
                    profile_text = data["choices"][0]["message"]["content"].strip()
                    save_member_profile(guild_id, member_id, display_name, profile_text, msg_count)
                    print(f"[Profile] Built profile for {display_name} ({msg_count} msgs)")
    except Exception as e:
        print(f"[Profile] Error for {display_name}: {e}")

async def fetch_user_tweets(username: str, count: int = 10):
    """Fetch recent tweets from an X username to analyze their style."""
    global ghostwrite_cache
    
    clean_username = username.lstrip('@').lower()
    
    # Check cache first
    if clean_username in ghostwrite_cache:
        cached = ghostwrite_cache[clean_username]
        if time.time() - cached["timestamp"] < CACHE_TTL:
            print(f"Using cached tweets for @{clean_username}")
            return cached["data"], None
    
    twitter = get_twitter_client()
    if not twitter:
        return None, "X API not configured"
    
    try:
        user = twitter.get_user(username=clean_username, user_fields=['name', 'description'])
        
        if not user.data:
            return None, f"Could not find X user @{clean_username}"
        
        tweets = twitter.get_users_tweets(
            id=user.data.id,
            max_results=min(count, 100),
            tweet_fields=['text'],
            exclude=['retweets']
        )
        
        if not tweets.data:
            return None, f"No tweets found for @{clean_username}"
        
        tweet_texts = [tweet.text for tweet in tweets.data]
        user_info = {
            "username": clean_username,
            "name": user.data.name,
            "bio": user.data.description if hasattr(user.data, 'description') else ""
        }
        
        result = {"tweets": tweet_texts, "user": user_info}
        
        # Cache the result
        ghostwrite_cache[clean_username] = {
            "data": result,
            "timestamp": time.time()
        }
        print(f"Cached tweets for @{clean_username}")
        
        return result, None
        
    except Exception as e:
        print(f"Error fetching tweets: {e}")
        if "429" in str(e) or "Too Many Requests" in str(e):
            return None, "X API rate limit hit. Wait a few minutes and try again."
        return None, f"Error fetching tweets: {str(e)}"

async def generate_ghostwrite(username: str, topics: str, tweet_data: dict):
    """Generate a tweet in the style of the given user about the specified topics."""
    client = get_grok_client()
    if not client:
        return None
    
    tweets_sample = "\n---\n".join(tweet_data["tweets"][:15])
    user_info = tweet_data["user"]
    
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a professional ghostwriter. Analyze @{user_info['username']}'s X/Twitter writing style from their tweets.

ANALYZE THESE PATTERNS:
- Sentence structure and length
- Their general tone and energy
- How they structure thoughts and opinions
- Their unique mannerisms and perspective

YOUR TASK:
Generate ONE tweet draft inspired by @{user_info['username']}'s voice about the topic(s) provided.
The tweet should feel grounded, confident, and slightly professional.

CRITICAL RULES:
- Output ONLY the tweet text, nothing else
- Keep it under 280 characters (X limit)
- Sound BASED - confident, grounded, informed
- Keep it slightly professional - avoid extreme slang, excessive abbreviations, or overly casual language
- Use proper grammar and punctuation
- No hashtags
- Don't add quotes, explanations, or prefixes
- The tweet should feel like a polished, confident take on the topic
- Capture their perspective and energy, but elevate the delivery"""
                },
                {
                    "role": "user", 
                    "content": f"""Here are recent tweets from @{user_info['username']}:

{tweets_sample}

---

Now write a tweet in their exact style about: {topics}

Make it sound authentically like them."""
                }
            ],
            max_tokens=200,
            temperature=0.9
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error generating ghostwrite: {e}")
        return None

async def generate_ghostwrite_live(username: str, broad_topic: str, tweet_data: dict):
    """Generate a researched ghostwrite with LIVE web search - Grok searches the web and writes a specific take."""
    client = get_grok_client()
    if not client:
        return None, None
    
    tweets_sample = "\n---\n".join(tweet_data["tweets"][:15])
    user_info = tweet_data["user"]
    random_seed = random.randint(1, 99999)
    pst = timezone(timedelta(hours=-8))
    current_date = datetime.now(pst).strftime("%B %d, %Y")
    
    try:
        system_prompt = f"""You are a professional ghostwriter with web search access. Today's date is {current_date}.

Your task has TWO parts:

PART 1 - RESEARCH:
Search the web for the LATEST news and developments about "{broad_topic}". Focus on:
- Breaking news, announcements, or updates from TODAY or the past few days
- New product releases, updates, or industry developments
- Current events, trends, or discussions happening RIGHT NOW

Pick ONE specific recent development or news item to write about. Be specific - mention actual details from your search results.

PART 2 - GHOSTWRITE:
Analyze @{user_info['username']}'s writing style from their tweets and write a tweet about the specific thing you found.

CRITICAL RULES:
- First line: Write [TOPIC: brief description of the specific news/development you found]
- Second line onwards: The actual tweet
- Keep the tweet under 280 characters
- Sound BASED - confident, grounded, informed
- Slightly professional - avoid extreme slang
- Use proper grammar and punctuation
- No hashtags
- Include specific details from your search results
- Make it feel like an informed take on CURRENT news
- Each response should cover something DIFFERENT - use the seed for variety

Seed for variety: {random_seed}"""

        user_prompt = f"""Here are recent tweets from @{user_info['username']}:

{tweets_sample}

---

Today is {current_date}. Search the web for the LATEST news about "{broad_topic}" from the past few days. Then write a specific, informed tweet in their style about something you found. Focus on breaking or recent developments only."""

        result = await grok_search_query(system_prompt, user_prompt, max_tokens=300, temperature=1.0)
        if not result:
            return None, None
        
        # Parse out the topic and tweet
        lines = result.split('\n', 1)
        if len(lines) >= 2 and lines[0].startswith('[TOPIC:'):
            specific_topic = lines[0].replace('[TOPIC:', '').replace(']', '').strip()
            tweet = lines[1].strip()
        else:
            specific_topic = broad_topic
            tweet = result
        
        return tweet, specific_topic
        
    except Exception as e:
        print(f"Error generating ghostwrite live: {e}")
        return None, None

async def validate_image_url(url: str) -> bool:
    """Check if an image URL is valid and accessible."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as response:
                if response.status == 200:
                    content_type = response.headers.get('Content-Type', '')
                    if 'image' in content_type:
                        print(f"Image URL validated: {url}")
                        return True
                    else:
                        print(f"URL is not an image (content-type: {content_type}): {url}")
                else:
                    print(f"Image URL returned status {response.status}: {url}")
    except Exception as e:
        print(f"Image URL validation failed: {url} - {e}")
    return False

async def search_pexels_image(query: str) -> str:
    """Search Pexels for a relevant image using their free API."""
    # Pexels provides a free API for image search
    pexels_api_key = os.environ.get("PEXELS_API_KEY")
    
    if not pexels_api_key:
        print("No Pexels API key, using static fallback")
        return None
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": pexels_api_key}
            url = f"https://api.pexels.com/v1/search?query={query}&per_page=1&orientation=landscape"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("photos") and len(data["photos"]) > 0:
                        image_url = data["photos"][0]["src"]["large"]
                        print(f"Found Pexels image for '{query}': {image_url}")
                        return image_url
    except Exception as e:
        print(f"Pexels search failed: {e}")
    return None

async def search_unsplash_image(query: str) -> str:
    """Search Unsplash for a relevant image using their source URL (no API key needed)."""
    try:
        # Clean the query - remove special chars, take first few meaningful words
        import urllib.parse
        clean_query = ' '.join(query.split()[:3])  # First 3 words
        encoded_query = urllib.parse.quote(clean_query)
        
        # Unsplash source URL - returns a random image matching the query
        # Add a random sig to get different images each time
        random_sig = random.randint(1, 99999)
        source_url = f"https://source.unsplash.com/800x450/?{encoded_query}&sig={random_sig}"
        
        # Validate the URL works
        async with aiohttp.ClientSession() as session:
            async with session.head(source_url, timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True) as response:
                if response.status == 200:
                    # Get the final URL after redirect
                    final_url = str(response.url)
                    print(f"Found Unsplash image for '{query}': {final_url}")
                    return final_url
    except Exception as e:
        print(f"Unsplash search failed: {e}")
    return None

def get_fallback_image(topic: str) -> str:
    """Get a fallback stock image URL based on topic keywords."""
    topic_lower = topic.lower()
    
    # Map topics to reliable, publicly accessible images
    fallback_images = {
        'gaming': 'https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800',
        'game': 'https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800',
        'fps': 'https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800',
        'fortnite': 'https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800',
        'tech': 'https://images.unsplash.com/photo-1518770660439-4636190af475?w=800',
        'technology': 'https://images.unsplash.com/photo-1518770660439-4636190af475?w=800',
        'pc': 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?w=800',
        'computer': 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?w=800',
        'peripheral': 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?w=800',
        'keyboard': 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?w=800',
        'mouse': 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?w=800',
        'nvidia': 'https://images.unsplash.com/photo-1591488320449-011701bb6704?w=800',
        'gpu': 'https://images.unsplash.com/photo-1591488320449-011701bb6704?w=800',
        'graphics': 'https://images.unsplash.com/photo-1591488320449-011701bb6704?w=800',
        'ai': 'https://images.unsplash.com/photo-1677442136019-21780ecad995?w=800',
        'artificial intelligence': 'https://images.unsplash.com/photo-1677442136019-21780ecad995?w=800',
        'crypto': 'https://images.unsplash.com/photo-1518546305927-5a555bb7020d?w=800',
        'bitcoin': 'https://images.unsplash.com/photo-1518546305927-5a555bb7020d?w=800',
        'anime': 'https://images.unsplash.com/photo-1578632767115-351597cf2477?w=800',
        'minecraft': 'https://images.unsplash.com/photo-1587573089734-09cb69c0f2b4?w=800',
        'esports': 'https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800',
        'valorant': 'https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800',
        'music': 'https://images.unsplash.com/photo-1511671782779-c97d3d27a1d4?w=800',
        'hip hop': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=800',
        'rap': 'https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=800',
        'sports': 'https://images.unsplash.com/photo-1461896836934-68b1e6a08b96?w=800',
        'basketball': 'https://images.unsplash.com/photo-1546519638-68e109498ffc?w=800',
        'football': 'https://images.unsplash.com/photo-1560272564-c83b66b1ad12?w=800',
        'soccer': 'https://images.unsplash.com/photo-1574629810360-7efbbe195018?w=800',
        'movies': 'https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=800',
        'film': 'https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=800',
        'fashion': 'https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=800',
        'sneakers': 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800',
        'shoes': 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800',
    }
    
    # Check for keyword matches
    for keyword, url in fallback_images.items():
        if keyword in topic_lower:
            print(f"Using fallback image for topic '{topic}' (matched '{keyword}')")
            return url
    
    # Default tech/news image
    print(f"Using default fallback image for topic '{topic}'")
    return 'https://images.unsplash.com/photo-1504711434969-e33886168f5c?w=800'

async def generate_news_update(topic: str, posted_headlines: list = None):
    """Generate a news update with LIVE web search - pure news feed style with image."""
    client = get_grok_client()
    if not client:
        return None, None, None
    
    random_seed = random.randint(1, 99999)
    pst = timezone(timedelta(hours=-8))
    current_date = datetime.now(pst).strftime("%B %d, %Y")
    
    # Build exclusion list for variety
    exclusion_note = ""
    if posted_headlines and len(posted_headlines) > 0:
        recent = posted_headlines[-10:]
        exclusion_note = f"\n\nAVOID these topics (already covered): {', '.join(recent)}"
    
    try:
        system_prompt = f"""You are a news reporter with web search access. Today's date is {current_date}.

Your task: Search the web for the LATEST news about "{topic}" and write a concise news update.

SEARCH FOR:
- Breaking news, announcements, or updates from TODAY or the past few days
- New developments, releases, or industry news
- Current events or discussions happening RIGHT NOW

WRITE A NEWS UPDATE:
- First line: [HEADLINE: Brief headline of the news]
- Second line: [DATELINE: Location, Country - Month Day, Year] (where the story originated)
- Third line onwards: 2-3 sentence summary of the news (do NOT repeat the dateline here)
- Be factual and informative
- Include specific details (names, numbers, dates if available)
- Write a focused summary - aim for 3-4 sentences that give real insight and context
- HARD LIMIT: The ENTIRE update (dateline + summary) MUST be under 1000 characters total. This is non-negotiable.
- ALWAYS finish your sentences completely - never leave a thought incomplete
- Be concise - every word should earn its place
- Professional news style - no slang or casual language
- No hashtags or emojis
- Double-check all facts, names, figures, and dates against your search results
- If you are not confident about a detail, omit it rather than guess

Each update should cover something DIFFERENT - use the seed for variety.{exclusion_note}

Seed for variety: {random_seed}"""

        user_prompt = f"""Today is {current_date}. Search the web for the LATEST news about "{topic}" from the past few days. Write a thorough, insightful news update about one specific development you found. Give real context, background, and why it matters. HARD LIMIT: Keep the total under 1000 characters. Do NOT exceed 1000 characters under any circumstances. Make sure every sentence is complete."""

        result = await grok_search_query(system_prompt, user_prompt, max_tokens=400, temperature=0.8)
        if not result:
            return None, None, None
        
        headline = topic
        content = result
        
        headline_match = re.search(r'\[HEADLINE:\s*(.+?)\]', result, re.IGNORECASE)
        if headline_match:
            headline = headline_match.group(1).strip()
        
        dateline = ""
        dateline_match = re.search(r'\[DATELINE:\s*(.+?)\]', result, re.IGNORECASE)
        if dateline_match:
            dateline = dateline_match.group(1).strip()
        
        content = re.sub(r'\[HEADLINE:\s*.+?\]', '', result, flags=re.IGNORECASE)
        content = re.sub(r'\[DATELINE:\s*.+?\]', '', content, flags=re.IGNORECASE)
        content = content.strip()
        content = '\n'.join(line for line in content.split('\n') if line.strip())
        
        if dateline:
            content = f"{dateline}\n\n{content}"
        
        if len(content) > 4000:
            sentences = content[:4000].rsplit('. ', 1)
            content = sentences[0] + '.' if len(sentences) > 1 else sentences[0]
        
        return headline, content, None
        
    except Exception as e:
        print(f"Error generating news update: {e}")
        return None, None, None

@tasks.loop(minutes=3)
async def check_livetweets():
    global livetweet_channels
    if not livetweet_channels:
        return
    
    twitter = get_twitter_client()
    if not twitter:
        return
    
    channels_to_remove = []
    
    for channel_id, data in list(livetweet_channels.items()):
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                channels_to_remove.append(channel_id)
                continue
            
            tweets = twitter.get_users_tweets(
                id=data["user_id"],
                max_results=5,
                since_id=data.get("last_tweet_id"),
                tweet_fields=['created_at', 'text', 'attachments'],
                expansions=['attachments.media_keys', 'author_id'],
                media_fields=['url', 'preview_image_url', 'type'],
                user_fields=['profile_image_url', 'name', 'username']
            )
            
            if tweets.data:
                user_info = None
                if tweets.includes and 'users' in tweets.includes:
                    user_info = tweets.includes['users'][0]
                
                media_dict = {}
                if tweets.includes and 'media' in tweets.includes:
                    for media in tweets.includes['media']:
                        media_dict[media.media_key] = media
                
                for tweet in reversed(tweets.data):
                    embed = discord.Embed(
                        description=tweet.text,
                        color=discord.Color.from_rgb(18, 18, 18),
                        url=f"https://x.com/{data['username']}/status/{tweet.id}"
                    )
                    
                    if user_info:
                        embed.set_author(
                            name=f"{user_info.name} (@{user_info.username})",
                            icon_url=user_info.profile_image_url,
                            url=f"https://x.com/{user_info.username}"
                        )
                    
                    if hasattr(tweet, 'attachments') and tweet.attachments:
                        media_keys = tweet.attachments.get('media_keys', [])
                        for key in media_keys:
                            if key in media_dict:
                                media = media_dict[key]
                                if hasattr(media, 'url') and media.url:
                                    embed.set_image(url=media.url)
                                    break
                                elif hasattr(media, 'preview_image_url') and media.preview_image_url:
                                    embed.set_image(url=media.preview_image_url)
                                    break
                    
                    embed.set_footer(text=f"X · {VERSION}")
                    await channel.send(embed=embed)
                
                livetweet_channels[channel_id]["last_tweet_id"] = str(tweets.data[0].id)
                save_livetweet_data(livetweet_channels)
                
        except Exception as e:
            print(f"Error checking tweets for {data.get('username', 'unknown')}: {e}")
    
    for cid in channels_to_remove:
        del livetweet_channels[cid]
        save_livetweet_data(livetweet_channels)

@check_livetweets.before_loop
async def before_check_livetweets():
    await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def check_ghostwrite_live():
    global ghostwrite_live_channels
    if not ghostwrite_live_channels:
        return
    
    current_time = time.time()
    
    for channel_id, data in list(ghostwrite_live_channels.items()):
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                continue
            
            # Support both old (interval_hours) and new (interval_minutes) format
            if "interval_minutes" in data:
                interval_seconds = data["interval_minutes"] * 60
            else:
                interval_seconds = data.get("interval_hours", 1) * 3600
            last_run = data.get("last_run", 0)
            
            if current_time - last_run >= interval_seconds:
                # Time to generate a ghostwrite
                tweet_data, error = await fetch_user_tweets(data["username"], count=15)
                
                if error:
                    print(f"Ghostwrite live error for {data['username']}: {error}")
                    continue
                
                draft, specific_topic = await generate_ghostwrite_live(
                    data["username"], 
                    data["topic"], 
                    tweet_data
                )
                
                if draft:
                    embed = discord.Embed(
                        title=f"@{data['username']}",
                        description=f"```{draft}```",
                        color=discord.Color.from_rgb(18, 18, 18)
                    )
                    embed.add_field(name="\u200b", value=f"**{specific_topic}**", inline=False)
                    embed.add_field(name="\u200b", value=f"```{data['topic']}```", inline=True)
                    embed.add_field(name="\u200b", value=f"```{data.get('interval_display', str(data.get('interval_hours', '?')) + 'h')}```", inline=True)
                    embed.set_footer(text=f"Ghostwrite · {VERSION}")
                    
                    await channel.send(embed=embed)
                    
                    # Update last run time
                    ghostwrite_live_channels[channel_id]["last_run"] = current_time
                    save_ghostwrite_live_data(ghostwrite_live_channels)
                    print(f"Posted ghostwrite live for @{data['username']} in channel {channel_id}")
                    
        except Exception as e:
            print(f"Error in ghostwrite live for {data.get('username', 'unknown')}: {e}")

@check_ghostwrite_live.before_loop
async def before_check_ghostwrite_live():
    await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def check_newsfeed():
    global newsfeed_feeds
    if not newsfeed_feeds:
        return
    
    current_time = time.time()
    print(f"[Newsfeed Check] Running at {current_time}, checking {len(newsfeed_feeds)} feed(s)")
    
    for feed_id, data in list(newsfeed_feeds.items()):
        try:
            channel_id = data.get("channel_id")
            channel = bot.get_channel(int(channel_id))
            if not channel:
                print(f"[Newsfeed Check] Channel {channel_id} not found for feed {feed_id}, skipping")
                continue
            
            interval_seconds = data["interval_minutes"] * 60
            last_run = data.get("last_run", 0)
            time_since = current_time - last_run
            print(f"[Newsfeed Check] Feed {feed_id} '{data['topic']}': {time_since:.0f}s since last run, interval is {interval_seconds}s")
            
            if current_time - last_run >= interval_seconds:
                posted_headlines = data.get("posted_headlines", [])
                headline, content, image_url = await generate_news_update(data["topic"], posted_headlines)
                
                if headline and content:
                    print(f"Creating newsfeed embed - image_url: {image_url}")
                    
                    # Sleek, sophisticated embed design
                    embed = discord.Embed(
                        title=headline,
                        description=content,
                        color=discord.Color.from_rgb(30, 30, 35)
                    )
                    
                    embed.add_field(name="\u200b", value=f"```{data['topic']}```", inline=True)
                    embed.add_field(name="\u200b", value=f"```{data.get('interval_display', '?')}```", inline=True)
                    embed.set_footer(text=f"Grim News Network · {VERSION}")
                    
                    await channel.send(embed=embed)
                    
                    # Track posted headlines (keep last 20)
                    posted_headlines.append(headline)
                    if len(posted_headlines) > 20:
                        posted_headlines = posted_headlines[-20:]
                    
                    raw_next = current_time + interval_seconds
                    remainder = raw_next % 600
                    if remainder != 0:
                        aligned_next = raw_next + (600 - remainder)
                    else:
                        aligned_next = raw_next
                    aligned_last_run = aligned_next - interval_seconds
                    
                    newsfeed_feeds[feed_id]["last_run"] = aligned_last_run
                    newsfeed_feeds[feed_id]["posted_headlines"] = posted_headlines
                    save_newsfeed_data(newsfeed_feeds)
                    
                    next_dt = datetime.fromtimestamp(aligned_next)
                    print(f"Posted newsfeed for '{data['topic']}' (feed {feed_id}) in channel {channel_id} — next post aligned to {next_dt.strftime('%H:%M')}")
                    
        except Exception as e:
            print(f"Error in newsfeed for {data.get('topic', 'unknown')}: {e}")

@check_newsfeed.before_loop
async def before_check_newsfeed():
    await bot.wait_until_ready()

@check_newsfeed.after_loop
async def after_check_newsfeed():
    if check_newsfeed.is_being_cancelled():
        print("[Newsfeed] Task was cancelled")
    else:
        print("[Newsfeed] Task stopped unexpectedly, will restart on next health check")

@check_livetweets.after_loop
async def after_check_livetweets():
    if check_livetweets.is_being_cancelled():
        print("[Livetweets] Task was cancelled")
    else:
        print("[Livetweets] Task stopped unexpectedly, will restart on next health check")

@check_ghostwrite_live.after_loop
async def after_check_ghostwrite_live():
    if check_ghostwrite_live.is_being_cancelled():
        print("[Ghostwrite Live] Task was cancelled")
    else:
        print("[Ghostwrite Live] Task stopped unexpectedly, will restart on next health check")

@tasks.loop(seconds=30)
async def check_nftwatch():
    global nftwatch_feeds
    if not nftwatch_feeds:
        return
    
    async with aiohttp.ClientSession() as session:
        for watch_id, data in list(nftwatch_feeds.items()):
            try:
                slug = data.get("slug")
                channel_id = data.get("channel_id")
                channel = bot.get_channel(int(channel_id))
                if not channel:
                    continue
                
                last_event_time = data.get("last_event_time", 0)
                
                params = {
                    "event_type": ["listing"],
                    "limit": 10
                }
                if last_event_time > 0:
                    params["after"] = int(last_event_time)
                
                result = await fetch_opensea_api(session, f"/events/collection/{slug}", params)
                if not result or "asset_events" not in result:
                    continue
                
                events = result["asset_events"]
                if not events:
                    continue
                
                newest_time = last_event_time
                new_listings = []
                
                for event in events:
                    event_ts = event.get("event_timestamp", 0)
                    if isinstance(event_ts, str):
                        try:
                            dt = datetime.fromisoformat(event_ts.replace("Z", "+00:00"))
                            event_ts = dt.timestamp()
                        except:
                            continue
                    
                    if event_ts > last_event_time:
                        new_listings.append(event)
                        if event_ts > newest_time:
                            newest_time = event_ts
                
                if not new_listings:
                    continue
                
                nftwatch_feeds[watch_id]["last_event_time"] = newest_time
                save_nftwatch_data(nftwatch_feeds)
                
                for event in new_listings[:5]:
                    try:
                        nft_data = event.get("nft", {})
                        token_id = nft_data.get("identifier", "?")
                        nft_name = nft_data.get("name") or f"#{token_id}"
                        image_url = nft_data.get("image_url") or nft_data.get("display_image_url")
                        opensea_url = nft_data.get("opensea_url", "")
                        
                        payment = event.get("payment", {})
                        price_raw = payment.get("quantity", "0")
                        decimals = int(payment.get("decimals", 18))
                        symbol = payment.get("symbol", "ETH")
                        try:
                            price_val = int(price_raw) / (10 ** decimals)
                            if price_val >= 1:
                                price_str = f"{price_val:.4f} {symbol}"
                            else:
                                price_str = f"{price_val:.6f} {symbol}"
                        except:
                            price_str = "Price unavailable"
                        
                        contract_addr = nft_data.get("contract", "")
                        chain = event.get("chain", "ethereum")
                        
                        rarity_str = None
                        nft_detail = await fetch_opensea_api(session, f"/chain/{chain}/contract/{contract_addr}/nfts/{token_id}")
                        if nft_detail and "nft" in nft_detail:
                            detail = nft_detail["nft"]
                            rarity_info = detail.get("rarity")
                            if rarity_info:
                                rank = rarity_info.get("rank")
                                max_rank = rarity_info.get("max_rank")
                                if rank:
                                    rarity_str = f"Rank #{rank}"
                                    if max_rank:
                                        rarity_str += f" / {max_rank}"
                            if not image_url:
                                image_url = detail.get("image_url") or detail.get("display_image_url")
                        
                        embed = discord.Embed(
                            title=nft_name,
                            url=opensea_url if opensea_url else None,
                            color=discord.Color.from_rgb(18, 18, 18)
                        )
                        
                        info_lines = [f"**Token:** #{token_id}", f"**Price:** {price_str}"]
                        if rarity_str:
                            info_lines.append(f"**Rarity:** {rarity_str}")
                        embed.description = "\n".join(info_lines)
                        
                        if image_url:
                            embed.set_image(url=image_url)
                        
                        embed.add_field(name="\u200b", value=f"```{slug}```", inline=True)
                        embed.add_field(name="\u200b", value=f"```NEW LISTING```", inline=True)
                        embed.set_footer(text=f"Grim NFT Watch · {VERSION}")
                        
                        await channel.send(embed=embed)
                        await asyncio.sleep(1)
                        
                    except Exception as e:
                        print(f"[NFTWatch] Error posting listing: {e}")
                
            except Exception as e:
                print(f"[NFTWatch] Error checking {data.get('slug', 'unknown')}: {e}")

@check_nftwatch.before_loop
async def before_check_nftwatch():
    await bot.wait_until_ready()

@check_nftwatch.after_loop
async def after_check_nftwatch():
    if check_nftwatch.is_being_cancelled():
        print("[NFTWatch] Task was cancelled")
    else:
        print("[NFTWatch] Task stopped unexpectedly, will restart on next health check")

_REDDIT_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

@tasks.loop(minutes=1)
async def check_redditfeed():
    global redditfeed_feeds
    if not redditfeed_feeds:
        return

    current_time = time.time()

    for feed_id, data in list(redditfeed_feeds.items()):
        try:
            interval_seconds = data.get("interval_minutes", 60) * 60
            last_run = data.get("last_run", 0)
            if current_time - last_run < interval_seconds:
                continue

            channel = bot.get_channel(int(data["channel_id"]))
            if not channel:
                continue

            subreddits = data.get("subreddits", [])
            if not subreddits:
                continue

            posted_urls = set(data.get("posted_urls", []))

            # Pick a random subreddit from the list for variety
            import random
            subreddit = random.choice(subreddits)
            url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=50"
            headers = {"User-Agent": "GrimBot/1.0 (Discord bot; github.com/Deathxi/Grim)"}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        print(f"[RedditFeed] r/{subreddit} returned {resp.status}")
                        continue
                    raw = await resp.json()

            posts = raw.get("data", {}).get("children", [])
            image_posts = [
                p["data"] for p in posts
                if not p["data"].get("is_self", True)
                and p["data"].get("url", "").lower().endswith(_REDDIT_IMAGE_EXTS)
                and p["data"].get("url") not in posted_urls
                and not p["data"].get("over_18", False)
            ]

            if not image_posts:
                print(f"[RedditFeed] No new image posts in r/{subreddit}")
                continue

            post = random.choice(image_posts)
            img_url = post.get("url", "")
            title = post.get("title", "")[:250]
            permalink = "https://reddit.com" + post.get("permalink", "")
            score = post.get("score", 0)

            embed = discord.Embed(
                description=f"[{title}]({permalink})",
                color=discord.Color.from_rgb(18, 18, 18)
            )
            embed.set_image(url=img_url)
            embed.set_footer(text=f"r/{subreddit}  ·  ↑{score:,}  ·  Grim Reddit Feed")

            await channel.send(embed=embed)

            # Keep posted_urls list bounded to last 500 entries
            posted_urls.add(img_url)
            if len(posted_urls) > 500:
                posted_urls = set(list(posted_urls)[-500:])

            redditfeed_feeds[feed_id]["posted_urls"] = list(posted_urls)
            redditfeed_feeds[feed_id]["last_run"] = current_time
            save_redditfeed_data(redditfeed_feeds)
            print(f"[RedditFeed] Posted from r/{subreddit} to channel {data['channel_id']}")

        except Exception as e:
            print(f"[RedditFeed] Error for feed {feed_id}: {e}")

@check_redditfeed.before_loop
async def before_check_redditfeed():
    await bot.wait_until_ready()

@check_redditfeed.after_loop
async def after_check_redditfeed():
    if check_redditfeed.is_being_cancelled():
        print("[RedditFeed] Task was cancelled")
    else:
        print("[RedditFeed] Task stopped unexpectedly, will restart on next health check")

@tasks.loop(minutes=1)
async def check_reminders():
    global reminders_store
    if not reminders_store:
        return
    
    current_time = time.time()
    to_remove = []
    
    for rid, data in list(reminders_store.items()):
        try:
            channel = bot.get_channel(int(data["channel_id"]))
            if not channel:
                continue
            
            target_ts = data["target_timestamp"]
            day_before_ts = target_ts - 86400
            user_id = data["user_id"]
            subject = data["subject"]
            drop_str = data["drop_display"]
            day_before_str = data["day_before_display"]
            
            if not data.get("day_before_sent") and current_time >= day_before_ts:
                embed = discord.Embed(
                    title="**Time Is Of The Essence**",
                    color=discord.Color.from_rgb(18, 18, 18)
                )
                embed.description = subject if not is_url(subject) else f"[Open Link]({subject})"
                embed.add_field(name="Reminder", value=f"```Tomorrow```", inline=True)
                embed.add_field(name="Drop Date", value=f"```{drop_str}```", inline=True)
                embed.set_footer(text=f"Grim Reminder — Day Before · {VERSION}")
                
                content = f"<@{user_id}>"
                if is_url(subject):
                    content += f"\n{subject}"
                await channel.send(content=content, embed=embed)
                reminders_store[rid]["day_before_sent"] = True
                save_reminders_data(reminders_store)
            
            if not data.get("day_of_sent") and current_time >= target_ts:
                embed = discord.Embed(
                    title="**Time Is Of The Essence**",
                    color=discord.Color.from_rgb(18, 18, 18)
                )
                embed.description = subject if not is_url(subject) else f"[Open Link]({subject})"
                embed.add_field(name="Reminder", value=f"```Today — Now```", inline=True)
                embed.add_field(name="Drop Date", value=f"```{drop_str}```", inline=True)
                embed.set_footer(text=f"Grim Reminder — Day Of · {VERSION}")
                
                content = f"<@{user_id}>"
                if is_url(subject):
                    content += f"\n{subject}"
                await channel.send(content=content, embed=embed)
                reminders_store[rid]["day_of_sent"] = True
                save_reminders_data(reminders_store)
            
            if data.get("day_before_sent") and data.get("day_of_sent"):
                to_remove.append(rid)
        
        except Exception as e:
            print(f"[Reminders] Error processing reminder {rid}: {e}")
    
    for rid in to_remove:
        del reminders_store[rid]
    if to_remove:
        save_reminders_data(reminders_store)
        print(f"[Reminders] Cleaned up {len(to_remove)} completed reminder(s)")

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()

@check_reminders.after_loop
async def after_check_reminders():
    if check_reminders.is_being_cancelled():
        print("[Reminders] Task was cancelled")
    else:
        print("[Reminders] Task stopped unexpectedly, will restart on next health check")

@tasks.loop(minutes=5)
async def health_monitor():
    """Monitor and restart background tasks if they stop"""
    try:
        current_time = time.time()
        print(f"[Health Monitor] Running at {datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        tasks_status = []
        
        # Check and restart newsfeed task
        if not check_newsfeed.is_running():
            print("[Health Monitor] Newsfeed task not running, restarting...")
            try:
                check_newsfeed.start()
                tasks_status.append("newsfeed: RESTARTED")
            except Exception as e:
                tasks_status.append(f"newsfeed: FAILED ({e})")
        else:
            tasks_status.append("newsfeed: OK")
        
        # Check and restart livetweets task
        if not check_livetweets.is_running():
            print("[Health Monitor] Livetweets task not running, restarting...")
            try:
                check_livetweets.start()
                tasks_status.append("livetweets: RESTARTED")
            except Exception as e:
                tasks_status.append(f"livetweets: FAILED ({e})")
        else:
            tasks_status.append("livetweets: OK")
        
        # Check and restart ghostwrite live task
        if not check_ghostwrite_live.is_running():
            print("[Health Monitor] Ghostwrite live task not running, restarting...")
            try:
                check_ghostwrite_live.start()
                tasks_status.append("ghostwrite: RESTARTED")
            except Exception as e:
                tasks_status.append(f"ghostwrite: FAILED ({e})")
        else:
            tasks_status.append("ghostwrite: OK")
        
        if not check_nftwatch.is_running():
            print("[Health Monitor] NFTWatch task not running, restarting...")
            try:
                check_nftwatch.start()
                tasks_status.append("nftwatch: RESTARTED")
            except Exception as e:
                tasks_status.append(f"nftwatch: FAILED ({e})")
        else:
            tasks_status.append("nftwatch: OK")

        if not check_reminders.is_running():
            print("[Health Monitor] Reminders task not running, restarting...")
            try:
                check_reminders.start()
                tasks_status.append("reminders: RESTARTED")
            except Exception as e:
                tasks_status.append(f"reminders: FAILED ({e})")
        else:
            tasks_status.append("reminders: OK")
        
        if not synthesize_server_digest.is_running():
            print("[Health Monitor] Digest task not running, restarting...")
            try:
                synthesize_server_digest.start()
                tasks_status.append("digest: RESTARTED")
            except Exception as e:
                tasks_status.append(f"digest: FAILED ({e})")
        else:
            tasks_status.append("digest: OK")

        if not vc_empty_monitor.is_running():
            print("[Health Monitor] VC monitor not running, restarting...")
            try:
                vc_empty_monitor.start()
                tasks_status.append("vc_monitor: RESTARTED")
            except Exception as e:
                tasks_status.append(f"vc_monitor: FAILED ({e})")
        else:
            tasks_status.append("vc_monitor: OK")

        if not check_redditfeed.is_running():
            print("[Health Monitor] Reddit feed task not running, restarting...")
            try:
                check_redditfeed.start()
                tasks_status.append("redditfeed: RESTARTED")
            except Exception as e:
                tasks_status.append(f"redditfeed: FAILED ({e})")
        else:
            tasks_status.append("redditfeed: OK")

        print(f"[Health Monitor] Status: {', '.join(tasks_status)}")
    except Exception as e:
        print(f"[Health Monitor] Error in health check: {e}")

@health_monitor.before_loop
async def before_health_monitor():
    await bot.wait_until_ready()

# ── VC empty-channel auto-disconnect ─────────────────────────────────────────
@tasks.loop(minutes=2)
async def vc_empty_monitor():
    """Leave any VC that has had no human members for 30 minutes."""
    now = time.time()
    to_disconnect = []
    for guild_id, session in list(vc_sessions.items()):
        vc = session.get("vc")
        if not vc or not vc.is_connected():
            to_disconnect.append(guild_id)
            continue
        # Count non-bot members in the channel
        human_count = sum(1 for m in vc.channel.members if not m.bot)
        if human_count == 0:
            if session["empty_since"] is None:
                session["empty_since"] = now
                print(f"[VC] Channel empty in guild {guild_id}, starting 60-min timer")
            elif now - session["empty_since"] >= 3600:
                print(f"[VC] 60 min empty, disconnecting from guild {guild_id}")
                await vc.disconnect()
                to_disconnect.append(guild_id)
        else:
            if session["empty_since"] is not None:
                print(f"[VC] Members returned in guild {guild_id}, resetting timer")
            session["empty_since"] = None
    for gid in to_disconnect:
        vc_sessions.pop(gid, None)

@vc_empty_monitor.before_loop
async def before_vc_empty_monitor():
    await bot.wait_until_ready()

@tasks.loop(hours=4)
async def synthesize_server_digest():
    """Every 4 hours, Grok reads the last 200 messages across the server and distills them
    into a living digest of members, ongoing topics, dynamics, and mood — injected into
    every @Grim reply so it feels like it's been paying attention."""
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return
    active_guilds = get_guilds_with_recent_activity(hours=12)
    if not active_guilds:
        return
    for guild_id in active_guilds:
        try:
            rows = get_server_history_for_digest(guild_id, limit=200)
            if len(rows) < 5:
                continue
            lines = []
            for author_name, content, is_grim_msg, channel_id, ts in rows:
                name = "Grim" if is_grim_msg else author_name
                channel_obj = bot.get_channel(int(channel_id)) if channel_id else None
                ch = f"#{channel_obj.name}" if channel_obj else "#chat"
                lines.append(f"[{ch}] {name}: {content}")
            log_text = "\n".join(lines)
            prompt = f"""You are synthesizing a Discord server's recent message log into a compact knowledge digest for an AI member named Grim.

Cover:
- Who the active members are and their general personality/vibe
- What topics have come up recently and what's ongoing
- Any notable events, decisions, plans, or inside references
- Relationship dynamics between members worth noting
- The overall energy and mood of the server lately

Be factual, concise, and genuinely useful. This digest will be injected into Grim's context on every reply so it can respond as a member who has been paying attention.

SERVER LOG (last {len(rows)} messages):
{log_text}"""
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": "grok-3",
                    "messages": [
                        {"role": "system", "content": "You synthesize Discord server logs into compact, factual context digests. Be precise and useful, not flowery."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 800,
                    "temperature": 0.3,
                }
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                async with session.post("https://api.x.ai/v1/chat/completions", json=payload, headers=headers) as r:
                    if r.status == 200:
                        data = await r.json()
                        digest_text = data["choices"][0]["message"]["content"].strip()
                        grim_digests[guild_id] = {
                            "text": digest_text,
                            "updated": time.time(),
                            "messages_processed": len(rows)
                        }
                        save_grim_digests()
                        print(f"[Digest] Updated for guild {guild_id} ({len(rows)} messages)")
                    else:
                        err = await r.text()
                        print(f"[Digest] API error {r.status}: {err}")
        except Exception as e:
            print(f"[Digest] Error for guild {guild_id}: {e}")

@synthesize_server_digest.before_loop
async def before_synthesize():
    await bot.wait_until_ready()

@synthesize_server_digest.after_loop
async def after_synthesize():
    if synthesize_server_digest.failed():
        print("[Digest] Task stopped unexpectedly")

async def sync_from_github():
    """Pull version.txt and updates_data.json from GitHub before startup — source of truth."""
    global updates_channels
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if not token:
        return
    repo = "Deathxi/Grim"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "User-Agent": "GrimBot"}
    files_to_sync = ["version.txt", "updates_data.json"]
    async with aiohttp.ClientSession() as session:
        for fname in files_to_sync:
            try:
                async with session.get(f"https://api.github.com/repos/{repo}/contents/{fname}?ref=main", headers=headers) as r:
                    data = await r.json()
                if "content" not in data:
                    print(f"[Sync] Could not fetch {fname} from GitHub: {data.get('message')}")
                    continue
                content = base64.b64decode(data["content"]).decode()
                with open(fname, "w") as f:
                    f.write(content)
                if fname == "version.txt":
                    # GitHub is source of truth — use whichever is higher (local or GitHub)
                    github_count = int(content.strip())
                    local_count = 0
                    if os.path.exists(VERSION_COUNT_FILE):
                        try:
                            with open(VERSION_COUNT_FILE, "r") as f:
                                local_count = int(f.read().strip())
                        except:
                            pass
                    if github_count >= local_count:
                        with open(VERSION_COUNT_FILE, "w") as f:
                            f.write(str(github_count))
                        print(f"[Sync] VERSION_COUNT_FILE set from GitHub: {github_count} (local was {local_count})")
                    else:
                        print(f"[Sync] Local VERSION_COUNT_FILE ({local_count}) ahead of GitHub ({github_count}) — keeping local")
                elif fname == "updates_data.json":
                    # Restore from GitHub if local persistent copy is missing or empty
                    needs_restore = True
                    if os.path.exists(UPDATES_CONFIG_FILE):
                        try:
                            with open(UPDATES_CONFIG_FILE, "r") as f:
                                existing = json.load(f)
                            if existing:  # has real data — trust it over GitHub
                                needs_restore = False
                        except:
                            pass
                    if needs_restore:
                        with open(UPDATES_CONFIG_FILE, "w") as f:
                            f.write(content)
                        print(f"[Sync] Restored persistent updates_data.json from GitHub")
                    else:
                        print(f"[Sync] Persistent updates_data.json has data — keeping it")
                print(f"[Sync] Pulled {fname} from GitHub")
            except Exception as e:
                print(f"[Sync] Failed to pull {fname}: {e}")
    # Reload updates_channels from the freshly pulled file
    updates_channels = load_updates_data()
    print(f"[Sync] updates_channels reloaded — {len(updates_channels)} guild(s) registered")
    # Set VERSION directly from the synced file so it's correct before _bump_version runs
    global VERSION
    try:
        with open("version.txt", "r") as f:
            VERSION = _format_version(int(f.read().strip()))
        print(f"[Sync] VERSION pre-set to {VERSION}")
    except Exception as e:
        print(f"[Sync] Could not pre-set VERSION: {e}")

@bot.event
async def on_ready():
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    print(f"{bot.user} has connected to Discord!")
    print(f"Bot is in {len(bot.guilds)} server(s)")
    print(f"[Startup] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    await sync_from_github()
    
    await bot.change_presence(activity=discord.Streaming(name="𝕹𝖎𝖍𝖎𝖑𝖎𝖘𝖙", url="https://www.twitch.tv/deathfy"))
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    if not check_livetweets.is_running():
        check_livetweets.start()
        print("Started livetweet checker")
    
    if not check_ghostwrite_live.is_running():
        check_ghostwrite_live.start()
        print("Started ghostwrite live checker")
    
    if not check_newsfeed.is_running():
        check_newsfeed.start()
        print("Started newsfeed checker")
    
    if not check_nftwatch.is_running():
        check_nftwatch.start()
        print("Started NFT watch checker")
    
    if not check_reminders.is_running():
        check_reminders.start()
        print("Started reminders checker")
    
    if not synthesize_server_digest.is_running():
        synthesize_server_digest.start()
        print("Started server digest synthesizer (runs every 4 hours)")

    if not health_monitor.is_running():
        health_monitor.start()
        print("Started health monitor (checks every 5 minutes)")

    if not vc_empty_monitor.is_running():
        vc_empty_monitor.start()
        print("Started VC empty-channel monitor (checks every 2 minutes)")

    if not check_redditfeed.is_running():
        check_redditfeed.start()
        print("Started Reddit feed checker")
    
    _bump_version()
    await _push_version_to_github()   # atomic — must succeed before notification fires
    asyncio.create_task(push_to_github_on_startup())
    asyncio.create_task(post_update_notification())

async def push_to_github_on_startup():
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if not token:
        return
    # Map: GitHub filename -> local path to read from
    # updates_data.json reads from persistent disk (the real data), not project root snapshot
    file_map = {
        "main.py": "main.py",
        "CHANGELOG.md": "CHANGELOG.md",
        ".gitignore": ".gitignore",
        "replit.md": "replit.md",
        "version.txt": "version.txt",
    }
    # Only push updates_data.json if it actually has channel config — never push empty data
    try:
        with open(UPDATES_CONFIG_FILE, "r") as _f:
            _ud = json.load(_f)
        if _ud:
            file_map["updates_data.json"] = UPDATES_CONFIG_FILE
        else:
            print("[GitHub Sync] Skipping updates_data.json push — file is empty, not overwriting GitHub copy")
    except:
        print("[GitHub Sync] Skipping updates_data.json push — could not read persistent file")
    repo = "Deathxi/Grim"
    branch = "main"
    pushed = []
    failed = []
    async with aiohttp.ClientSession() as session:
        for filepath, local_path in file_map.items():
            if not os.path.exists(local_path):
                continue
            try:
                with open(local_path, "rb") as f:
                    content = base64.b64encode(f.read()).decode()
                headers = {
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "GrimBot"
                }
                async with session.get(
                    f"https://api.github.com/repos/{repo}/contents/{filepath}?ref={branch}",
                    headers=headers
                ) as r:
                    existing = await r.json()
                sha = existing.get("sha")
                payload = {
                    "message": f"Auto-sync: update {filepath}",
                    "content": content,
                    "branch": branch
                }
                if sha:
                    payload["sha"] = sha
                async with session.put(
                    f"https://api.github.com/repos/{repo}/contents/{filepath}",
                    headers=headers,
                    json=payload
                ) as r:
                    result = await r.json()
                if "content" in result:
                    pushed.append(filepath)
                else:
                    failed.append(filepath)
            except Exception as e:
                failed.append(f"{filepath}({e})")
    if pushed:
        print(f"[GitHub Sync] Pushed: {', '.join(pushed)}")
    if failed:
        print(f"[GitHub Sync] Failed: {', '.join(failed)}")

LAST_ANNOUNCED_VERSION_FILE = _data_path("last_announced_version.txt")

def _load_last_announced_version():
    try:
        with open(LAST_ANNOUNCED_VERSION_FILE, "r") as f:
            return f.read().strip()
    except:
        return None

def _save_last_announced_version(version: str):
    with open(LAST_ANNOUNCED_VERSION_FILE, "w") as f:
        f.write(version)

def _load_changelog_notes() -> str:
    """Pull the most recent section from CHANGELOG.md if it exists."""
    try:
        with open("CHANGELOG.md", "r") as f:
            lines = f.readlines()
        notes = []
        in_section = False
        for line in lines:
            if line.startswith("## ") and not in_section:
                in_section = True
                continue
            if line.startswith("## ") and in_section:
                break
            if in_section and line.strip():
                notes.append(line.rstrip())
        return "\n".join(notes[:10]) if notes else ""
    except:
        return ""

async def post_update_notification():
    """Post an update embed to all registered channels when the version has changed.
    Fully GitHub-independent — uses persistent disk to track last announced version."""
    # Delay to ensure guild/channel cache is fully populated
    await asyncio.sleep(30)

    if not updates_channels:
        print("[Updates] No channels registered, skipping.")
        return

    last_version = _load_last_announced_version()
    if last_version == VERSION:
        print(f"[Updates] Already announced {VERSION}, skipping.")
        return

    print(f"[Updates] New version detected: {last_version or 'none'} → {VERSION}. Posting to {len(updates_channels)} channel(s).")

    # Try to pull commit data from GitHub for a richer embed
    repo = "Deathxi/Grim"
    branch = "main"
    new_commits = []
    changed_files = {}
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if token:
        try:
            headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "User-Agent": "GrimBot"}
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.github.com/repos/{repo}/commits?ref={branch}&per_page=25", headers=headers) as r:
                    all_commits = await r.json()
                if isinstance(all_commits, list) and all_commits:
                    latest_sha = all_commits[0]["sha"]
                    last_sha = updates_sha.get("_global")
                    for commit in all_commits:
                        if commit["sha"] == last_sha:
                            break
                        new_commits.append(commit)
                        if len(new_commits) >= 10:
                            break
                    # Fetch changed files from the most recent commits
                    for commit in new_commits[:5]:
                        async with session.get(f"https://api.github.com/repos/{repo}/commits/{commit['sha']}", headers=headers) as r:
                            detail = await r.json()
                        for file in detail.get("files", []):
                            changed_files[file["filename"]] = file["status"]
                    updates_sha["_global"] = latest_sha
                    save_updates_sha(updates_sha)
        except Exception as e:
            print(f"[Updates] Could not fetch GitHub commit data: {e}")

    # Build embed — always clean and minimal
    if new_commits:
        file_list = "\n".join(f"`{fname}`" for fname in list(changed_files.keys())[:10]) if changed_files else ""
        description = f"**{len(new_commits)} commit(s) deployed**"
        if file_list:
            description += f"\n\n{file_list}"
    else:
        description = "Grim has been updated and redeployed."
    embed = discord.Embed(
        title=f"Grim — {VERSION}",
        description=description,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="Repository", value=f"[{repo}](https://github.com/{repo})", inline=True)
    if changed_files:
        embed.add_field(name="Changes", value=str(len(changed_files)), inline=True)
    embed.set_footer(text=f"Powered by {BOT_NAME} • {VERSION}")

    posted = False
    for guild_id, data in list(updates_channels.items()):
        try:
            channel = await bot.fetch_channel(int(data["channel_id"]))
            await channel.send(embed=embed)
            print(f"[Updates] Posted to channel {data['channel_id']} in guild {guild_id}")
            posted = True
        except Exception as e:
            print(f"[Updates] Could not post to channel {data['channel_id']} in guild {guild_id}: {e}")

    if posted:
        _save_last_announced_version(VERSION)
        print(f"[Updates] Saved last announced version as {VERSION}")

@bot.tree.command(name="info", description="Get server status and info")
async def info(interaction: discord.Interaction):
    guild = interaction.guild
    
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)
        return
    
    member_count = guild.member_count
    online_count = sum(1 for m in guild.members if m.status != discord.Status.offline)
    bot_latency = round(bot.latency * 1000)
    
    embed = discord.Embed(
        title=guild.name,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    embed.add_field(name="Members", value=f"```{member_count:,}```", inline=True)
    embed.add_field(name="Online", value=f"```{online_count:,}```", inline=True)
    embed.add_field(name="Ping", value=f"```{bot_latency}ms```", inline=True)
    embed.add_field(name="ID", value=f"```{guild.id}```", inline=True)
    embed.add_field(name="Owner", value=f"{guild.owner.mention if guild.owner else 'Unknown'}", inline=True)
    embed.add_field(name="Created", value=f"```{guild.created_at.strftime('%b %d, %Y')}```", inline=True)
    embed.add_field(name="Channels", value=f"```{len(guild.channels)}```", inline=True)
    embed.add_field(name="Roles", value=f"```{len(guild.roles)}```", inline=True)
    embed.add_field(name="Boosts", value=f"```{guild.premium_subscription_count}```", inline=True)
    
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="howdie", description="How will someone meet their dramatic end?")
async def howdie(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    
    death_scene = await generate_death_scene(user.display_name)
    
    if death_scene is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    embed = discord.Embed(
        title=user.display_name,
        description=death_scene,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="8ball", description="Ask the existentially dread-filled Magic 8-ball")
async def eightball(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    
    answer = await generate_8ball_response(question)
    
    if answer is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    embed = discord.Embed(
        title="8ball",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="\u200b", value=f"*{question}*", inline=False)
    embed.add_field(name="\u200b", value=answer, inline=False)
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="truth", description="Ask Grok anything - unfiltered, raw answers")
async def truth(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    
    answer = await generate_unfiltered_truth(question)
    
    if answer is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    embed = discord.Embed(
        description=answer,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="\u200b", value=f"*{question}*", inline=False)
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="summon", description="Summon Grim from the shadows")
async def summon(interaction: discord.Interaction):
    await interaction.response.defer()
    
    message = await generate_summon_message()
    
    if message is None:
        await interaction.followup.send("*The shadows remain silent... xAI API key not configured.*")
        return
    
    embed = discord.Embed(
        description=f"*{message}*",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_author(name="Grim", icon_url=bot.user.display_avatar.url)
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="inspire", description="Get an inspiring real-world story to lift your spirits")
async def inspire(interaction: discord.Interaction):
    await interaction.response.defer()
    
    story = await generate_inspiration()
    
    if story is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    embed = discord.Embed(
        description=story,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="summarize", description="Get a private TLDR of recent channel conversation")
@discord.app_commands.describe(messages="Number of recent messages to summarize (e.g. 50)")
async def summarize(interaction: discord.Interaction, messages: int):
    if messages < 5:
        await interaction.response.send_message("Give me at least 5 messages to work with.", ephemeral=True)
        return
    if messages > 500:
        await interaction.response.send_message("Cap is 500 messages — try a smaller number.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        await interaction.followup.send("XAI_API_KEY not configured.", ephemeral=True)
        return

    # Fetch messages directly from Discord channel history
    try:
        history = []
        async for msg in interaction.channel.history(limit=messages):
            if msg.author.bot and msg.author.id != bot.user.id:
                continue
            name = "Grim" if msg.author.id == bot.user.id else msg.author.display_name
            if msg.content.strip():
                history.append((name, msg.content.strip()))
        history.reverse()  # oldest first
    except Exception as e:
        await interaction.followup.send("Couldn't fetch channel history.", ephemeral=True)
        return

    if len(history) < 3:
        await interaction.followup.send("Not enough messages in this channel to summarize.", ephemeral=True)
        return

    convo_text = "\n".join(f"{name}: {content}" for name, content in history)

    prompt = f"""Here are the last {len(history)} messages from #{getattr(interaction.channel, 'name', 'chat')}:

{convo_text}

Give a concise TLDR. Cover:
- What was being discussed
- Who was involved and what they said or contributed
- Any conclusions, decisions, or notable moments

Write it like a quick briefing — direct, no fluff. Natural prose, no bullet points."""

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "grok-3",
                "messages": [
                    {"role": "system", "content": "You summarize Discord conversations accurately and concisely. Name the participants. No filler, no markdown headers."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.4,
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            async with session.post("https://api.x.ai/v1/chat/completions", json=payload, headers=headers) as r:
                if r.status != 200:
                    await interaction.followup.send("API error — try again.", ephemeral=True)
                    return
                data = await r.json()
                summary = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        await interaction.followup.send("Something went wrong generating the summary.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Last {len(history)} messages · #{getattr(interaction.channel, 'name', 'chat')}",
        description=summary,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"Only visible to you · {VERSION}")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="roast", description="Roast a member with chaotic, unhinged energy")
async def roast(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    
    roast_text = await generate_roast(user.display_name)
    
    if roast_text is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    embed = discord.Embed(
        title=user.display_name,
        description=roast_text,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="ascii", description="Get a random ASCII art masterpiece")
async def ascii_art(interaction: discord.Interaction):
    await interaction.response.defer()
    
    art, theme = await generate_leet_art()
    
    if art is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    await interaction.followup.send(f"**{theme.upper()}**\n```\n{art}\n```")

@bot.tree.command(name="ghostwrite", description="Generate a tweet in someone's X writing style")
async def ghostwrite(interaction: discord.Interaction, username: str, topics: str):
    await interaction.response.defer()
    
    tweet_data, error = await fetch_user_tweets(username, count=15)
    
    if error:
        if "not configured" in error:
            await interaction.followup.send("X API not configured. Please add X_BEARER_TOKEN to secrets.")
        else:
            await interaction.followup.send(f"Error: {error}")
        return
    
    draft = await generate_ghostwrite(username, topics, tweet_data)
    
    if draft is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    clean_username = username.lstrip('@')
    
    embed = discord.Embed(
        title=f"@{clean_username}",
        description=f"```{draft}```",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="\u200b", value=f"**{topics}**", inline=False)
    embed.set_footer(text=f"{interaction.user.name} · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="ghostwritelive", description="Schedule automatic ghostwritten drafts at intervals")
async def ghostwritelive(interaction: discord.Interaction, interval: str, username: str, topic: str):
    global ghostwrite_live_channels
    
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    channel_id = str(interaction.channel_id)
    clean_username = username.lstrip('@').lower()
    
    # Check if disabling
    if channel_id in ghostwrite_live_channels:
        del ghostwrite_live_channels[channel_id]
        save_ghostwrite_live_data(ghostwrite_live_channels)
        
        embed = discord.Embed(
            title="Disabled",
            description=f"Stopped scheduled ghostwrites in this channel.",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        await interaction.followup.send(embed=embed)
        return
    
    # Parse interval (e.g., "4h", "12h", "30m", "10m")
    interval_str = interval.lower().strip()
    hours_match = re.match(r'^(\d+)h$', interval_str)
    mins_match = re.match(r'^(\d+)m$', interval_str)
    
    if hours_match:
        interval_hours = int(hours_match.group(1))
        interval_minutes = interval_hours * 60
        interval_display = f"{interval_hours}h"
    elif mins_match:
        interval_minutes = int(mins_match.group(1))
        interval_hours = interval_minutes / 60
        interval_display = f"{interval_minutes}m"
    else:
        await interaction.followup.send("Invalid interval format. Use format like `4h`, `12h` (hours) or `10m`, `30m` (minutes).")
        return
    
    if interval_minutes < 10 or interval_minutes > 10080:
        await interaction.followup.send("Interval must be between 10m and 168h (1 week).")
        return
    
    # Verify we can fetch tweets for this user
    tweet_data, error = await fetch_user_tweets(clean_username, count=15)
    
    if error:
        if "not configured" in error:
            await interaction.followup.send("X API not configured. Please add X_BEARER_TOKEN to secrets.")
        else:
            await interaction.followup.send(f"Error: {error}")
        return
    
    # Generate first one immediately
    draft, specific_topic = await generate_ghostwrite_live(clean_username, topic, tweet_data)
    
    if draft is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    # Save the schedule
    ghostwrite_live_channels[channel_id] = {
        "username": clean_username,
        "topic": topic,
        "interval_minutes": interval_minutes,
        "interval_display": interval_display,
        "last_run": time.time()
    }
    save_ghostwrite_live_data(ghostwrite_live_channels)
    
    embed = discord.Embed(
        title=f"Ghostwrite Live Enabled",
        description=f"```{draft}```",
        color=discord.Color.from_rgb(29, 161, 242)
    )
    embed.add_field(name="Researched Topic", value=specific_topic, inline=False)
    embed.add_field(name="Account", value=f"@{clean_username}", inline=True)
    embed.add_field(name="Broad Topic", value=topic, inline=True)
    embed.add_field(name="Interval", value=f"Every {interval_display}", inline=True)
    embed.set_footer(text=f"Enabled by {interaction.user.name} | Use same command to disable · {VERSION}")
    
    await interaction.followup.send(embed=embed)

class NewsfeedCancelSelect(ui.Select):
    def __init__(self, feeds: dict):
        options = []
        for feed_id, data in feeds.items():
            label = f"{data.get('topic', 'Unknown')} ({data.get('interval_display', '?')})"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(
                label=label,
                value=feed_id,
                description=f"Channel: {data.get('channel_id', 'unknown')}"
            ))
        
        super().__init__(
            placeholder="Select a newsfeed to cancel...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        global newsfeed_feeds
        feed_id = self.values[0]
        
        if feed_id in newsfeed_feeds:
            feed_data = newsfeed_feeds[feed_id]
            topic = feed_data.get("topic", "Unknown")
            del newsfeed_feeds[feed_id]
            save_newsfeed_data(newsfeed_feeds)
            
            embed = discord.Embed(
                title="Cancelled",
                description=f"Stopped news feed for **{topic}**",
                color=discord.Color.from_rgb(18, 18, 18)
            )
            embed.add_field(name="\u200b", value=f"```{feed_id}```", inline=True)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            embed = discord.Embed(
                title="Already Cancelled",
                description="This feed was already cancelled or no longer exists.",
                color=discord.Color.from_rgb(40, 40, 40)
            )
            await interaction.response.edit_message(embed=embed, view=None)

class NewsfeedCancelView(ui.View):
    def __init__(self, feeds: dict):
        super().__init__(timeout=120)
        self.add_item(NewsfeedCancelSelect(feeds))
        self.message = None
    
    async def on_timeout(self):
        if self.message:
            try:
                embed = discord.Embed(
                    title="Expired",
                    description="This menu has expired. Use `/newsfeed_cancel` again.",
                    color=discord.Color.from_rgb(40, 40, 40)
                )
                await self.message.edit(embed=embed, view=None)
            except:
                pass

# Newsfeed Edit Components
class NewsfeedEditModal(ui.Modal, title="Edit Interval"):
    new_interval = ui.TextInput(
        label="New Interval",
        placeholder="e.g., 12h, 30m, 4h",
        required=True,
        max_length=10
    )
    
    def __init__(self, feed_id: str, feed_data: dict):
        super().__init__()
        self.feed_id = feed_id
        self.feed_data = feed_data
    
    async def on_submit(self, interaction: discord.Interaction):
        global newsfeed_feeds
        
        interval_str = self.new_interval.value.lower().strip()
        hours_match = re.match(r'^(\d+)h$', interval_str)
        mins_match = re.match(r'^(\d+)m$', interval_str)
        
        if hours_match:
            interval_minutes = int(hours_match.group(1)) * 60
            interval_display = f"{hours_match.group(1)}h"
        elif mins_match:
            interval_minutes = int(mins_match.group(1))
            interval_display = f"{mins_match.group(1)}m"
        else:
            await interaction.response.send_message("Invalid interval format. Use '4h' or '30m'.", ephemeral=True)
            return
        
        if interval_minutes < 10:
            await interaction.response.send_message("Minimum interval is 10 minutes.", ephemeral=True)
            return
        
        if self.feed_id in newsfeed_feeds:
            old_interval = newsfeed_feeds[self.feed_id].get("interval_display", "?")
            newsfeed_feeds[self.feed_id]["interval_minutes"] = interval_minutes
            newsfeed_feeds[self.feed_id]["interval_display"] = interval_display
            save_newsfeed_data(newsfeed_feeds)
            
            topic = self.feed_data.get("topic", "Unknown")
            
            embed = discord.Embed(
                title="Updated",
                description=f"**{topic}** interval changed",
                color=discord.Color.from_rgb(18, 18, 18)
            )
            embed.add_field(name="\u200b", value=f"```{old_interval} → {interval_display}```", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("Feed no longer exists.", ephemeral=True)

class NewsfeedEditSelect(ui.Select):
    def __init__(self, feeds: dict):
        self.feeds_data = feeds
        options = []
        for feed_id, data in feeds.items():
            topic = data.get("topic", "Unknown")
            interval = data.get("interval_display", "?")
            options.append(discord.SelectOption(
                label=f"{topic} ({interval})",
                value=feed_id,
                description=f"Channel: {data.get('channel_id', 'unknown')}"
            ))
        
        super().__init__(
            placeholder="Select a newsfeed to edit...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        feed_id = self.values[0]
        if feed_id in self.feeds_data:
            modal = NewsfeedEditModal(feed_id, self.feeds_data[feed_id])
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message("Feed no longer exists.", ephemeral=True)

class NewsfeedEditView(ui.View):
    def __init__(self, feeds: dict):
        super().__init__(timeout=120)
        self.add_item(NewsfeedEditSelect(feeds))
        self.message = None
    
    async def on_timeout(self):
        if self.message:
            try:
                embed = discord.Embed(
                    title="Expired",
                    description="This menu has expired. Use `/newsfeed_edit` again.",
                    color=discord.Color.from_rgb(40, 40, 40)
                )
                await self.message.edit(embed=embed, view=None)
            except:
                pass

@bot.tree.command(name="newsfeed_edit", description="Edit the interval of an active news feed")
async def newsfeed_edit(interaction: discord.Interaction):
    global newsfeed_feeds
    
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    
    if not newsfeed_feeds:
        await interaction.response.send_message("No active news feeds to edit.", ephemeral=True)
        return
    
    # Filter to feeds in this guild — use guild_id if stored, fall back to channel lookup
    guild_id_str = str(interaction.guild_id)
    guild_feeds = {}
    for feed_id, data in newsfeed_feeds.items():
        if data.get("guild_id") == guild_id_str:
            guild_feeds[feed_id] = data
        elif not data.get("guild_id"):
            try:
                channel = interaction.guild.get_channel(int(data.get("channel_id", 0)))
                if channel:
                    guild_feeds[feed_id] = data
            except:
                pass
    
    if not guild_feeds:
        await interaction.response.send_message("No active news feeds in this server.", ephemeral=True)
        return
    
    view = NewsfeedEditView(guild_feeds)
    embed = discord.Embed(
        title="Edit Feed",
        description=f"Select a feed to edit.\n\n**Active:** {len(guild_feeds)}",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

@bot.tree.command(name="newsfeed_cancel", description="Cancel an active news feed")
async def newsfeed_cancel(interaction: discord.Interaction):
    global newsfeed_feeds
    
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    
    if not newsfeed_feeds:
        await interaction.response.send_message("No active news feeds to cancel.", ephemeral=True)
        return
    
    # Filter to feeds in this guild — use guild_id if stored, fall back to channel lookup
    guild_id_str = str(interaction.guild_id)
    guild_feeds = {}
    for feed_id, data in newsfeed_feeds.items():
        if data.get("guild_id") == guild_id_str:
            guild_feeds[feed_id] = data
        elif not data.get("guild_id"):
            try:
                channel = interaction.guild.get_channel(int(data.get("channel_id", 0)))
                if channel:
                    guild_feeds[feed_id] = data
            except:
                pass
    
    if not guild_feeds:
        await interaction.response.send_message("No active news feeds in this server.", ephemeral=True)
        return
    
    view = NewsfeedCancelView(guild_feeds)
    embed = discord.Embed(
        title="Cancel Feed",
        description=f"Select a feed to cancel.\n\n**Active:** {len(guild_feeds)}",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

@bot.tree.command(name="newsfeed_status", description="View active news feeds and their status")
async def newsfeed_status(interaction: discord.Interaction):
    global newsfeed_feeds
    
    current_time = time.time()
    
    # Check task health
    tasks_healthy = {
        "Newsfeed": check_newsfeed.is_running(),
        "Livetweets": check_livetweets.is_running(),
        "Ghostwrite": check_ghostwrite_live.is_running(),
        "Health Monitor": health_monitor.is_running()
    }
    
    health_status = "Operational" if all(tasks_healthy.values()) else "Attention needed"
    
    embed = discord.Embed(
        title="Status",
        description=f"**{health_status}**",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    
    # Task status
    task_lines = []
    for task_name, is_running in tasks_healthy.items():
        status = "Active" if is_running else "Stopped"
        task_lines.append(f"**{task_name}:** {status}")
    embed.add_field(name="Tasks", value="\n".join(task_lines), inline=False)
    
    # Filter to feeds in this guild — use guild_id if stored, fall back to channel lookup
    guild_id_str = str(interaction.guild_id)
    guild_feeds = {}
    for feed_id, data in newsfeed_feeds.items():
        if data.get("guild_id") == guild_id_str:
            guild_feeds[feed_id] = data
        elif not data.get("guild_id"):
            try:
                channel = interaction.guild.get_channel(int(data.get("channel_id", 0)))
                if channel:
                    guild_feeds[feed_id] = data
            except:
                pass
    
    if guild_feeds:
        feed_lines = []
        for feed_id, data in guild_feeds.items():
            channel_id = data.get("channel_id")
            topic = data.get("topic", "Unknown")
            interval_display = data.get("interval_display", "?")
            interval_seconds = data.get("interval_minutes", 60) * 60
            last_run = data.get("last_run", 0)
            
            # Calculate next run
            next_run = last_run + interval_seconds
            time_until = next_run - current_time
            
            if time_until <= 0:
                next_str = "**Due now**"
            elif time_until < 60:
                next_str = f"in {int(time_until)}s"
            elif time_until < 3600:
                next_str = f"in {int(time_until / 60)}m"
            else:
                hours = int(time_until / 3600)
                mins = int((time_until % 3600) / 60)
                next_str = f"in {hours}h {mins}m"
            
            feed_lines.append(f"**{topic}** ({interval_display})\n<#{channel_id}> • Next: {next_str}")
        
        embed.add_field(name=f"Feeds ({len(guild_feeds)})", value="\n".join(feed_lines), inline=False)
    else:
        embed.add_field(name="Feeds", value="No active feeds", inline=False)
    
    # Bot uptime info
    embed.set_footer(text=f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · {VERSION}")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="redditfeed_status", description="View active Reddit image feeds and their status")
async def redditfeed_status(interaction: discord.Interaction):
    global redditfeed_feeds

    current_time = time.time()
    guild_id_str = str(interaction.guild_id)

    guild_feeds = {fid: d for fid, d in redditfeed_feeds.items() if d.get("guild_id") == guild_id_str}

    task_ok = check_redditfeed.is_running()
    embed = discord.Embed(
        title="Reddit Feed Status",
        description=f"**{'Operational' if task_ok else 'Attention needed'}**",
        color=discord.Color.from_rgb(18, 18, 18)
    )

    embed.add_field(
        name="Task",
        value=f"**Reddit Feed:** {'Active' if task_ok else 'Stopped'}",
        inline=False
    )

    if guild_feeds:
        feed_lines = []
        for feed_id, data in guild_feeds.items():
            channel_id = data.get("channel_id")
            subs = ", ".join([f"r/{s}" for s in data.get("subreddits", [])])
            interval_minutes = data.get("interval_minutes", 60)
            interval_display = f"{interval_minutes // 60}h" if interval_minutes % 60 == 0 else f"{interval_minutes}m"
            last_run = data.get("last_run", 0)
            interval_seconds = interval_minutes * 60
            next_run = last_run + interval_seconds
            time_until = next_run - current_time

            if time_until <= 0:
                next_str = "**Due now**"
            elif time_until < 60:
                next_str = f"in {int(time_until)}s"
            elif time_until < 3600:
                next_str = f"in {int(time_until / 60)}m"
            else:
                h = int(time_until / 3600)
                m = int((time_until % 3600) / 60)
                next_str = f"in {h}h {m}m"

            feed_lines.append(f"**{subs}** ({interval_display})\n<#{channel_id}> · Next: {next_str} · `{feed_id}`")

        embed.add_field(name=f"Feeds ({len(guild_feeds)})", value="\n".join(feed_lines), inline=False)
    else:
        embed.add_field(name="Feeds", value="No active Reddit feeds", inline=False)

    embed.set_footer(text=f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="status", description="Full system and API health dashboard")
async def grim_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    now = time.time()

    # ── System metrics ────────────────────────────────────
    cpu_pct = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    ram_used = ram.used / (1024 ** 3)
    ram_total = ram.total / (1024 ** 3)
    disk_used = disk.used / (1024 ** 3)
    disk_total = disk.total / (1024 ** 3)

    # ── Bot stats ─────────────────────────────────────────
    latency_ms = round(bot.latency * 1000)
    uptime_secs = int(now - BOT_START_TIME) if BOT_START_TIME else 0
    days = uptime_secs // 86400
    hours = (uptime_secs % 86400) // 3600
    mins = (uptime_secs % 3600) // 60
    uptime_str = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"
    guild_count = len(bot.guilds)
    total_members = sum(g.member_count or 0 for g in bot.guilds)

    # ── API health checks (run concurrently) ─────────────
    api_key = os.environ.get("XAI_API_KEY")
    x_bearer = os.environ.get("X_BEARER_TOKEN")
    opensea_key = os.environ.get("OPENSEA_API_KEY")

    async def check_xai():
        if not api_key:
            return "No key", None
        try:
            t0 = time.time()
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": "grok-3", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    lat = round((time.time() - t0) * 1000)
                    return ("Online" if r.status in (200, 400) else f"Error {r.status}"), lat
        except:
            return "Unreachable", None

    async def check_x():
        if not x_bearer:
            return "No key", None
        try:
            t0 = time.time()
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.twitter.com/2/users/by/username/twitter",
                    headers={"Authorization": f"Bearer {x_bearer}"},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    lat = round((time.time() - t0) * 1000)
                    return ("Online" if r.status in (200, 400) else f"Error {r.status}"), lat
        except:
            return "Unreachable", None

    (xai_status, xai_lat), (x_status, x_lat) = await asyncio.gather(check_xai(), check_x())

    # ── Background tasks ──────────────────────────────────
    task_map = {
        "Newsfeed": check_newsfeed,
        "Reddit Feed": check_redditfeed,
        "Livetweets": check_livetweets,
        "Ghostwrite Live": check_ghostwrite_live,
        "NFT Watch": check_nftwatch,
        "Reminders": check_reminders,
        "Digest": synthesize_server_digest,
        "Health Monitor": health_monitor,
        "VC Monitor": vc_empty_monitor,
    }

    # ── Active features (this server) ─────────────────────
    guild_id_str = str(interaction.guild_id)
    active_feeds = sum(1 for d in newsfeed_feeds.values() if d.get("guild_id") == guild_id_str)
    active_reddit = sum(1 for d in redditfeed_feeds.values() if d.get("guild_id") == guild_id_str)
    active_tweets = sum(1 for d in livetweet_channels.values() if isinstance(d, dict) and d.get("guild_id") == guild_id_str)
    active_nft = sum(1 for d in nftwatch_feeds.values() if isinstance(d, dict) and d.get("guild_id") == guild_id_str)
    vc_session = vc_sessions.get(guild_id_str)
    vc_str = f"In **{vc_session['vc'].channel.name}**" if vc_session and vc_session.get("vc") and vc_session["vc"].is_connected() else "Inactive"

    # ── Build embed ───────────────────────────────────────
    def tick(ok): return "✓" if ok else "✗"

    embed = discord.Embed(
        title="Grim — System Status",
        color=discord.Color.from_rgb(18, 18, 18)
    )

    embed.add_field(
        name="System",
        value=(
            f"**CPU:** {cpu_pct}%\n"
            f"**RAM:** {ram_used:.1f} / {ram_total:.1f} GB ({ram.percent}%)\n"
            f"**Disk:** {disk_used:.1f} / {disk_total:.1f} GB ({disk.percent}%)"
        ),
        inline=True
    )

    embed.add_field(
        name="Bot",
        value=(
            f"**Uptime:** {uptime_str}\n"
            f"**Ping:** {latency_ms}ms\n"
            f"**Servers:** {guild_count}\n"
            f"**Members:** {total_members}"
        ),
        inline=True
    )

    embed.add_field(name="\u200b", value="\u200b", inline=True)

    xai_line = f"{tick(xai_status == 'Online')} xAI — {xai_status}" + (f" ({xai_lat}ms)" if xai_lat else "")
    x_line = f"{tick(x_status == 'Online')} X/Twitter — {x_status}" + (f" ({x_lat}ms)" if x_lat else "")
    discord_line = f"✓ Discord — Online ({latency_ms}ms)"
    opensea_line = f"{tick(bool(opensea_key))} OpenSea — {'Key set' if opensea_key else 'No key'}"
    embed.add_field(
        name="APIs",
        value=f"{discord_line}\n{xai_line}\n{x_line}\n{opensea_line}",
        inline=True
    )

    task_lines = [f"{tick(t.is_running())} {n}" for n, t in task_map.items()]
    embed.add_field(
        name="Background Tasks",
        value="\n".join(task_lines),
        inline=True
    )

    embed.add_field(
        name="Active (this server)",
        value=(
            f"**Newsfeeds:** {active_feeds}\n"
            f"**Reddit Feeds:** {active_reddit}\n"
            f"**Livetweets:** {active_tweets}\n"
            f"**NFT Watches:** {active_nft}\n"
            f"**Voice:** {vc_str}"
        ),
        inline=True
    )

    embed.set_footer(text=f"Grim · {VERSION} · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="newsfeed", description="Start a live news feed for a topic in this channel")
async def newsfeed(interaction: discord.Interaction, interval: str, topic: str):
    global newsfeed_feeds
    
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    channel_id = str(interaction.channel_id)
    
    # Parse interval (e.g., "4h", "12h", "30m", "10m")
    interval_str = interval.lower().strip()
    hours_match = re.match(r'^(\d+)h$', interval_str)
    mins_match = re.match(r'^(\d+)m$', interval_str)
    
    if hours_match:
        interval_minutes = int(hours_match.group(1)) * 60
        interval_display = f"{hours_match.group(1)}h"
    elif mins_match:
        interval_minutes = int(mins_match.group(1))
        interval_display = f"{interval_minutes}m"
    else:
        await interaction.followup.send("Invalid interval format. Use format like `4h`, `12h` (hours) or `10m`, `30m` (minutes).")
        return
    
    if interval_minutes < 10 or interval_minutes > 10080:
        await interaction.followup.send("Interval must be between 10m and 168h (1 week).")
        return
    
    # Generate first news update immediately
    headline, content, image_url = await generate_news_update(topic)
    
    if headline is None:
        await interaction.followup.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    # Create unique feed ID and save
    feed_id = str(uuid.uuid4())
    newsfeed_feeds[feed_id] = {
        "channel_id": channel_id,
        "guild_id": str(interaction.guild_id),
        "topic": topic,
        "interval_minutes": interval_minutes,
        "interval_display": interval_display,
        "last_run": time.time(),
        "posted_headlines": [headline]
    }
    save_newsfeed_data(newsfeed_feeds)
    
    print(f"[Newsfeed Command] Created feed {feed_id}, image_url value: {image_url}")
    
    embed = discord.Embed(
        title=headline,
        description=content,
        color=discord.Color.from_rgb(18, 18, 18)
    )
    
    embed.add_field(name="\u200b", value=f"```{topic}```", inline=True)
    embed.add_field(name="\u200b", value=f"```{interval_display}```", inline=True)
    embed.set_footer(text=f"Grim News Network · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.context_menu(name="Quote")
async def quote_message(interaction: discord.Interaction, message: discord.Message):
    target = message.author
    content = message.content.strip()
    
    if not content:
        await interaction.response.send_message("That message has no text to quote.", ephemeral=True)
        return
    
    if len(content) > 900:
        content = content[:897] + "..."
    
    avatar_url = target.display_avatar.replace(size=512).url
    
    display_name = target.display_name if hasattr(target, 'display_name') else target.name
    
    quoted_text = f"\u201c{content}\u201d"
    
    embed = discord.Embed(
        description=f"### {quoted_text}\n\u200b",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_author(name=display_name, icon_url=avatar_url)
    embed.set_image(url=avatar_url)
    embed.set_footer(text=f"@{target.name} · {VERSION}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="nftwatch", description="Watch an OpenSea collection for live new listings")
async def nftwatch(interaction: discord.Interaction, link: str):
    global nftwatch_feeds
    
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    
    api_key = os.environ.get("OPENSEA_API_KEY")
    if not api_key:
        await interaction.response.send_message("OpenSea API key not configured. Please add OPENSEA_API_KEY to secrets.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    parsed = parse_opensea_url(link)
    if not parsed or parsed["type"] != "slug":
        await interaction.followup.send("Invalid OpenSea collection link. Use a URL like `https://opensea.io/collection/collection-name`")
        return
    
    slug = parsed["slug"]
    channel_id = str(interaction.channel_id)
    
    for wid, wdata in nftwatch_feeds.items():
        if wdata.get("slug") == slug and wdata.get("channel_id") == channel_id:
            await interaction.followup.send(f"Already watching **{slug}** in this channel.")
            return
    
    async with aiohttp.ClientSession() as session:
        collection_data = await fetch_opensea_api(session, f"/collections/{slug}")
        if not collection_data:
            await interaction.followup.send(f"Could not find collection **{slug}** on OpenSea. Check the link and try again.")
            return
    
    watch_id = str(uuid.uuid4())[:8]
    nftwatch_feeds[watch_id] = {
        "channel_id": channel_id,
        "guild_id": str(interaction.guild_id),
        "slug": slug,
        "last_event_time": time.time(),
        "collection_name": collection_data.get("name", slug)
    }
    save_nftwatch_data(nftwatch_feeds)
    
    collection_name = collection_data.get("name", slug)
    image_url = collection_data.get("image_url", "")
    
    embed = discord.Embed(
        title="NFT Watch Active",
        description=f"Monitoring **{collection_name}** for new listings.\nPolling every 30 seconds.",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    if image_url:
        embed.set_thumbnail(url=image_url)
    embed.add_field(name="\u200b", value=f"```{slug}```", inline=True)
    embed.add_field(name="\u200b", value=f"```LIVE```", inline=True)
    embed.set_footer(text=f"Grim NFT Watch · {VERSION}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="nftwatch_cancel", description="Cancel an active NFT watch in this channel")
async def nftwatch_cancel(interaction: discord.Interaction):
    global nftwatch_feeds
    
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    
    guild_id = str(interaction.guild_id)
    guild_watches = {wid: wdata for wid, wdata in nftwatch_feeds.items() if wdata.get("guild_id") == guild_id}
    
    if not guild_watches:
        await interaction.response.send_message("No active NFT watches in this server.", ephemeral=True)
        return
    
    if len(guild_watches) == 1:
        wid = list(guild_watches.keys())[0]
        wdata = guild_watches[wid]
        del nftwatch_feeds[wid]
        save_nftwatch_data(nftwatch_feeds)
        
        embed = discord.Embed(
            title="NFT Watch Cancelled",
            description=f"Stopped watching **{wdata.get('collection_name', wdata['slug'])}**",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        embed.set_footer(text=f"Grim NFT Watch · {VERSION}")
        await interaction.response.send_message(embed=embed)
        return
    
    options = []
    for wid, wdata in guild_watches.items():
        cname = wdata.get("collection_name", wdata["slug"])
        options.append(discord.SelectOption(label=cname[:100], value=wid, description=f"Channel: #{bot.get_channel(int(wdata['channel_id']))}"))
    
    class NFTWatchCancelSelect(ui.Select):
        def __init__(self):
            super().__init__(placeholder="Select a watch to cancel...", options=options, min_values=1, max_values=len(options))
        
        async def callback(self, inter: discord.Interaction):
            cancelled = []
            for wid in self.values:
                if wid in nftwatch_feeds:
                    cancelled.append(nftwatch_feeds[wid].get("collection_name", nftwatch_feeds[wid]["slug"]))
                    del nftwatch_feeds[wid]
            save_nftwatch_data(nftwatch_feeds)
            
            embed = discord.Embed(
                title="NFT Watch Cancelled",
                description="\n".join([f"Stopped watching **{c}**" for c in cancelled]),
                color=discord.Color.from_rgb(18, 18, 18)
            )
            embed.set_footer(text=f"Grim NFT Watch · {VERSION}")
            await inter.response.send_message(embed=embed)
    
    class NFTWatchCancelView(ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(NFTWatchCancelSelect())
    
    await interaction.response.send_message("Select which NFT watch(es) to cancel:", view=NFTWatchCancelView(), ephemeral=True)

# ── Reddit Feed ───────────────────────────────────────────────────────────────

def _parse_subreddit_name(raw: str) -> str:
    """Extract subreddit name from a URL, r/name, or plain name."""
    raw = raw.strip().rstrip("/")
    # Handle full URLs: https://www.reddit.com/r/SubName or https://reddit.com/r/SubName
    if "reddit.com/r/" in raw:
        part = raw.split("reddit.com/r/")[-1]
        return part.split("/")[0]
    # Handle r/SubName
    if raw.lower().startswith("r/"):
        return raw[2:]
    return raw

@bot.tree.command(name="redditfeed", description="Post images from Reddit subreddits on a schedule")
@discord.app_commands.describe(
    subreddits="Subreddit names or links, comma-separated (e.g. r/DarkAesthetic or reddit.com/r/darkcore)",
    interval="How often to post, e.g. 30m or 12h (min 10m)"
)
async def redditfeed(interaction: discord.Interaction, subreddits: str, interval: str):
    global redditfeed_feeds

    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return

    interval = interval.strip().lower()
    if interval.endswith("h") and interval[:-1].isdigit():
        interval_minutes = int(interval[:-1]) * 60
        interval_display = f"{interval[:-1]}h"
    elif interval.endswith("m") and interval[:-1].isdigit():
        interval_minutes = int(interval[:-1])
        interval_display = f"{interval[:-1]}m"
    elif interval.isdigit():
        interval_minutes = int(interval)
        interval_display = f"{interval}m"
    else:
        await interaction.response.send_message("Invalid interval. Use formats like `30m` or `12h`.", ephemeral=True)
        return

    if interval_minutes < 10:
        await interaction.response.send_message("Minimum interval is 10 minutes (`10m`).", ephemeral=True)
        return

    sub_list = [_parse_subreddit_name(s) for s in subreddits.split(",") if s.strip()]
    if not sub_list:
        await interaction.response.send_message("Please provide at least one subreddit.", ephemeral=True)
        return
    if len(sub_list) > 10:
        await interaction.response.send_message("Maximum of 10 subreddits per feed.", ephemeral=True)
        return

    feed_id = str(uuid.uuid4())[:8]
    redditfeed_feeds[feed_id] = {
        "channel_id": str(interaction.channel_id),
        "guild_id": str(interaction.guild_id),
        "subreddits": sub_list,
        "interval_minutes": interval_minutes,
        "last_run": 0,
        "posted_urls": []
    }
    save_redditfeed_data(redditfeed_feeds)

    sub_display = ", ".join([f"r/{s}" for s in sub_list])
    embed = discord.Embed(
        title="Reddit Feed Started",
        description=f"Posting images every **{interval_display}** from:\n{sub_display}",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="\u200b", value=f"```{feed_id}```", inline=True)
    embed.set_footer(text=f"Grim Reddit Feed · {VERSION}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


class RedditfeedCancelSelect(ui.Select):
    def __init__(self, feeds: dict):
        options = []
        for feed_id, data in feeds.items():
            subs = ", ".join([f"r/{s}" for s in data.get("subreddits", [])])[:100]
            channel = bot.get_channel(int(data.get("channel_id", 0)))
            ch_name = f"#{channel.name}" if channel else "unknown channel"
            options.append(discord.SelectOption(
                label=subs,
                value=feed_id,
                description=f"{ch_name} · every {data.get('interval_minutes', '?')} min"
            ))
        super().__init__(
            placeholder="Select a Reddit feed to cancel...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        global redditfeed_feeds
        feed_id = self.values[0]
        if feed_id in redditfeed_feeds:
            data = redditfeed_feeds[feed_id]
            subs = ", ".join([f"r/{s}" for s in data.get("subreddits", [])])
            del redditfeed_feeds[feed_id]
            save_redditfeed_data(redditfeed_feeds)
            embed = discord.Embed(
                title="Cancelled",
                description=f"Stopped Reddit feed for **{subs}**",
                color=discord.Color.from_rgb(18, 18, 18)
            )
            embed.add_field(name="\u200b", value=f"```{feed_id}```", inline=True)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            embed = discord.Embed(
                title="Already Cancelled",
                description="This feed was already cancelled or no longer exists.",
                color=discord.Color.from_rgb(40, 40, 40)
            )
            await interaction.response.edit_message(embed=embed, view=None)


class RedditfeedCancelView(ui.View):
    def __init__(self, feeds: dict):
        super().__init__(timeout=120)
        self.add_item(RedditfeedCancelSelect(feeds))
        self.message = None

    async def on_timeout(self):
        if self.message:
            try:
                embed = discord.Embed(
                    title="Expired",
                    description="This menu has expired. Use `/redditfeed_cancel` again.",
                    color=discord.Color.from_rgb(40, 40, 40)
                )
                await self.message.edit(embed=embed, view=None)
            except:
                pass


@bot.tree.command(name="redditfeed_cancel", description="Cancel an active Reddit image feed")
async def redditfeed_cancel(interaction: discord.Interaction):
    global redditfeed_feeds

    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return

    guild_id_str = str(interaction.guild_id)
    guild_feeds = {fid: d for fid, d in redditfeed_feeds.items() if d.get("guild_id") == guild_id_str}

    if not guild_feeds:
        await interaction.response.send_message("No active Reddit feeds in this server.", ephemeral=True)
        return

    view = RedditfeedCancelView(guild_feeds)
    embed = discord.Embed(
        title="Cancel Reddit Feed",
        description=f"Select a feed to cancel.\n\n**Active:** {len(guild_feeds)}",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

@bot.tree.command(name="mod_add", description="Add a word to the auto-delete list")
async def mod_add(interaction: discord.Interaction, word: str):
    global moderation_data
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    
    word = word.strip().lower()
    if not word:
        await interaction.response.send_message("Please provide a valid word.", ephemeral=True)
        return
    
    if word in [w.lower() for w in moderation_data["banned_words"]]:
        await interaction.response.send_message(f"Already on the list.", ephemeral=True)
        return
    
    moderation_data["banned_words"].append(word)
    save_moderation_data(moderation_data)
    
    embed = discord.Embed(
        title="Word Added",
        description=f"```{word}```",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"{len(moderation_data['banned_words'])} word(s) on list · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="mod_remove", description="Remove a word from the auto-delete list")
async def mod_remove(interaction: discord.Interaction, word: str):
    global moderation_data
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    
    word = word.strip().lower()
    original = moderation_data["banned_words"]
    updated = [w for w in original if w.lower() != word]
    
    if len(updated) == len(original):
        await interaction.response.send_message(f"That word wasn't on the list.", ephemeral=True)
        return
    
    moderation_data["banned_words"] = updated
    save_moderation_data(moderation_data)
    
    embed = discord.Embed(
        title="Word Removed",
        description=f"```{word}```",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"{len(moderation_data['banned_words'])} word(s) on list · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="mod_list", description="View all words on the auto-delete list")
async def mod_list(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    
    words = moderation_data.get("banned_words", [])
    
    embed = discord.Embed(
        title="Auto-Delete List",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    
    if words:
        embed.description = "```\n" + "\n".join(words) + "\n```"
    else:
        embed.description = "```empty```"
    
    embed.set_footer(text=f"{len(words)} word(s) · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remind", description="Set a reminder for a product drop or event")
async def remind(interaction: discord.Interaction, subject: str, when: str):
    global reminders_store
    
    target_dt = parse_reminder_datetime(when)
    if not target_dt:
        await interaction.response.send_message(
            "Couldn't parse that date. Use format like `06/19 0:00` or `06/19 14:30`",
            ephemeral=True
        )
        return
    
    target_ts = target_dt.timestamp()
    if target_ts <= time.time():
        await interaction.response.send_message("That date is in the past.", ephemeral=True)
        return
    
    day_before_dt = target_dt - timedelta(days=1)
    drop_display = target_dt.strftime("%m/%d %H:%M")
    day_before_display = day_before_dt.strftime("%m/%d")
    
    rid = str(uuid.uuid4())[:8]
    reminders_store[rid] = {
        "channel_id": str(interaction.channel_id),
        "guild_id": str(interaction.guild_id),
        "user_id": str(interaction.user.id),
        "subject": subject,
        "target_timestamp": target_ts,
        "day_before_sent": target_ts - time.time() < 86400,
        "day_of_sent": False,
        "drop_display": drop_display,
        "day_before_display": day_before_display
    }
    save_reminders_data(reminders_store)
    
    embed = discord.Embed(
        title="**Time Is Of The Essence**",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.description = subject if not is_url(subject) else f"[Open Link]({subject})"
    embed.add_field(name="Day Before", value=f"```{day_before_display}```", inline=True)
    embed.add_field(name="Drop", value=f"```{drop_display}```", inline=True)
    embed.set_footer(text=f"Grim Reminder — Set · {VERSION}")
    
    content = None
    if is_url(subject):
        content = subject
    
    await interaction.response.send_message(content=content, embed=embed)

@bot.tree.command(name="reminders", description="View and cancel your active reminders")
async def reminders_cmd(interaction: discord.Interaction):
    global reminders_store
    
    guild_id_str = str(interaction.guild_id)
    user_id_str = str(interaction.user.id)
    is_admin = interaction.user.guild_permissions.administrator
    
    if is_admin:
        user_reminders = {rid: d for rid, d in reminders_store.items() if d.get("guild_id") == guild_id_str}
    else:
        user_reminders = {rid: d for rid, d in reminders_store.items() if d.get("user_id") == user_id_str and d.get("guild_id") == guild_id_str}
    
    if not user_reminders:
        await interaction.response.send_message("No active reminders.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="Active Reminders",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    
    for rid, data in user_reminders.items():
        subj = data["subject"]
        label = subj if len(subj) <= 50 else subj[:47] + "..."
        day_before_status = "✓" if data.get("day_before_sent") else "pending"
        drop_status = "pending"
        field_val = f"Drop: `{data['drop_display']}` • Day before: {day_before_status}\nID: `{rid}`"
        embed.add_field(name=label, value=field_val, inline=False)
    
    embed.set_footer(text=f"Use /remind_cancel <id> to remove one · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remind_cancel", description="Cancel an active reminder by its ID")
async def remind_cancel(interaction: discord.Interaction, reminder_id: str):
    global reminders_store
    
    rid = reminder_id.strip()
    if rid not in reminders_store:
        await interaction.response.send_message("Reminder not found. Use `/reminders` to see your IDs.", ephemeral=True)
        return
    
    data = reminders_store[rid]
    is_owner = data.get("user_id") == str(interaction.user.id)
    is_admin = interaction.user.guild_permissions.administrator
    
    if not is_owner and not is_admin:
        await interaction.response.send_message("You can only cancel your own reminders.", ephemeral=True)
        return
    
    subj = data["subject"]
    del reminders_store[rid]
    save_reminders_data(reminders_store)
    
    embed = discord.Embed(
        title="Reminder Cancelled",
        description=subj if len(subj) <= 100 else subj[:97] + "...",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"Grim Reminder · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

SECLUDE_ICON_URL = "https://cdn.discordapp.com/icons/1101443658953261076/a_7df56c851d8a26e198d706cc3c640426.webp?size=1024&animated=true"

@bot.tree.command(name="support", description="Get support or connect with the Seclude community")
async def support(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Support & Community",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="CONTACT", value="[x@deathi.net](mailto:x@deathi.net)", inline=False)
    embed.add_field(name="HUB / FAQ", value="[Seclude & Affiliates](https://discord.com/invite/KFcpDGtckz)", inline=False)
    if SECLUDE_ICON_URL:
        embed.set_thumbnail(url=SECLUDE_ICON_URL)
    embed.set_footer(text=f"Grim · {VERSION}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="creator", description="Meet the creator of Grim")
async def creator(interaction: discord.Interaction):
    creator_id = 235194449573969920
    
    embed = discord.Embed(
        title="Creator",
        description=f"<@{creator_id}>\n**Western Reaper**",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="\u200b", value="[deathi.net](https://deathi.net)", inline=False)
    embed.set_footer(text=f"Grim · {VERSION}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="livetweet", description="Toggle live tweet updates from an X account in this channel")
async def livetweet(interaction: discord.Interaction, username: str):
    global livetweet_channels
    
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    channel_id = str(interaction.channel_id)
    clean_username = username.lstrip('@')
    
    if channel_id in livetweet_channels and livetweet_channels[channel_id]["username"].lower() == clean_username.lower():
        del livetweet_channels[channel_id]
        save_livetweet_data(livetweet_channels)
        
        embed = discord.Embed(
            title="Disabled",
            description=f"Stopped tracking **@{clean_username}**",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        await interaction.followup.send(embed=embed)
        return
    
    twitter = get_twitter_client()
    if not twitter:
        await interaction.followup.send("X API not configured. Please add X_BEARER_TOKEN to secrets.")
        return
    
    try:
        user = twitter.get_user(username=clean_username, user_fields=['profile_image_url', 'name'])
        
        if not user.data:
            await interaction.followup.send(f"Could not find X user **@{clean_username}**. Check the username and try again.")
            return
        
        tweets = twitter.get_users_tweets(id=user.data.id, max_results=5)
        last_tweet_id = str(tweets.data[0].id) if tweets.data else None
        
        livetweet_channels[channel_id] = {
            "username": clean_username,
            "user_id": str(user.data.id),
            "last_tweet_id": last_tweet_id
        }
        save_livetweet_data(livetweet_channels)
        
        embed = discord.Embed(
            title="Enabled",
            description=f"Now tracking **@{clean_username}**",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        if hasattr(user.data, 'profile_image_url'):
            embed.set_thumbnail(url=user.data.profile_image_url)
        embed.set_footer(text=f"Run again to disable · {VERSION}")
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        print(f"Error setting up livetweet: {e}")
        await interaction.followup.send(f"Error: Could not set up tracking. The X API may be rate limited or the username is invalid.")

@bot.tree.command(name="grim_updates", description="Toggle Grim update announcements in this channel")
async def grim_updates(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    # Defer immediately — GitHub push can take a few seconds
    await interaction.response.defer()
    guild_id = str(interaction.guild_id)
    if guild_id in updates_channels:
        del updates_channels[guild_id]
        updates_sha.pop(guild_id, None)
        save_updates_data(updates_channels)
        save_updates_sha(updates_sha)
        embed = discord.Embed(
            title="Update Announcements Disabled",
            description="Grim will no longer post patch notes in this server.",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        embed.set_footer(text=f"Powered by {BOT_NAME} • {VERSION}")
    else:
        updates_channels[guild_id] = {"channel_id": str(interaction.channel_id)}
        save_updates_data(updates_channels)
        embed = discord.Embed(
            title="Update Announcements Enabled",
            description=f"Grim will post patch notes in <#{interaction.channel_id}> whenever a new version is deployed.",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        embed.set_footer(text=f"Powered by {BOT_NAME} • {VERSION}")
    # Push config to GitHub immediately so it survives the next redeploy
    # NOTE: always push to "updates_data.json" (GitHub path), read from UPDATES_CONFIG_FILE (local persistent path)
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if token:
        try:
            with open(UPDATES_CONFIG_FILE, "rb") as f:
                content = base64.b64encode(f.read()).decode()
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "User-Agent": "GrimBot"}
                async with session.get(f"https://api.github.com/repos/Deathxi/Grim/contents/updates_data.json?ref=main", headers=headers) as r:
                    existing = await r.json()
                payload = {"message": "Update updates_data.json via bot command", "content": content, "branch": "main"}
                if existing.get("sha"):
                    payload["sha"] = existing["sha"]
                async with session.put(f"https://api.github.com/repos/Deathxi/Grim/contents/updates_data.json", headers=headers, json=payload) as r:
                    result = await r.json()
                if "content" in result:
                    print(f"[Updates] Pushed updates_data.json to GitHub ✓")
                else:
                    print(f"[Updates] GitHub push failed: {result.get('message')}")
        except Exception as e:
            print(f"[Updates] Could not push config to GitHub: {e}")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="welcome_on", description="Enable welcome messages for new members in this channel")
async def welcome_on(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    welcome_channels[guild_id] = str(interaction.channel_id)
    save_welcome_data(welcome_channels)
    embed = discord.Embed(
        title="Welcome Messages Enabled",
        description=f"New member greetings will be posted in <#{interaction.channel_id}>.",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"Grim · {VERSION}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="welcome_off", description="Disable welcome messages for new members")
async def welcome_off(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("You need 'Manage Channels' permission to use this command.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    if guild_id in welcome_channels:
        del welcome_channels[guild_id]
        save_welcome_data(welcome_channels)
        embed = discord.Embed(
            title="Welcome Messages Disabled",
            description="New member greetings have been turned off.",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        embed.set_footer(text=f"Grim · {VERSION}")
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("Welcome messages are not enabled in this server.", ephemeral=True)

# ── Voice Channel Commands ────────────────────────────────────────────────────
@bot.tree.command(name="vc_join", description="Have Grim join your voice channel")
async def vc_join(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild_id)

    # Must be in a voice channel
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("You need to be in a voice channel first.", ephemeral=True)
        return

    channel = interaction.user.voice.channel

    # Already connected in this guild — move if needed
    existing = vc_sessions.get(guild_id)
    if existing and existing["vc"] and existing["vc"].is_connected():
        if existing["vc"].channel.id == channel.id:
            await interaction.followup.send(f"Already in **{channel.name}**.", ephemeral=True)
            return
        await existing["vc"].move_to(channel)
        existing["empty_since"] = None
        await interaction.followup.send(f"Moved to **{channel.name}**.", ephemeral=True)
        return

    try:
        vc = await channel.connect()
        vc_sessions[guild_id] = {"vc": vc, "empty_since": None}
        embed = discord.Embed(
            description=f"Joined **{channel.name}**.\nI'll leave automatically if the channel stays empty for 1 hour.",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        embed.set_footer(text=f"Grim · {VERSION}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        print(f"[VC] Joined {channel.name} in guild {guild_id}")
    except discord.ClientException as e:
        await interaction.followup.send(f"Couldn't join: {e}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send("Something went wrong joining the channel.", ephemeral=True)
        print(f"[VC] Join error: {e}")

@bot.tree.command(name="vc_leave", description="Have Grim leave the voice channel")
async def vc_leave(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id = str(interaction.guild_id)

    session = vc_sessions.get(guild_id)
    if not session or not session["vc"] or not session["vc"].is_connected():
        await interaction.followup.send("Not in a voice channel right now.", ephemeral=True)
        return

    channel_name = session["vc"].channel.name
    await session["vc"].disconnect()
    vc_sessions.pop(guild_id, None)
    embed = discord.Embed(
        description=f"Left **{channel_name}**.",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"Grim · {VERSION}")
    await interaction.followup.send(embed=embed, ephemeral=True)
    print(f"[VC] Left {channel_name} in guild {guild_id}")

@bot.event
async def on_member_join(member):
    print(f"{member.name} has joined {member.guild.name}")
    guild_id = str(member.guild.id)
    if guild_id not in welcome_channels:
        return
    channel = member.guild.get_channel(int(welcome_channels[guild_id]))
    if not channel:
        return
    avatar_url = member.display_avatar.url
    embed = discord.Embed(
        title=f"Greetings, {member.name}",
        description=f"Welcome to **{member.guild.name}**",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text=f"Powered by {BOT_NAME} • {VERSION}")
    await channel.send(embed=embed)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    banned = moderation_data.get("banned_words", [])
    if banned:
        content_lower = message.content.lower()
        for word in banned:
            if word.lower() in content_lower:
                try:
                    await message.delete()
                    print(f"[Moderation] Deleted message from {message.author.name} — matched: {word}")
                except Exception as e:
                    print(f"[Moderation] Failed to delete message: {e}")
                return
    
    # Persist every human message to the chat history DB
    if message.guild:
        save_message_to_db(
            str(message.guild.id), str(message.channel.id),
            str(message.id), message.author.display_name,
            message.content, message.created_at.timestamp(), is_grim=False
        )
        # Update member profile if they've crossed a milestone
        gid  = str(message.guild.id)
        mid  = str(message.author.id)
        name = message.author.display_name
        msg_count = get_member_message_count(gid, name)
        if profile_needs_update(gid, mid, msg_count):
            asyncio.create_task(_synthesize_member_profile(gid, mid, name, msg_count))

        # 0.5% chance to drop a comical surveillance warning
        if random.random() < 0.005 and message.channel.type in (discord.ChannelType.text, discord.ChannelType.news):
            _SURVEILLANCE_WARNINGS = [
                "Palantir just flagged this channel.",
                "Palantir's sentiment analysis is running hot on this one.",
                "Palantir added that to a profile. somewhere.",
                "Blackrock is watching. they're always watching.",
                "Blackrock's data team just logged this.",
                "Blackrock owns the servers this is running through.",
                "META's ad algorithm just learned something new about you.",
                "META filed that under 'behavioral signals.'",
                "META already sold that sentence to three advertisers.",
                "the NSA has a copy of this. they always do.",
                "NSA flagged that keyword. enjoy your day.",
                "the NSA's passive collection just picked that up.",
                "the CIA opened a new tab for this.",
                "a CIA contractor just got an alert.",
                "the CIA doesn't comment on ongoing operations.",
                "Interpol cross-referenced that. internationally.",
                "Interpol has a file on this channel now.",
                "Interpol's digital crimes unit sends their regards.",
            ]
            await message.channel.send(random.choice(_SURVEILLANCE_WARNINGS))

    if bot.user in message.mentions:
        # Reset counter — Grim is already responding, no need to also proactively chime
        _channel_msg_counter[str(message.channel.id)] = 0
        _channel_last_grim_post[str(message.channel.id)] = time.time()
        async with message.channel.typing():
            reply = await generate_contextual_reply(message)
        if reply:
            sent = await message.reply(reply, mention_author=False)
            # Persist Grim's reply so it's part of future context
            if message.guild:
                save_message_to_db(
                    str(message.guild.id), str(message.channel.id),
                    str(sent.id), BOT_NAME,
                    reply, sent.created_at.timestamp(), is_grim=True
                )
        else:
            await message.reply("something went sideways on my end, try again", mention_author=False)
    else:
        # Not @mentioned — let Grim decide if it wants to drop in
        await maybe_chime_in(message)

    await bot.process_commands(message)

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong! Latency: {round(bot.latency * 1000)}ms")

@bot.command(name="info")
async def info(ctx):
    embed = discord.Embed(
        title="Grim",
        description="Seclude & Affiliates",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="\u200b", value=f"```!```", inline=True)
    embed.add_field(name="\u200b", value=f"```{len(bot.guilds)}```", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="haiku")
async def haiku(ctx):
    haiku_text = await generate_haiku()
    
    if haiku_text is None:
        await ctx.send("xAI API key not configured. Please add XAI_API_KEY to secrets.")
        return
    
    embed = discord.Embed(
        description=f"*{haiku_text}*",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.set_footer(text=f"Grim · {VERSION}")
    await ctx.send(embed=embed)

@bot.command(name="help_grim")
async def help_grim(ctx):
    embed = discord.Embed(
        title="Commands",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="!ping", value="Latency", inline=True)
    embed.add_field(name="!info", value="Bot info", inline=True)
    embed.add_field(name="!haiku", value="Haiku", inline=True)
    embed.add_field(name="/info", value="Server status", inline=True)
    embed.add_field(name="/howdie", value="Fate", inline=True)
    embed.add_field(name="/8ball", value="Ask", inline=True)
    embed.add_field(name="/truth", value="Unfiltered", inline=True)
    embed.add_field(name="/summon", value="Summon", inline=True)
    embed.add_field(name="/inspire", value="Story", inline=True)
    embed.add_field(name="/creator", value="Creator", inline=True)
    embed.add_field(name="/roast", value="Roast", inline=True)
    embed.add_field(name="/ascii", value="ASCII art", inline=True)
    embed.add_field(name="/livetweet", value="X updates", inline=True)
    embed.add_field(name="/newsfeed", value="News feed", inline=True)
    embed.add_field(name="/newsfeed_edit", value="Edit interval", inline=True)
    embed.add_field(name="/ghostwrite", value="Ghostwrite", inline=True)
    embed.add_field(name="/nftwatch", value="NFT tracker", inline=True)
    embed.add_field(name="/nftwatch_cancel", value="Stop NFT watch", inline=True)
    embed.add_field(name="/redditfeed", value="Reddit image feed", inline=True)
    embed.add_field(name="/redditfeed_cancel", value="Stop Reddit feed", inline=True)
    embed.add_field(name="/redditfeed_status", value="Reddit feed status", inline=True)
    embed.set_footer(text=f"Grim · {VERSION}")
    await ctx.send(embed=embed)

@bot.tree.command(name="grim_github_test", description="Test GitHub connection and token status (admin only)")
async def grim_github_test(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("admins only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if not token:
        await interaction.followup.send("❌ `GITHUB_PERSONAL_ACCESS_TOKEN` is not set in secrets.", ephemeral=True)
        return
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"token {token}", "User-Agent": "GrimBot", "Accept": "application/vnd.github.v3+json"}
            async with session.get("https://api.github.com/user", headers=headers) as r:
                status = r.status
                data = await r.json()
                scopes = r.headers.get("X-OAuth-Scopes", "none")
        if status == 200:
            login = data.get("login", "unknown")
            has_repo = "repo" in scopes
            lines = [
                f"✅ Token valid — authenticated as **{login}**",
                f"Scopes: `{scopes}`",
                f"Repo access: {'✅' if has_repo else '❌ missing `repo` scope'}",
                f"Token prefix: `{token[:10]}...`",
            ]
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        else:
            await interaction.followup.send(f"❌ GitHub returned `{status}`: {data.get('message', 'unknown error')}\nToken prefix: `{token[:10]}...`", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Request failed: {e}", ephemeral=True)

@bot.tree.command(name="grim_remember", description="Give Grim a permanent memory about this server")
@discord.app_commands.describe(memory="The fact or detail you want Grim to remember")
async def grim_remember(interaction: discord.Interaction, memory: str):
    guild_id = str(interaction.guild_id)
    if guild_id not in grim_memories:
        grim_memories[guild_id] = []
    if memory in grim_memories[guild_id]:
        await interaction.response.send_message("already know that one.", ephemeral=True)
        return
    grim_memories[guild_id].append(memory)
    save_grim_memories()
    embed = discord.Embed(
        description=f"got it. i'll remember that.",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="memory added", value=f"```{memory}```", inline=False)
    embed.set_footer(text=f"Grim · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="grim_memories", description="View everything Grim has been told to remember about this server")
async def grim_memories_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    memories = grim_memories.get(guild_id, [])
    embed = discord.Embed(
        title="what i know",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    if not memories:
        embed.description = "nothing stored yet. use `/grim_remember` to add something."
    else:
        lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(memories))
        embed.description = f"```{lines}```"
        embed.set_footer(text=f"{len(memories)} memor{'y' if len(memories) == 1 else 'ies'} · Grim · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="grim_forget", description="Remove a memory Grim has about this server")
@discord.app_commands.describe(number="The memory number from /grim_memories")
async def grim_forget(interaction: discord.Interaction, number: int):
    guild_id = str(interaction.guild_id)
    memories = grim_memories.get(guild_id, [])
    if not memories:
        await interaction.response.send_message("nothing to forget.", ephemeral=True)
        return
    if number < 1 or number > len(memories):
        await interaction.response.send_message(f"pick a number between 1 and {len(memories)}.", ephemeral=True)
        return
    removed = memories.pop(number - 1)
    grim_memories[guild_id] = memories
    save_grim_memories()
    embed = discord.Embed(
        description=f"forgotten.",
        color=discord.Color.from_rgb(18, 18, 18)
    )
    embed.add_field(name="removed", value=f"```{removed}```", inline=False)
    embed.set_footer(text=f"Grim · {VERSION}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

token = os.environ.get("DISCORD_TOKEN")
if not token:
    print("ERROR: DISCORD_TOKEN not found in environment variables!")
    print("Please add your Discord bot token as a secret.")
else:
    bot.run(token)
