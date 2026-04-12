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

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO    = "able0089/Namer"
DB_FILE_PATH   = "learned.json"
HASH_SIZE      = 16
MAX_POKEMON    = 1025
POKETWO_BOT_ID = 716390085896962058

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}

print("==> Config loaded.", flush=True)
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

# {pokemon_name: phash} — HOME sprite hashes, used for identification
sprite_db:     dict[str, list[int]] = {}
# {channel_id: (spawn_image_url, bot_message)}
last_spawn:    dict[int, tuple[str, discord.Message]] = {}
# learned pokemon names — just a set of confirmed names
learned_names: set[str] = set()
github_sha:    str | None = None


# ── GITHUB PERSISTENCE ────────────────────────────────────────────────────────

def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

async def load_from_github(session: aiohttp.ClientSession):
    global learned_names, github_sha
    if not GITHUB_TOKEN:
        print("==> No GITHUB_TOKEN.", flush=True)
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DB_FILE_PATH}"
    try:
        async with session.get(url, headers=gh_headers()) as resp:
            if resp.status == 404:
                print("==> learned.json not found — starting fresh.", flush=True)
                return
            data      = await resp.json()
            github_sha = data["sha"]
            content   = base64.b64decode(data["content"]).decode("utf-8")
            learned_names = set(json.loads(content))
            print(f"==> Loaded {len(learned_names)} learned Pokémon from GitHub.", flush=True)
    except Exception as e:
        print(f"==> GitHub load error: {e}", flush=True)

async def save_to_github(session: aiohttp.ClientSession):
    global github_sha
    if not GITHUB_TOKEN:
        return
    url         = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DB_FILE_PATH}"
    content_b64 = base64.b64encode(json.dumps(sorted(learned_names), indent=2).encode()).decode()
    payload: dict = {
        "message": f"bot: {len(learned_names)} pokemon learned",
        "content": content_b64,
    }
    if github_sha:
        payload["sha"] = github_sha
    try:
        async with session.put(url, headers=gh_headers(), json=payload) as resp:
            if resp.status in (200, 201):
                github_sha = (await resp.json())["content"]["sha"]
                print(f"==> Saved {len(learned_names)} names to GitHub.", flush=True)
            else:
                print(f"==> GitHub save failed: {await resp.text()}", flush=True)
    except Exception as e:
        print(f"==> GitHub save error: {e}", flush=True)


# ── ENGLISH NAME RESOLVER ─────────────────────────────────────────────────────

