import asyncio
import os
import sys
import io
import re
import json
import math
import base64
import discord
import aiohttp
from PIL import Image

print("==> bot.py starting up", flush=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN not set!", flush=True)
    sys.exit(1)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = "able0089/Namer"
DB_FILE_PATH = "learned.json"

# Accept spawns from ANY bot account to handle Poketwo clones like "P2 (:"
POKETWO_BOT_ID = 716390085896962058  # official Poketwo ID (kept for catch/fled)
HASH_SIZE   = 16
MAX_POKEMON = 1025

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}

print("==> Config loaded.", flush=True)
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

learned_cache: dict[str, str] = {}
last_spawn:    dict[int, tuple[str, discord.Message]] = {}
sprite_db:     dict[str, list[int]] = {}
github_sha:    str | None = None


# ── GITHUB PERSISTENCE ────────────────────────────────────────────────────────

def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

async def load_from_github(session: aiohttp.ClientSession):
    global learned_cache, github_sha
    if not GITHUB_TOKEN:
        print("==> No GITHUB_TOKEN — learned data won't persist.", flush=True)
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DB_FILE_PATH}"
    try:
        async with session.get(url, headers=gh_headers()) as resp:
            if resp.status == 404:
                print("==> learned.json not in repo yet — starting fresh.", flush=True)
                return
            data = await resp.json()
            github_sha = data["sha"]
            content = base64.b64decode(data["content"]).decode("utf-8")
            learned_cache = json.loads(content)
            print(f"==> Loaded {len(learned_cache)} Pokémon from GitHub.", flush=True)
    except Exception as e:
        print(f"==> GitHub load error: {e}", flush=True)

async def save_to_github(session: aiohttp.ClientSession):
    global github_sha
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DB_FILE_PATH}"
    content_b64 = base64.b64encode(
        json.dumps(learned_cache, indent=2).encode()
    ).decode()
    payload: dict = {
        "message": f"bot: learned {len(learned_cache)} pokemon",
        "content": content_b64,
    }
    if github_sha:
        payload["sha"] = github_sha
    try:
        async with session.put(url, headers=gh_headers(), json=payload) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                github_sha = data["content"]["sha"]
                print(f"==> Saved to GitHub! Total: {len(learned_cache)} Pokémon.", flush=True)
            else:
                text = await resp.text()
                print(f"==> GitHub save failed {resp.status}: {text}", flush=True)
    except Exception as e:
        print(f"==> GitHub save error: {e}", flush=True)

async def teach(hash_str: str, name: str, source: str, session: aiohttp.ClientSession):
    name = name.strip().capitalize()
    if not hash_str or not name:
        return
    if learned_cache.get(hash_str) == name:
        return
    learned_cache[hash_str] = name
    print(f"==> Learned: {name} (via {source}) — {len(learned_cache)} total", flush=True)
    await save_to_github(session)


# ── IMAGE HASHING ─────────────────────────────────────────────────────────────

