import asyncio
import re
import os
import sys
import base64
import discord
import aiohttp

print("==> bot.py starting up", flush=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN environment variable is not set!", flush=True)
    sys.exit(1)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("FATAL: ANTHROPIC_API_KEY environment variable is not set!", flush=True)
    sys.exit(1)

POKETWO_BOT_ID = 716390085896962058
_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}

print("==> Config loaded.", flush=True)
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True


# ── AI IMAGE IDENTIFICATION ───────────────────────────────────────────────────

async def identify_pokemon_from_image(image_url: str, session: aiohttp.ClientSession) -> str | None:
    """Download the spawn image and ask Claude to identify the Pokémon."""

    # Download image
    try:
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                print(f"==> Failed to download image: {resp.status}", flush=True)
                return None
            image_bytes = await resp.read()
            content_type = resp.content_type or "image/jpeg"
    except Exception as e:
        print(f"==> Image download error: {e}", flush=True)
        return None

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    # Ask Claude to identify it
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": content_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a Pokémon from the game. "
                            "Reply with ONLY the Pokémon's English name, nothing else. "
                            "No punctuation, no explanation, just the name."
                        ),
                    },
                ],
            }
        ],
    }

    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"==> Claude API error {resp.status}: {text}", flush=True)
                return None
            data = await resp.json()
            name = data["content"][0]["text"].strip()
            print(f"==> Claude identified: {name}", flush=True)
            return name
    except Exception as e:
        print(f"==> Claude API request error: {e}", flush=True)
        return None


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
                print("==> No image found in embed.", flush=True)
                return

            print(f"==> Identifying image: {image_url}", flush=True)

            async with aiohttp.ClientSession() as session:
                name = await identify_pokemon_from_image(image_url, session)

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
