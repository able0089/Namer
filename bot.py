import discord
import re
import os
import aiohttp
from aiohttp import web

# ── CONFIG (set these as Environment Variables in Render) ─────────────────────
TOKEN = os.environ["DISCORD_TOKEN"]          # Required
POKETWO_BOT_ID = 716390085896962058          # Official Poketwo bot ID
PORT = int(os.environ.get("PORT", 8080))     # Render injects PORT automatically
# Optional: comma-separated channel IDs e.g. "123456,789012"
_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ── KEEP-ALIVE WEB SERVER ─────────────────────────────────────────────────────
# Render requires a service to bind to a port. This tiny HTTP server satisfies
# that requirement and also acts as a health-check endpoint.

async def handle_health(request):
    return web.Response(text="Bot is running ✅")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Keep-alive server listening on port {PORT}")


# ── POKEMON HELPERS ───────────────────────────────────────────────────────────

def extract_pokemon_name_from_url(url: str) -> str | None:
    """Pull dex number or name slug from Poketwo sprite URLs."""
    # Dex number: .../132.png
    match = re.search(r"/(\d+)\.(?:png|gif|jpg|webp)(?:\?|$)", url)
    if match:
        return match.group(1)
    # Name slug: .../bulbasaur.png
    match = re.search(r"/([a-z][a-z0-9\-]+)\.(?:png|gif|jpg|webp)(?:\?|$)", url)
    if match:
        return match.group(1)
    return None


async def resolve_pokemon(identifier: str, session: aiohttp.ClientSession) -> str:
    """Resolve a dex number or slug to a display name via PokéAPI."""
    url = f"https://pokeapi.co/api/v2/pokemon/{identifier.lower()}"
    async with session.get(url) as resp:
        if resp.status == 200:
            data = await resp.json()
            return data["name"].capitalize()
    return identifier.capitalize()


def is_spawn_message(message: discord.Message) -> bool:
    if message.author.id != POKETWO_BOT_ID:
        return False
    for embed in message.embeds:
        title = (embed.title or "").lower()
        desc  = (embed.description or "").lower()
        if "wild pokémon" in title or "wild pokémon" in desc:
            return True
    return False


# ── DISCORD EVENTS ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    await start_webserver()
    print("Watching for Poketwo spawns…")


@client.event
async def on_message(message: discord.Message):
    if WATCH_CHANNEL_IDS and message.channel.id not in WATCH_CHANNEL_IDS:
        return
    if not is_spawn_message(message):
        return

    image_urls: list[str] = []
    for embed in message.embeds:
        if embed.image and embed.image.url:
            image_urls.append(embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            image_urls.append(embed.thumbnail.url)

    if not image_urls:
        return

    async with aiohttp.ClientSession() as session:
        for url in image_urls:
            identifier = extract_pokemon_name_from_url(url)
            if identifier:
                name = await resolve_pokemon(identifier, session)
                await message.channel.send(
                    f"🔍 That's **{name}**!",
                    reference=message,
                    mention_author=False,
                )
                return

    await message.channel.send(
        "🤔 I spotted a spawn but couldn't identify the Pokémon from the image URL.",
        reference=message,
        mention_author=False,
    )


import asyncio

async def start_bot():
    await asyncio.sleep(10)  # delay to avoid rate limit
    await client.start(TOKEN)

loop = asyncio.get_event_loop()
loop.run_until_complete(start_bot())