def phash(img: Image.Image) -> list[int]:
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    img = bg.convert("L").resize((HASH_SIZE, HASH_SIZE), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    return [1 if p > avg else 0 for p in pixels]

def hash_to_str(h: list[int]) -> str:
    return "".join(str(b) for b in h)

def hamming(h1: list[int], h2: list[int]) -> int:
    return sum(b1 != b2 for b1, b2 in zip(h1, h2))

async def get_image_hash(image_url: str, session: aiohttp.ClientSession):
    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None, None
            img = Image.open(io.BytesIO(await resp.read()))
            h = phash(img)
            return h, hash_to_str(h)
    except Exception as e:
        print(f"==> Image fetch error: {e}", flush=True)
        return None, None


# ── SPRITE DATABASE ───────────────────────────────────────────────────────────

async def build_sprite_db(session: aiohttp.ClientSession):
    global sprite_db
    print("==> Fetching Pokémon list…", flush=True)
    try:
        async with session.get(
            f"https://pokeapi.co/api/v2/pokemon?limit={MAX_POKEMON}",
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            data = await resp.json()
            pokemon_list = data["results"]
    except Exception as e:
        print(f"==> Failed: {e}", flush=True)
        return

    print("==> Downloading HOME sprites…", flush=True)
    success = 0
    for entry in pokemon_list:
        name   = entry["name"]
        dex_id = entry["url"].rstrip("/").split("/")[-1]
        url    = f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/home/{dex_id}.png"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                img = Image.open(io.BytesIO(await resp.read()))
                sprite_db[name] = phash(img)
                success += 1
        except Exception:
            pass
        if success % 100 == 0 and success > 0:
            print(f"==> {success} sprites loaded…", flush=True)
            await asyncio.sleep(0.3)
    print(f"==> ✅ Sprite DB ready! {success} Pokémon loaded.", flush=True)

def guess_from_sprites(spawn_hash: list[int]) -> tuple[str | None, float]:
    if not sprite_db:
        return None, 0
    best_name, best_dist = None, math.inf
    for name, h in sprite_db.items():
        d = hamming(spawn_hash, h)
        if d < best_dist:
            best_dist = d
            best_name = name
    confidence = round((1 - best_dist / (HASH_SIZE * HASH_SIZE)) * 100, 1)
    return best_name, confidence


# ── MESSAGE HELPERS ───────────────────────────────────────────────────────────

def get_spawn_image_url(message: discord.Message) -> str | None:
    for embed in message.embeds:
        if embed.image and embed.image.url:
            return embed.image.url
        if embed.thumbnail and embed.thumbnail.url:
            return embed.thumbnail.url
    return None

def is_spawn_message(message: discord.Message) -> bool:
    """Accept spawns from ANY bot — handles official Poketwo and clones like P2."""
    if not message.author.bot:
        return False
    for embed in message.embeds:
        title = (embed.title or "").lower()
        desc  = (embed.description or "").lower()
        if "wild" in title and "pokémon" in title:
            return True
        if "wild" in desc and "pokémon" in desc:
            return True
    return False

def is_poketwo(message: discord.Message) -> bool:
    """Check if message is from any Poketwo-like bot."""
    return message.author.bot

def extract_fled_name(message: discord.Message) -> str | None:
    for embed in message.embeds:
        match = re.search(r"Wild (.+?) fled", embed.title or "", re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None

def extract_catch_name(message: discord.Message) -> str | None:
    content = message.content or ""
    match = re.search(r"[Yy]ou caught (?:a|an) (?:level \d+ )?(.+?)!", content)
    if match:
        return match.group(1).strip()
    for embed in message.embeds:
        match = re.search(r"[Yy]ou caught (?:a|an) (?:level \d+ )?(.+?)!", embed.description or "")
        if match:
            return match.group(1).strip()
    return None


# ── DISCORD BOT ───────────────────────────────────────────────────────────────

async def run_bot():
    delay = 10
    attempt = 0

    while True:
        attempt += 1
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"==> Logged in as {client.user} (ID: {client.user.id})", flush=True)
            print(f"==> Ready! Learned DB has {len(learned_cache)} Pokémon.", flush=True)

        @client.event
        async def on_message(message: discord.Message):
            channel_id = message.channel.id
            if WATCH_CHANNEL_IDS and channel_id not in WATCH_CHANNEL_IDS:
                return

            # ── SPAWN ─────────────────────────────────────────────────────────
            if is_spawn_message(message):
                print(f"==> Spawn detected in #{message.channel} from {message.author}", flush=True)

                # Learn from fled name in new spawn title
                fled_name = extract_fled_name(message)
                if fled_name and channel_id in last_spawn:
                    prev_hash_str, prev_bot_msg = last_spawn[channel_id]
                    async with aiohttp.ClientSession() as session:
                        await teach(prev_hash_str, fled_name, "fled", session)
                    try:
                        await prev_bot_msg.edit(content=f"🔍 That was **{fled_name.capitalize()}**!")
                    except Exception:
                        pass

                image_url = get_spawn_image_url(message)
                if not image_url:
                    print("==> No image found in spawn embed.", flush=True)
                    return

                async with aiohttp.ClientSession() as session:
                    spawn_hash, hash_str = await get_image_hash(image_url, session)

                if spawn_hash is None:
                    return

                if hash_str in learned_cache:
                    name = learned_cache[hash_str]
                    print(f"==> Known: {name}", flush=True)
                    bot_msg = await message.channel.send(
                        f"🔍 That's **{name}**!",
                        reference=message,
                        mention_author=False,
                    )
                else:
                    guess, confidence = guess_from_sprites(spawn_hash)
                    if guess and confidence >= 55:
                        print(f"==> Guessing: {guess} ({confidence}%)", flush=True)
                        bot_msg = await message.channel.send(
                            f"🔍 That's **{guess.capitalize()}**! *(learning...)*",
                            reference=message,
                            mention_author=False,
                        )
                    else:
                        print("==> Unknown, waiting to learn…", flush=True)
                        bot_msg = await message.channel.send(
                            f"❓ Unknown Pokémon! *(I'll learn when caught/fled)*",
                            reference=message,
                            mention_author=False,
                        )

                last_spawn[channel_id] = (hash_str, bot_msg)
                return

            # ── CATCH MESSAGE ─────────────────────────────────────────────────
            if is_poketwo(message):
                catch_name = extract_catch_name(message)
                if catch_name and channel_id in last_spawn:
                    prev_hash_str, prev_bot_msg = last_spawn[channel_id]
                    async with aiohttp.ClientSession() as session:
                        await teach(prev_hash_str, catch_name, "catch", session)
                    try:
                        await prev_bot_msg.edit(content=f"🔍 That was **{catch_name.capitalize()}**!")
                    except Exception:
                        pass
                    last_spawn.pop(channel_id, None)
                return

            # ── !guess command — reply to a spawn message to force a guess ────
            if message.content.lower() == "!guess" and message.reference:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    await message.channel.send("❌ Couldn't find the referenced message.")
                    return

                image_url = get_spawn_image_url(ref_msg)
                if not image_url:
                    await message.channel.send("❌ No Pokémon image found in that message.")
                    return

                async with aiohttp.ClientSession() as session:
                    spawn_hash, hash_str = await get_image_hash(image_url, session)

                if spawn_hash is None:
                    await message.channel.send("❌ Couldn't load the image.")
                    return

                if hash_str in learned_cache:
                    name = learned_cache[hash_str]
                    await message.channel.send(f"🔍 That's **{name}**! *(from memory)*")
                else:
                    guess, confidence = guess_from_sprites(spawn_hash)
                    if guess:
                        await message.channel.send(f"🔍 My best guess: **{guess.capitalize()}** ({confidence}% confidence)")
                        # Save to last_spawn so !correct can fix it
                        last_spawn[channel_id] = (hash_str, await message.channel.fetch_message(message.id))
                    else:
                        await message.channel.send("🤔 No idea, sorry!")
                return

            # ── !correct command ───────────────────────────────────────────────
            if message.content.lower().startswith("!correct "):
                name = message.content[9:].strip()
                if name and channel_id in last_spawn:
                    prev_hash_str, prev_bot_msg = last_spawn[channel_id]
                    async with aiohttp.ClientSession() as session:
                        await teach(prev_hash_str, name, "manual", session)
                    try:
                        await prev_bot_msg.edit(content=f"🔍 That's **{name.capitalize()}**! *(corrected)*")
                    except Exception:
                        pass
                    await message.add_reaction("✅")
                else:
                    await message.add_reaction("❌")

        try:
            print(f"==> Attempt {attempt}: Connecting to Discord…", flush=True)
            await client.start(TOKEN)
            print("==> Disconnected. Reconnecting in 30 s…", flush=True)
            await asyncio.sleep(30)

        except discord.LoginFailure:
            print("FATAL: Invalid token.", flush=True)
            sys.exit(1)

        except Exception as e:
            print(f"==> Connection error: {e}", flush=True)
            print(f"==> Retrying in {delay} s…", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    print("==> Waiting 5 s…", flush=True)
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        await load_from_github(session)
        await build_sprite_db(session)
    await run_bot()

asyncio.run(main())
