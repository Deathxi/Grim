import os
import random
import json
import asyncio
import aiohttp
import uuid
import base64
import discord
from discord.ext import commands, tasks
from discord import ui
from openai import OpenAI
import tweepy

BOT_NAME = "Grim"

VERSION_COUNT_FILE = os.path.expanduser("~/.grim_data/version_count.txt")

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

def _bump_version():
    global VERSION
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
    print(f"[Version] Deploy #{count} → {VERSION}")

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

# Channel config lives in project root — pushed to GitHub so it survives redeploys
UPDATES_CONFIG_FILE = "updates_data.json"
# SHA tracking lives in ~/.grim_data/ — ephemeral, resetting on fresh deploy is fine
UPDATES_SHA_FILE = _data_path("updates_sha.json")

def load_updates_data():
    try:
        if os.path.exists(UPDATES_CONFIG_FILE):
            with open(UPDATES_CONFIG_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_updates_data(data):
    with open(UPDATES_CONFIG_FILE, 'w') as f:
        json.dump(data, f)

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
        
        print(f"[Health Monitor] Status: {', '.join(tasks_status)}")
    except Exception as e:
        print(f"[Health Monitor] Error in health check: {e}")

@health_monitor.before_loop
async def before_health_monitor():
    await bot.wait_until_ready()

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
                # Also sync version.txt into persistent counter file
                if fname == "version.txt":
                    with open(VERSION_COUNT_FILE, "w") as f:
                        f.write(content.strip())
                print(f"[Sync] Pulled {fname} from GitHub")
            except Exception as e:
                print(f"[Sync] Failed to pull {fname}: {e}")
    # Reload updates_channels from the freshly pulled file
    updates_channels = load_updates_data()
    print(f"[Sync] updates_channels reloaded — {len(updates_channels)} guild(s) registered")

@bot.event
async def on_ready():
    print(f"{bot.user} has connected to Discord!")
    print(f"Bot is in {len(bot.guilds)} server(s)")
    print(f"[Startup] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    await sync_from_github()
    
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="𝕴𝖋 𝕷𝖔𝖔𝖐𝖘 𝕮𝖔𝖚𝖑𝖉 𝕶𝖎𝖑𝖑"))
    
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
    
    if not health_monitor.is_running():
        health_monitor.start()
        print("Started health monitor (checks every 5 minutes)")
    
    _bump_version()
    asyncio.create_task(push_to_github_on_startup())

async def push_to_github_on_startup():
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if not token:
        return
    files = ["main.py", "CHANGELOG.md", ".gitignore", "replit.md", "version.txt", "updates_data.json"]
    repo = "Deathxi/Grim"
    branch = "main"
    pushed = []
    failed = []
    async with aiohttp.ClientSession() as session:
        for filepath in files:
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "rb") as f:
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
    await post_update_notification()

