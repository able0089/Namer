import asyncio
import re
import os
import sys
import traceback
import discord
import aiohttp

print("==> bot.py starting up", flush=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN environment variable is not set!", flush=True)
    sys.exit(1)

POKETWO_BOT_ID = 716390085896962058
_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}

print("==> Config loaded.", flush=True)
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True


# ── POKEMON IDENTIFICATION ────────────────────────────────────────────────────

async def identify_pokemon(message: discord.Message, session: aiohttp.ClientSession) -> str | None:
    """
    Try multiple strategies to identify the Pokémon from a Poketwo spawn embed.
    Returns the Pokémon name or None if unidentified.
    """

    for embed in message.embeds:
        # ── Strategy 1: dex number from image URL ────────────────────────────
        image_url = (embed.image and embed.image.url) or (embed.thumbnail and embed.thumbnail.url) or ""
        print(f"==> Image URL: {image_url}", flush=True)

        match = re.search(r"/(\d+)\.(?:png|gif|jpg|webp)", image_url)
        if match:
            dex = match.group(1)
            print(f"==> Found dex number: {dex}", flush=True)
            name = await pokeapi_lookup(dex, session)
            if name:
                return name

        # ── Strategy 2: name slug from image URL ─────────────────────────────
        match = re.search(r"/([a-z][a-z0-9\-]+)\.(?:png|gif|jpg|webp)", image_url)
        if match:
            slug = match.group(1)
            # filter out common non-pokemon path segments
            if slug not in ("images", "sprites", "pokemon", "static", "assets"):
                print(f"==> Found slug: {slug}", flush=True)
                name = await pokeapi_lookup(slug, session)
                if name:
                    return name

        # ── Strategy 3: look for hidden Pokémon name in embed footer/fields ──
        footer_text = (embed.footer and embed.footer.text) or ""
        for field in embed.fields:
            text = f"{field.name} {field.value}"
            slug_match = re.search(r"\b([A-Z][a-z]+(?:[- ][A-Z][a-z]+)*)\b", text)
            if slug_match:
                candidate = slug_match.group(1).lower().replace(" ", "-")
                name = await pokeapi_lookup(candidate, session)
                if name:
                    return name

    return None


async def pokeapi_lookup(identifier: str, session: aiohttp.ClientSession) -> str | None:
    """Look up a Pokémon by dex number or name slug. Returns proper name or None."""
    url = f"https://pokeapi.co/api/v2/pokemon/{identifier.lower()}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["name"].capitalize()
    except Exception as e:
        print(f"==> PokéAPI error for '{identifier}': {e}", flush=True)
    return None


def is_spawn_message(message: discord.Message) -> bool:
    if message.author.id != POKETWO_BOT_ID:
        return False
    for embed in message.embeds:
        title = (embed.title or "").lower()
        desc  = (embed.description or "").lower()
        if "wild pokémon" in title or "wild pokémon" in desc:
            return True
        # also catch "A wild pokémon has appeared!" style
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

            # Log all embed image URLs to help debug
            for embed in message.embeds:
                print(f"==> Embed title: {embed.title}", flush=True)
                print(f"==> Embed image: {embed.image and embed.image.url}", flush=True)
                print(f"==> Embed thumbnail: {embed.thumbnail and embed.thumbnail.url}", flush=True)

            async with aiohttp.ClientSession() as session:
                name = await identify_pokemon(message, session)

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
    await run_bot()

asyncio.run(main())
