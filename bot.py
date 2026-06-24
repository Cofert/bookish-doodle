import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import os
from typing import Optional
from aiohttp import web

# ─── CONFIG (set these as Railway environment variables) ──────────────────────
DISCORD_TOKEN  = os.environ["DISCORD_TOKEN"]
PROXY_USERNAME = os.environ["PROXY_USERNAME"]
PROXY_PASSWORD = os.environ["PROXY_PASSWORD"]
PROXY_HOST     = os.environ.get("PROXY_HOST", "p.webshare.io")
PROXY_PORT     = int(os.environ.get("PROXY_PORT", "80"))
PORT           = int(os.environ.get("PORT", "8080"))   # Railway injects $PORT
# ─────────────────────────────────────────────────────────────────────────────

PROXY_URL = f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"

DEMAND_LABELS = {-1: "Unassigned", 0: "Terrible", 1: "Low", 2: "Normal", 3: "High", 4: "Amazing"}
TREND_LABELS  = {-1: "Unassigned", 0: "Lowering", 1: "Unstable", 2: "Stable", 3: "Raising", 4: "Fluctuating"}

# ─── DISCORD BOT SETUP ───────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ─── HTTP HEALTH SERVER (Railway requires a bound port) ───────────────────────
async def health_handler(request):
    return web.Response(text="OK")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Health server listening on port {PORT}")


# ─── HELPERS ─────────────────────────────────────────────────────────────────
async def fetch(session: aiohttp.ClientSession, url: str, use_proxy: bool = True) -> dict | None:
    kwargs = {
        "headers": {"User-Agent": "Mozilla/5.0 (compatible; RolimonsBot/1.0)"},
        "timeout": aiohttp.ClientTimeout(total=15),
    }
    if use_proxy:
        kwargs["proxy"] = PROXY_URL
    try:
        async with session.get(url, **kwargs) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            print(f"[HTTP {resp.status}] {url}")
            return None
    except Exception as e:
        print(f"[fetch error] {url} → {e}")
        return None


async def search_rolimons_item(session: aiohttp.ClientSession, item_name: str) -> dict | None:
    """Fetch Rolimons item catalogue and fuzzy-match by name."""
    data = await fetch(session, "https://www.rolimons.com/itemapi/itemdetails")
    if not data or "items" not in data:
        return None

    needle = item_name.lower().strip()
    best, best_score = None, 0

    for item_id, d in data["items"].items():
        # d = [name, acronym, value, ?, demand, trend, ?, rap, ?, ?]
        name = d[0]
        name_lower = name.lower()

        if needle == name_lower:
            score = 100
        elif name_lower.startswith(needle):
            score = 90
        elif needle in name_lower:
            score = 80
        elif all(w in name_lower for w in needle.split()):
            score = 60
        else:
            score = 0

        if score > best_score:
            best_score = score
            best = {
                "id":     int(item_id),
                "name":   name,
                "value":  d[2],
                "demand": d[4],
                "trend":  d[5],
                "rap":    d[7],
            }

    return best if best_score > 0 else None


async def get_item_owners(session: aiohttp.ClientSession, asset_id: int, limit: int = 5) -> list[dict]:
    """Fetch top N owners of a Roblox limited from the Roblox Inventory API."""
    url = (
        f"https://inventory.roblox.com/v2/assets/{asset_id}/owners"
        f"?limit={limit}&sortOrder=Asc"
    )
    data = await fetch(session, url)
    if not data or "data" not in data:
        return []
    return data["data"]


async def get_rolimons_player_trade_ads(session: aiohttp.ClientSession, user_id: int) -> Optional[int]:
    """
    Pull trade-ad count from Rolimons player info endpoint.
    Returns the total trade ads the user has ever posted, or None if unavailable.
    """
    url = f"https://www.rolimons.com/api/playerinfo/{user_id}"
    data = await fetch(session, url)
    if not data:
        return None
    # Rolimons may return trade_ad_count directly, or inside a nested key
    return (
        data.get("trade_ad_count")
        or data.get("tradeAdCount")
        or (data.get("player_data") or {}).get("trade_ad_count")
        or None
    )