async def post_update_notification():
    if not updates_channels:
        print("[Updates] No channels registered, skipping.")
        return
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if not token:
        print("[Updates] No GitHub token, skipping.")
        return
    # Small delay to ensure guild/channel cache is fully populated after startup
    await asyncio.sleep(5)
    repo = "Deathxi/Grim"
    branch = "main"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GrimBot"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.github.com/repos/{repo}/commits?ref={branch}&per_page=25",
                headers=headers
            ) as r:
                all_commits = await r.json()
            if not isinstance(all_commits, list) or not all_commits:
                print(f"[Updates] GitHub returned unexpected response: {all_commits}")
                return
            latest_sha = all_commits[0]["sha"]
            print(f"[Updates] Latest GitHub SHA: {latest_sha[:7]} — checking {len(updates_channels)} channel(s)")
            for guild_id, data in list(updates_channels.items()):
                last_sha = updates_sha.get(guild_id)
                print(f"[Updates] Guild {guild_id} — last SHA: {last_sha[:7] if last_sha else 'None'}")
                # Collect commits newer than last_sha (cap at 10)
                new_commits = []
                for commit in all_commits:
                    if commit["sha"] == last_sha:
                        break
                    new_commits.append(commit)
                    if len(new_commits) >= 10:
                        break
                if not new_commits:
                    print(f"[Updates] No new commits for guild {guild_id}, skipping.")
                    updates_sha[guild_id] = latest_sha
                    save_updates_sha(updates_sha)
                    continue
                print(f"[Updates] {len(new_commits)} new commit(s) to post for guild {guild_id}")
                # Fetch changed files across new commits
                changed_files = {}
                for commit in new_commits[:5]:
                    async with session.get(
                        f"https://api.github.com/repos/{repo}/commits/{commit['sha']}",
                        headers=headers
                    ) as r:
                        detail = await r.json()
                    for file in detail.get("files", []):
                        changed_files[file["filename"]] = file["status"]
                file_list = "\n".join(f"`{fname}`" for fname in list(changed_files.keys())[:10]) or "`main.py`"
                embed = discord.Embed(
                    title=f"Grim — {VERSION}",
                    description=f"**{len(new_commits)} commit(s)** deployed\n\n{file_list}",
                    color=discord.Color.from_rgb(18, 18, 18)
                )
                embed.add_field(name="Repository", value="[Deathxi/Grim](https://github.com/Deathxi/Grim)", inline=True)
                embed.add_field(name="Changes", value=str(len(changed_files)), inline=True)
                embed.set_footer(text=f"Powered by {BOT_NAME} • {VERSION}")
                try:
                    channel = await bot.fetch_channel(int(data["channel_id"]))
                    await channel.send(embed=embed)
                    print(f"[Updates] Posted to channel {data['channel_id']} in guild {guild_id}")
                    # Only advance SHA if post succeeded
                    updates_sha[guild_id] = latest_sha
                    save_updates_sha(updates_sha)
                except Exception as ce:
                    print(f"[Updates] Could not post to channel {data['channel_id']}: {ce}")
    except Exception as e:
        print(f"[Updates] Failed to post update notification: {e}")

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
        await interaction.response.send_message(embed=embed)
    else:
        updates_channels[guild_id] = {"channel_id": str(interaction.channel_id)}
        save_updates_data(updates_channels)
        embed = discord.Embed(
            title="Update Announcements Enabled",
            description=f"Grim will post patch notes in <#{interaction.channel_id}> whenever a new version is deployed.",
            color=discord.Color.from_rgb(18, 18, 18)
        )
        embed.set_footer(text=f"Powered by {BOT_NAME} • {VERSION}")
        await interaction.response.send_message(embed=embed)
    # Push config to GitHub immediately so it survives the next redeploy
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if token:
        try:
            import base64 as _b64
            with open(UPDATES_CONFIG_FILE, "rb") as f:
                content = _b64.b64encode(f.read()).decode()
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "User-Agent": "GrimBot"}
                async with session.get(f"https://api.github.com/repos/Deathxi/Grim/contents/{UPDATES_CONFIG_FILE}?ref=main", headers=headers) as r:
                    existing = await r.json()
                payload = {"message": "Update updates_data.json via bot command", "content": content, "branch": "main"}
                if existing.get("sha"):
                    payload["sha"] = existing["sha"]
                await session.put(f"https://api.github.com/repos/Deathxi/Grim/contents/{UPDATES_CONFIG_FILE}", headers=headers, json=payload)
                print(f"[Updates] Pushed updates_data.json to GitHub")
        except Exception as e:
            print(f"[Updates] Could not push config to GitHub: {e}")

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
    
    if bot.user in message.mentions:
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '').strip()
        
        if clean_content:
            reply = await generate_reply(clean_content, message.author.display_name)
            if reply:
                await message.reply(reply, mention_author=False)
        else:
            await message.reply("You rang?", mention_author=False)
    
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
    embed.set_footer(text=f"Grim · {VERSION}")
    await ctx.send(embed=embed)

token = os.environ.get("DISCORD_TOKEN")
if not token:
    print("ERROR: DISCORD_TOKEN not found in environment variables!")
    print("Please add your Discord bot token as a secret.")
else:
    bot.run(token)
