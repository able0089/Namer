import asyncio
import os
import sys
import io
import re
import json
import base64
import discord
import aiohttp
from PIL import Image
from aiohttp import web as aiohttp_web

print("==> bot.py starting up", flush=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN not set!", flush=True)
    sys.exit(1)

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO    = "able0089/Namer"
DB_FILE_PATH   = "learned.json"
POKETWO_BOT_ID = 716390085896962058
HF_MODEL       = "imzynoxprince/pokemons-image-classifier-gen1-gen9"

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}

print("==> Config loaded.", flush=True)
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

last_spawn:    dict[int, tuple[str, discord.Message]] = {}
learned_names: set[str] = set()
github_sha:    str | None = None
classifier    = None  # will be loaded at startup


# ── AI MODEL ──────────────────────────────────────────────────────────────────

def load_model():
    global classifier
    try:
        from transformers import pipeline
        print("==> Loading Pokémon classifier model…", flush=True)
        classifier = pipeline(
            "image-classification",
            model=HF_MODEL,
            device=-1,  # CPU
        )
        print("==> Model loaded! Ready to identify Pokémon.", flush=True)
    except Exception as e:
        print(f"==> Model load error: {e}", flush=True)
        classifier = None

async def identify_pokemon(image_url: str, session: aiohttp.ClientSession) -> tuple[str | None, float]:
    """Use the ViT model to identify a Pokémon from an image URL."""
    if classifier is None:
        return None, 0.0
    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None, 0.0
            img_bytes = await resp.read()
        img  = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, lambda: classifier(img, top_k=1))
        if results:
            name       = results[0]["label"].replace("-", " ").title()
            confidence = round(results[0]["score"] * 100, 1)
            return name, confidence
    except Exception as e:
        print(f"==> Identification error: {e}", flush=True)
    return None, 0.0


# ── GITHUB PERSISTENCE ────────────────────────────────────────────────────────

def gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

async def load_from_github(session: aiohttp.ClientSession):
    global learned_names, github_sha
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DB_FILE_PATH}"
    try:
        async with session.get(url, headers=gh_headers()) as resp:
            if resp.status == 404:
                print("==> learned.json not found — starting fresh.", flush=True)
                return
            data       = await resp.json()
            github_sha = data["sha"]
            content    = base64.b64decode(data["content"]).decode("utf-8")
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
    return raw.strip().capitalize()

async def teach(raw_name: str, source: str, session: aiohttp.ClientSession) -> str:
    if not raw_name.strip():
        return raw_name
    english = await resolve_english_name(raw_name, session)
    if english not in learned_names:
        learned_names.add(english)
        print(f"==> Learned: {english} (via {source}) — {len(learned_names)} total", flush=True)
        await save_to_github(session)
    return english


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
                print(f"==> Catch: '{name}'", flush=True)
                return name
    return None


# ── KEEP-ALIVE WEB SERVER ─────────────────────────────────────────────────────

async def handle_health(request):
    return aiohttp_web.Response(text="Bot is running ✅")

async def start_webserver():
    PORT   = int(os.environ.get("PORT", 8080))
    app    = aiohttp_web.Application()
    app.router.add_get("/", handle_health)
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    await aiohttp_web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"==> Web server on port {PORT}", flush=True)


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

                fled_name = extract_fled_name(message)
                if fled_name and channel_id in last_spawn:
                    _, prev_bot_msg = last_spawn[channel_id]
                    async with aiohttp.ClientSession() as session:
                        english = await teach(fled_name, "fled", session)
                    await message.channel.send(f"✅ Learned: **{english}**!")

                image_url = get_spawn_image_url(message)
                if not image_url:
                    return

                async with aiohttp.ClientSession() as session:
                    name, confidence = await identify_pokemon(image_url, session)

                if name and confidence >= 50:
                    print(f"==> Identified: {name} ({confidence}%)", flush=True)
                    bot_msg = await message.channel.send(
                        f"🔍 That's **{name}**! *({confidence}% confidence)*",
                        reference=message,
                        mention_author=False,
                    )
                else:
                    bot_msg = await message.channel.send(
                        f"❓ Unknown Pokémon! *(use !correct Name to teach me)*",
                        reference=message,
                        mention_author=False,
                    )

                last_spawn[channel_id] = (image_url, bot_msg)
                return

            # ── CATCH MESSAGE ─────────────────────────────────────────────────
            if message.author.id == POKETWO_BOT_ID:
                catch_name = extract_catch_name(message)
                if catch_name and channel_id in last_spawn:
                    async with aiohttp.ClientSession() as session:
                        english = await teach(catch_name, "catch", session)
                    await message.channel.send(f"✅ Learned: **{english}**!")
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
                    await message.channel.send("❌ No image found.")
                    return
                async with aiohttp.ClientSession() as session:
                    name, confidence = await identify_pokemon(image_url, session)
                if name:
                    bot_msg = await message.channel.send(
                        f"🔍 That's **{name}**! *({confidence}% confidence)*"
                    )
                    last_spawn[channel_id] = (image_url, bot_msg)
                else:
                    await message.channel.send("🤔 No idea!")
                return

            # ── !correct ──────────────────────────────────────────────────────
            if message.content.lower().startswith("!correct "):
                raw_name = message.content[9:].strip()
                if raw_name and channel_id in last_spawn:
                    async with aiohttp.ClientSession() as session:
                        english = await teach(raw_name, "manual", session)
                    await message.channel.send(f"✅ Learned: **{english}**! *(corrected)*")
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


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    await start_webserver()
    print("==> Waiting 5s…", flush=True)
    await asyncio.sleep(5)
    # Load model in thread so it doesn't block
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, load_model)
    async with aiohttp.ClientSession() as session:
        await load_from_github(session)
    await run_bot()

asyncio.run(main())
