import asyncio
import os
import sys
import re
import json
import base64
import random
import time

import discord
import aiohttp
from aiohttp import web as aiohttp_web

print("==> bot.py starting up", flush=True)

# ── CONFIG ─────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN not set!", flush=True)
    sys.exit(1)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")

GITHUB_REPO = "able0089/Namer"
DB_FILE_PATH = "learned.json"
POKETWO_BOT_ID = 716390085896962058

HF_API_URL = "https://api-inference.huggingface.co/models/imzynoxprince/pokemons-image-classifier-gen1-gen9"

_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS = {int(x) for x in _raw.split(",") if x.strip()}

# ── GLOBAL CONTROL ─────────────────────────────────────────────────────
http_session = None
last_action_time = 0
cooldown_seconds = 2.5
semaphore = asyncio.Semaphore(3)

intents = discord.Intents.default()
intents.message_content = True

# ── HELPERS ────────────────────────────────────────────────────────────

def get_spawn_image_url(message):
    for embed in message.embeds:
        if embed.image and embed.image.url:
            return embed.image.url
        if embed.thumbnail and embed.thumbnail.url:
            return embed.thumbnail.url
    return None

def is_spawn_message(message):
    if message.author.id != POKETWO_BOT_ID:
        return False
    for embed in message.embeds:
        text = (embed.title or "") + (embed.description or "")
        text = text.lower()
        if "wild" in text and "pokémon" in text:
            return True
    return False

# ── HF IDENTIFIER ──────────────────────────────────────────────────────

async def identify_pokemon(image_url):
    global http_session
    try:
        async with http_session.get(image_url) as resp:
            if resp.status != 200:
                return None, 0
            img = await resp.read()

        headers = {}
        if HF_TOKEN:
            headers["Authorization"] = f"Bearer {HF_TOKEN}"

        async with http_session.post(HF_API_URL, data=img, headers=headers) as resp:
            result = await resp.json()

        if isinstance(result, list) and result:
            top = result[0]
            name = top["label"].replace("-", " ").title()
            conf = round(top["score"] * 100, 1)
            print(f"==> {name} ({conf}%)", flush=True)
            return name, conf

    except Exception as e:
        print("HF error:", e)

    return None, 0

# ── BOT ────────────────────────────────────────────────────────────────

async def run_bot():
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"==> Logged in as {client.user}", flush=True)

    @client.event
    async def on_message(message):
        await handle_message(message)

    async def handle_message(message):
        global last_action_time

        async with semaphore:
            if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
                return

            if is_spawn_message(message):
                await asyncio.sleep(random.uniform(1.5, 3.0))

                if time.time() - last_action_time < cooldown_seconds:
                    return

                last_action_time = time.time()

                image_url = get_spawn_image_url(message)
                if not image_url:
                    return

                name, conf = await identify_pokemon(image_url)

                if name and conf >= 50:
                    await message.channel.send(
                        f"🔍 That's **{name}**! *({conf}%)*",
                        reference=message,
                        mention_author=False,
                    )
                else:
                    await message.channel.send(
                        "❓ Not sure!",
                        reference=message,
                        mention_author=False,
                    )

            if message.content.lower() == "!ping":
                await message.channel.send("🏓 Pong!")

    await client.start(TOKEN)

# ── WEB SERVER ─────────────────────────────────────────────────────────

async def start_web():
    app = aiohttp_web.Application()
    app.router.add_get("/", lambda r: aiohttp_web.Response(text="OK"))
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    await aiohttp_web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()

# ── MAIN ───────────────────────────────────────────────────────────────

async def main():
    global http_session
    http_session = aiohttp.ClientSession()

    await start_web()
    await asyncio.sleep(5)

    await run_bot()

asyncio.run(main())