# ─── SLASH COMMAND ────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} — slash commands synced.")


@bot.tree.command(
    name="scrape",
    description="Look up a Roblox limited item's owners via Rolimons"
)
@app_commands.describe(
    item      = "Name of the limited item (e.g. 'Valkyrie Helm')",
    owners    = "How many owners to show (1–10, default 5)",
    trade_ads = "Include total trade ads each owner has created?",
)
async def scrape(
    interaction: discord.Interaction,
    item: str,
    owners: app_commands.Range[int, 1, 10] = 5,
    trade_ads: bool = False,
):
    await interaction.response.defer(thinking=True)

    async with aiohttp.ClientSession() as session:

        # 1️⃣  Resolve item on Rolimons
        item_data = await search_rolimons_item(session, item)
        if not item_data:
            await interaction.followup.send(
                f"❌ Couldn't find **{item}** on Rolimons. Check spelling and try again."
            )
            return

        asset_id   = item_data["id"]
        item_name  = item_data["name"]
        item_value = item_data["value"]
        rap        = item_data["rap"]
        demand_str = DEMAND_LABELS.get(item_data["demand"], "?")
        trend_str  = TREND_LABELS.get(item_data["trend"], "?")

        # 2️⃣  Fetch owners from Roblox
        owner_list = await get_item_owners(session, asset_id, limit=owners)
        if not owner_list:
            await interaction.followup.send(
                f"⚠️ Found **{item_name}** (`{asset_id}`) on Rolimons but couldn't fetch owners.\n"
                f"The item may be non-transferable or Roblox is rate-limiting the request."
            )
            return

        # 3️⃣  Build embed
        embed = discord.Embed(
            title=f"🔍 {item_name}",
            url=f"https://www.rolimons.com/item/{asset_id}",
            color=0x5865F2,
        )
        embed.add_field(name="Item ID", value=f"`{asset_id}`", inline=True)
        embed.add_field(
            name="Value",
            value=f"R${item_value:,}" if item_value > 0 else "Untracked",
            inline=True,
        )
        embed.add_field(
            name="RAP",
            value=f"R${rap:,}" if rap > 0 else "N/A",
            inline=True,
        )
        embed.add_field(name="Demand", value=demand_str, inline=True)
        embed.add_field(name="Trend",  value=trend_str,  inline=True)
        embed.add_field(name="\u200b", value="\u200b",   inline=True)

        # 4️⃣  Build owner lines (fetch trade ads concurrently if requested)
        async def build_owner_line(i: int, owner: dict) -> str:
            uid   = owner.get("id") or (owner.get("owner") or {}).get("id")
            uname = owner.get("name") or (owner.get("owner") or {}).get("name") or "Unknown"
            profile  = f"https://www.roblox.com/users/{uid}/profile"
            rolimons = f"https://www.rolimons.com/player/{uid}"

            line = f"**{i}.** [{uname}]({profile}) — [Rolimons]({rolimons})"

            if trade_ads and uid:
                count = await get_rolimons_player_trade_ads(session, uid)
                if count is not None:
                    line += f" • 📋 {count} trade ad{'s' if count != 1 else ''}"
                else:
                    line += " • 📋 Trade ads: N/A"

            return line

        tasks = [build_owner_line(i, o) for i, o in enumerate(owner_list, 1)]
        owner_lines = await asyncio.gather(*tasks)

        embed.add_field(
            name=f"👥 Top {len(owner_lines)} Owner{'s' if len(owner_lines) != 1 else ''}",
            value="\n".join(owner_lines) or "None found",
            inline=False,
        )
        embed.set_footer(text="Rolimons × Roblox API • via Webshare proxy")

        await interaction.followup.send(embed=embed)


# ─── ENTRYPOINT ───────────────────────────────────────────────────────────────
async def main():
    await start_health_server()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
