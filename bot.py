import asyncio
import os
import sys
import base64
import discord
import aiohttp

print("==> bot.py starting up", flush=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("FATAL: DISCORD_TOKEN not set!", flush=True)
    sys.exit(1)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("FATAL: GEMINI_API_KEY not set!", flush=True)
    sys.exit(1)

POKETWO_BOT_ID = 716390085896962058
_raw = os.environ.get("WATCH_CHANNEL_IDS", "")
WATCH_CHANNEL_IDS: set[int] = {int(x) for x in _raw.split(",") if x.strip()}

print("==> Config loaded.", flush=True)
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent?key={key}"
)


# ── AI IMAGE IDENTIFICATION ───────────────────────────────────────────────────

async def identify_pokemon_from_image(image_url: str, session: aiohttp.ClientSession) -> str | None:
    # Download the spawn image
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

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": content_type,
                            "data": image_b64,
                        }
                    },
                    {
                        "text": (
                            "This is a Pokémon from the game. "
                            "Reply with ONLY the Pokémon's English name, nothing else. "
                            "No punctuation, no explanation, just the name."
                        )
                    },
                ]
            }
        ]
    }

    try:
        url = GEMINI_URL.format(key=GEMINI_API_KEY)
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                print(f"==> Gemini API error {resp.status}: {data}", flush=True)
                return None
            name = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"==> Gemini identified: {name}", flush=True)
            return name
    except Exception as e:
        print(f"==> Gemini request error: {e}", flush=True)
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
                print("==> No image found.", flush=True)
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