async def resolve_english_name(raw: str, session: aiohttp.ClientSession) -> str:
    slug = raw.strip().lower().replace(" ", "-")
    try:
        async with session.get(
            f"https://pokeapi.co/api/v2/pokemon/{slug}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                async with session.get(data["species"]["url"], timeout=aiohttp.ClientTimeout(total=5)) as sr:
                    if sr.status == 200:
                        for entry in (await sr.json())["names"]:
                            if entry["language"]["name"] == "en":
                                return entry["name"]
                return data["name"].capitalize()
    except Exception:
        pass
    # Fallback: search all species
    try:
        async with session.get(
            "https://pokeapi.co/api/v2/pokemon-species?limit=1025",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                for species in (await resp.json())["results"]:
                    async with session.get(species["url"], timeout=aiohttp.ClientTimeout(total=5)) as sr:
                        if sr.status != 200:
                            continue
                        sdata = await sr.json()
                        for entry in sdata["names"]:
                            if entry["name"].lower() == raw.strip().lower():
                                for en in sdata["names"]:
                                    if en["language"]["name"] == "en":
                                        return en["name"]
    except Exception:
        pass
    return raw.strip().capitalize()


# ── TEACH ─────────────────────────────────────────────────────────────────────

async def teach(raw_name: str, source: str, session: aiohttp.ClientSession) -> str:
    """Resolve to English, add to learned_names set, save to GitHub."""
    if not raw_name.strip():
        return raw_name
    english = await resolve_english_name(raw_name, session)
    if english.lower() != raw_name.strip().lower():
        print(f"==> Resolved '{raw_name}' → '{english}'", flush=True)
    if english in learned_names:
        return english
    learned_names.add(english)
    print(f"==> Learned: {english} (via {source}) — {len(learned_names)} total", flush=True)
    await save_to_github(session)
    return english


# ── IMAGE HASHING ─────────────────────────────────────────────────────────────

def phash(img: Image.Image) -> list[int]:
    img = img.convert("RGBA")
    bg  = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    img    = bg.convert("L").resize((HASH_SIZE, HASH_SIZE), Image.LANCZOS)
    pixels = list(img.getdata())
    avg    = sum(pixels) / len(pixels)
    return [1 if p > avg else 0 for p in pixels]

def hamming(h1: list[int], h2: list[int]) -> int:
    return sum(b1 != b2 for b1, b2 in zip(h1, h2))

async def fetch_image_hash(image_url: str, session: aiohttp.ClientSession) -> list[int] | None:
    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            return phash(Image.open(io.BytesIO(await resp.read())))
    except Exception as e:
        print(f"==> Image fetch error: {e}", flush=True)
        return None


# ── SPRITE DATABASE ───────────────────────────────────────────────────────────

async def build_sprite_db(session: aiohttp.ClientSession):
    global sprite_db
    print("==> Fetching Pokémon list…", flush=True)
    try:
        async with session.get(
            f"https://pokeapi.co/api/v2/pokemon?limit={MAX_POKEMON}",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            pokemon_list = (await resp.json())["results"]
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
                img            = Image.open(io.BytesIO(await resp.read()))
                sprite_db[name] = phash(img)
                success        += 1
        except Exception:
            pass
        if success % 100 == 0 and success > 0:
            print(f"==> {success} sprites loaded…", flush=True)
            await asyncio.sleep(0.3)
    print(f"==> Sprite DB ready! {success} Pokémon loaded.", flush=True)


def identify_from_sprites(spawn_hash: list[int], only_learned: bool = True) -> tuple[str | None, float]:
    """
    Compare spawn image hash against HOME sprites.
    If only_learned=True, only match against Pokémon we've confirmed before.
    Falls back to all sprites if no learned match is good enough.
    """
    candidates = {}
    if only_learned and learned_names:
        # First try only learned Pokémon — much more accurate
        for name in learned_names:
            key = name.lower().replace(" ", "-")
            if key in sprite_db:
                candidates[name] = sprite_db[key]

    if not candidates:
        # Fall back to all sprites
        candidates = {k.capitalize(): v for k, v in sprite_db.items()}

    if not candidates:
        return None, 0.0

    best_name, best_dist = None, math.inf
    for name, h in candidates.items():
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
    texts = [message.content or ""] + [e.description or "" for e in message.embeds]
    for text in texts:
        match = re.search(
            r"[Yy]ou caught (?:a|an) (?:[Ll]evel \d+ )?([A-Za-z][A-Za-z0-9\-]*(?:\s[A-Za-z][A-Za-z0-9\-]*)?)",
            text,
        )
        if match:
            name = match.group(1).strip()
            name = re.split(r"[:\s]*[♂♀✨\(]", name)[0].strip()
            if name:
                print(f"==> Catch detected: '{name}'", flush=True)
                return name
    return None


# ── DISCORD BOT ───────────────────────────────────────────────────────────────

async def run_bot():
    delay   = 10
    attempt = 0

    while True:
        attempt += 1
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"==> Logged in as {client.user} (ID: {client.user.id})", flush=True)
            print(f"==> Ready! Known Pokémon: {len(learned_names)}", flush=True)

        @client.event
        async def on_message(message: discord.Message):
            asyncio.create_task(handle_message(message))

        async def handle_message(message: discord.Message):
            channel_id = message.channel.id
            if WATCH_CHANNEL_IDS and channel_id not in WATCH_CHANNEL_IDS:
                return

            # ── SPAWN ─────────────────────────────────────────────────────────
            if is_spawn_message(message):
                print(f"==> Spawn in #{message.channel}", flush=True)

                # Learn previous Pokémon from fled text
                fled_name = extract_fled_name(message)
                if fled_name and channel_id in last_spawn:
                    _, prev_bot_msg = last_spawn[channel_id]
                    async with aiohttp.ClientSession() as session:
                        english = await teach(fled_name, "fled", session)
                    try:
                        await message.channel.send(f"✅ Learned: **{english}**!")
                    except Exception:
                        pass

                image_url = get_spawn_image_url(message)
                if not image_url:
                    return

                async with aiohttp.ClientSession() as session:
                    spawn_hash = await fetch_image_hash(image_url, session)

                if spawn_hash is None:
                    return

                # Always compare against HOME sprites — consistent every time
                name, confidence = identify_from_sprites(spawn_hash, only_learned=True)
                if name and confidence >= 60:
                    print(f"==> Identified: {name} ({confidence}%)", flush=True)
                    bot_msg = await message.channel.send(
                        f"🔍 That's **{name}**! *({confidence}% confidence)*",
                        reference=message,
                        mention_author=False,
                    )
                else:
                    # Try all sprites as fallback
                    name, confidence = identify_from_sprites(spawn_hash, only_learned=False)
                    if name and confidence >= 55:
                        print(f"==> Fallback guess: {name} ({confidence}%)", flush=True)
                        bot_msg = await message.channel.send(
                            f"🔍 That's **{name}**! *({confidence}% confidence, unconfirmed)*",
                            reference=message,
                            mention_author=False,
                        )
                    else:
                        bot_msg = await message.channel.send(
                            f"❓ Unknown! *(I'll learn when caught/fled)*",
                            reference=message,
                            mention_author=False,
                        )

                last_spawn[channel_id] = (image_url, bot_msg)
                return

            # ── CATCH MESSAGE ─────────────────────────────────────────────────
            if message.author.id == POKETWO_BOT_ID:
                catch_name = extract_catch_name(message)
                if catch_name and channel_id in last_spawn:
                    _, prev_bot_msg = last_spawn[channel_id]
                    async with aiohttp.ClientSession() as session:
                        english = await teach(catch_name, "catch", session)
                    try:
                        await message.channel.send(f"✅ Learned: **{english}**!")
                    except Exception:
                        pass
                    last_spawn.pop(channel_id, None)
                return

            # ── !guess ────────────────────────────────────────────────────────
            if message.content.lower() == "!guess" and message.reference:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    await message.channel.send("❌ Couldn't find that message.")
                    return
                image_url = get_spawn_image_url(ref_msg)
                if not image_url:
                    await message.channel.send("❌ No Pokémon image found.")
                    return
                async with aiohttp.ClientSession() as session:
                    spawn_hash = await fetch_image_hash(image_url, session)
                if not spawn_hash:
                    await message.channel.send("❌ Couldn't load image.")
                    return
                name, confidence = identify_from_sprites(spawn_hash, only_learned=False)
                if name:
                    bot_msg = await message.channel.send(
                        f"🔍 Best guess: **{name}** *({confidence}% confidence)*"
                    )
                    last_spawn[channel_id] = (image_url, bot_msg)
                else:
                    await message.channel.send("🤔 No idea!")
                return

            # ── !correct ──────────────────────────────────────────────────────
            if message.content.lower().startswith("!correct "):
                raw_name = message.content[9:].strip()
                if raw_name and channel_id in last_spawn:
                    _, prev_bot_msg = last_spawn[channel_id]
                    async with aiohttp.ClientSession() as session:
                        english = await teach(raw_name, "manual", session)
                    try:
                        await message.channel.send(f"✅ Learned: **{english}**! *(corrected)*")
                    except Exception:
                        pass
                    await message.add_reaction("✅")
                else:
                    await message.add_reaction("❌")

        try:
            print(f"==> Attempt {attempt}: Connecting…", flush=True)
            await client.start(TOKEN)
            print("==> Disconnected. Reconnecting in 30s…", flush=True)
            await asyncio.sleep(30)
        except discord.LoginFailure:
            print("FATAL: Invalid token.", flush=True)
            sys.exit(1)
        except Exception as e:
            print(f"==> Error: {e}", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


# ── KEEP-ALIVE WEB SERVER (required for Render) ──────────────────────────────

from aiohttp import web as aiohttp_web

async def handle_health(request):
    return aiohttp_web.Response(text="Bot is running ✅")

async def start_webserver():
    PORT = int(os.environ.get("PORT", 8080))
    app  = aiohttp_web.Application()
    app.router.add_get("/", handle_health)
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    await aiohttp_web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"==> Web server on port {PORT}", flush=True)


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    await start_webserver()
    print("==> Waiting 5s…", flush=True)
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        await load_from_github(session)
        await build_sprite_db(session)
    await run_bot()

asyncio.run(main())
