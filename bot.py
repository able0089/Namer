import asyncio
import os
import sys
import io
import re
import math
import discord
import aiohttp
from PIL import Image
from motor.motor_asyncio import AsyncIOMotorClient

print("==> bot.py starting up", flush=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN not set!", flush=True)
    sys.exit(1)

MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    print("FATAL: MONGO_URI not set!", flush=True)
    sys.exit(1)

POKETWO_BOT_ID = 716390085896962058
_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}
HASH_SIZE = 16
MAX_POKEMON = 1025

print("==> Config loaded.", flush=True)
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

# MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = mongo_client["pokebot"]
collection = db["learned"]

# In-memory cache (so we don't query MongoDB on every spawn)
learned_cache: dict[str, str] = {}

# {channel_id: (hash_str, bot_message)}
last_spawn: dict[int, tuple[str, discord.Message]] = {}

# {name: phash} — HOME sprites for fallback
sprite_db: dict[str, list[int]] = {}


# ── MONGODB HELPERS ───────────────────────────────────────────────────────────

async def load_learned():
    global learned_cache
    async for doc in collection.find():
        learned_cache[doc["hash"]] = doc["name"]
    print(f"==> Loaded {len(learned_cache)} Pokémon from MongoDB.", flush=True)

async def teach(hash_str: str, name: str, source: str):
    name = name.strip().capitalize()
    if not hash_str or not name:
        return
    learned_cache[hash_str] = name
    await collection.update_one(
        {"hash": hash_str},
        {"$set": {"name": name}},
        upsert=True
    )
    print(f"==> Learned: {name} (via {source}) — DB now has {len(learned_cache)} Pokémon", flush=True)


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


# ── SPRITE DATABASE (fallback) ────────────────────────────────────────────────

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
        print(f"==> Failed to fetch list: {e}", flush=True)
        return

    print(f"==> Downloading HOME sprites…", flush=True)
    success = 0
    for entry in pokemon_list:
        name = entry["name"]
        dex_id = entry["url"].rstrip("/").split("/")[-1]
        url = f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/home/{dex_id}.png"
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
    if message.author.id != POKETWO_BOT_ID:
        return False
    for embed in message.embeds:
        title = (embed.title or "").lower()
        desc  = (embed.description or "").lower()
        if "wild" in title and "pokémon" in title:
            return True
        if "wild" in desc and "pokémon" in desc:
            return True
    return False

def extract_fled_name(message: discord.Message) -> str | None:
    for embed in message.embeds:
        match = re.search(r"Wild (.+?) fled", embed.title or "", re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None

def extract_catch_name(message: discord.Message) -> str | None:
    content = message.content or ""
    match = re.search(r"[Yy]ou caught (a|an) (?:level \d+ )?(.+?)!", content)
    if match:
        return match.group(2).strip()
    for embed in message.embeds:
        match = re.search(r"[Yy]ou caught (a|an) (?:level \d+ )?(.+?)!", embed.description or "")
        if match:
            return match.group(2).strip()
    return None

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
            print(f"==> Ready! MongoDB has {len(learned_cache)} Pokémon.", flush=True)

        @client.event
        async def on_message(message: discord.Message):
            channel_id = message.channel.id

            if WATCH_CHANNEL_IDS and channel_id not in WATCH_CHANNEL_IDS:
                return

            # ── SPAWN ─────────────────────────────────────────────────────────
            if is_spawn_message(message):
                print(f"==> Spawn in #{message.channel}", flush=True)

                # Learn from fled name in new spawn title
                fled_name = extract_fled_name(message)
                if fled_name and channel_id in last_spawn:
                    prev_hash_str, prev_bot_msg = last_spawn[channel_id]
                    await teach(prev_hash_str, fled_name, "fled")
                    try:
                        await prev_bot_msg.edit(content=f"🔍 That was **{fled_name.capitalize()}**!")
                    except Exception:
                        pass

                image_url = get_spawn_image_url(message)
                if not image_url:
                    return

                async with aiohttp.ClientSession() as session:
                    spawn_hash, hash_str = await get_image_hash(image_url, session)

                if spawn_hash is None:
                    return

                # Check MongoDB cache first
                if hash_str in learned_cache:
                    name = learned_cache[hash_str]
                    print(f"==> Known: {name}", flush=True)
                    bot_msg = await message.channel.send(
                        f"🔍 That's **{name}**!",
                        reference=message,
                        mention_author=False,
                    )
                else:
                    # Fallback sprite guess
                    guess, confidence = guess_from_sprites(spawn_hash)
                    if guess and confidence >= 55:
                        print(f"==> Guessing: {guess} ({confidence}%)", flush=True)
                        bot_msg = await message.channel.send(
                            f"🔍 That's **{guess.capitalize()}**! *(learning...)*",
                            reference=message,
                            mention_author=False,
                        )
                    else:
                        bot_msg = await message.channel.send(
                            f"❓ Unknown Pokémon! *(learning when caught/fled)*",
                            reference=message,
                            mention_author=False,
                        )

                last_spawn[channel_id] = (hash_str, bot_msg)
                return

            # ── CATCH MESSAGE ─────────────────────────────────────────────────
            if message.author.id == POKETWO_BOT_ID:
                catch_name = extract_catch_name(message)
                if catch_name and channel_id in last_spawn:
                    prev_hash_str, prev_bot_msg = last_spawn[channel_id]
                    await teach(prev_hash_str, catch_name, "catch")
                    try:
                        await prev_bot_msg.edit(content=f"🔍 That was **{catch_name.capitalize()}**!")
                    except Exception:
                        pass
                    last_spawn.pop(channel_id, None)
                return

            # ── MANUAL CORRECTION: !correct Name ─────────────────────────────
            if message.content.lower().startswith("!correct "):
                name = message.content[9:].strip()
                if name and channel_id in last_spawn:
                    prev_hash_str, prev_bot_msg = last_spawn[channel_id]
                    await teach(prev_hash_str, name, "manual")
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
    await load_learned()
    print("==> Waiting 5 s…", flush=True)
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        await build_sprite_db(session)
    await run_bot()

asyncio.run(main())
