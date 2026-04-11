import asyncio
import os
import sys
import io
import math
import discord
import aiohttp
from PIL import Image

print("==> bot.py starting up", flush=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN not set!", flush=True)
    sys.exit(1)

POKETWO_BOT_ID = 716390085896962058
_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}
HASH_SIZE = 16          # larger = more accurate but slower
MAX_POKEMON = 1025      # up to Gen 9
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

# Global sprite database: {name: hash_bits}
sprite_db: dict[str, list[int]] = {}


# ── IMAGE HASHING (perceptual hash / pHash) ───────────────────────────────────

def phash(img: Image.Image, size: int = HASH_SIZE) -> list[int]:
    """
    Perceptual hash - converts image to a fingerprint that stays similar
    even if brightness/contrast/scale changes slightly.
    """
    # Convert to greyscale, resize to size x size
    img = img.convert("L").resize((size, size), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    return [1 if p > avg else 0 for p in pixels]


def hamming_distance(h1: list[int], h2: list[int]) -> int:
    """Count differing bits between two hashes. Lower = more similar."""
    return sum(b1 != b2 for b1, b2 in zip(h1, h2))


# ── SPRITE DATABASE BUILDER ───────────────────────────────────────────────────

async def build_sprite_db(session: aiohttp.ClientSession):
    """Download all Pokémon sprites from PokéAPI and hash them."""
    global sprite_db
    print(f"==> Building sprite database (this takes ~2 min on first run)…", flush=True)

    # Get full list of pokemon
    async with session.get(
        f"https://pokeapi.co/api/v2/pokemon?limit={MAX_POKEMON}",
        timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        data = await resp.json()
        pokemon_list = data["results"]  # [{name, url}, ...]

    print(f"==> Found {len(pokemon_list)} Pokémon. Downloading sprites…", flush=True)

    success = 0
    for entry in pokemon_list:
        name = entry["name"]
        dex_id = entry["url"].rstrip("/").split("/")[-1]

        sprite_url = f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/{dex_id}.png"

        try:
            async with session.get(
                sprite_url,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    continue
                img_bytes = await resp.read()
                img = Image.open(io.BytesIO(img_bytes))
                sprite_db[name] = phash(img)
                success += 1
        except Exception:
            pass  # skip failures silently

        # Small delay to avoid hammering GitHub
        if success % 100 == 0:
            print(f"==> Loaded {success} sprites…", flush=True)
            await asyncio.sleep(0.5)

    print(f"==> Sprite database ready! {success} Pokémon loaded.", flush=True)


# ── POKEMON IDENTIFICATION ────────────────────────────────────────────────────

async def identify_pokemon(image_url: str, session: aiohttp.ClientSession) -> str | None:
    if not sprite_db:
        print("==> Sprite DB not ready yet.", flush=True)
        return None

    # Download spawn image
    try:
        async with session.get(
            image_url,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            img_bytes = await resp.read()
            img = Image.open(io.BytesIO(img_bytes))
    except Exception as e:
        print(f"==> Failed to download spawn image: {e}", flush=True)
        return None

    spawn_hash = phash(img)

    # Find closest match
    best_name = None
    best_dist = math.inf
    for name, sprite_hash in sprite_db.items():
        dist = hamming_distance(spawn_hash, sprite_hash)
        if dist < best_dist:
            best_dist = dist
            best_name = name

    max_bits = HASH_SIZE * HASH_SIZE
    confidence = round((1 - best_dist / max_bits) * 100, 1)
    print(f"==> Best match: {best_name} (confidence: {confidence}%, distance: {best_dist})", flush=True)

    # Reject if confidence too low
    if confidence < 60:
        print("==> Confidence too low, skipping.", flush=True)
        return None

    return best_name.capitalize()


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
            print("==> Watching for Poketwo spawns…", flush=True)

        @client.event
        async def on_message(message: discord.Message):
            if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
                return
            if not is_spawn_message(message):
                return

            print(f"==> Spawn detected in #{message.channel}", flush=True)
            image_url = get_spawn_image_url(message)
            if not image_url:
                return

            async with aiohttp.ClientSession() as session:
                name = await identify_pokemon(image_url, session)

            if name:
                await message.channel.send(
                    f"🔍 That's **{name}**!",
                    reference=message,
                    mention_author=False,
                )
            else:
                await message.channel.send(
                    "🤔 I spotted a spawn but couldn't identify the Pokémon.",
                    reference=message,
                    mention_author=False,
                )

        try:
            print(f"==> Attempt {attempt}: Connecting to Discord…", flush=True)
            await client.start(TOKEN)
            print("==> Bot disconnected. Reconnecting in 30 s…", flush=True)
            await asyncio.sleep(30)

        except discord.LoginFailure:
            print("FATAL: Invalid token.", flush=True)
            sys.exit(1)

        except Exception as e:
            print(f"==> Connection failed (attempt {attempt}): {e}", flush=True)
            print(f"==> Retrying in {delay} s…", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    print("==> Waiting 5 s before Discord login…", flush=True)
    await asyncio.sleep(5)

    # Build sprite DB and start bot concurrently
    async with aiohttp.ClientSession() as session:
        await build_sprite_db(session)

    await run_bot()

asyncio.run(main())
