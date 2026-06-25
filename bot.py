import sys
print("[startup] Python version", flush=True)

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import os
from aiohttp import web

print("[startup] all imports done", flush=True)

# Config
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD")
PROXY_HOST = os.environ.get("PROXY_HOST", "p.webshare.io")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "80"))
PORT = int(os.environ.get("PORT", "8080"))

if not DISCORD_TOKEN:
    print("[ERROR] DISCORD_TOKEN not set!", flush=True)
    sys.exit(1)

print("[startup] env vars ok", flush=True)

PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
import base64
_creds = base64.b64encode(f"{PROXY_USERNAME}:{PROXY_PASSWORD}".encode()).decode()
PROXY_HEADERS = {"Proxy-Authorization": f"Basic {_creds}"}

DEMAND_LABELS = {-1: "Unassigned", 0: "Terrible", 1: "Low", 2: "Normal", 3: "High", 4: "Amazing"}
TREND_LABELS = {-1: "Unassigned", 0: "Lowering", 1: "Unstable", 2: "Stable", 3: "Raising", 4: "Fluctuating"}

# In-memory stores
verified_users = {}
trade_ad_tasks = {}
trade_ad_config = {}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

print("[startup] bot created", flush=True)

# Health server
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
    print(f"[health] server on port {PORT}", flush=True)

# HTTP helper
async def fetch(session, url, use_proxy=True):
    h = {"User-Agent": "Mozilla/5.0"}
    kwargs = {"headers": h, "timeout": aiohttp.ClientTimeout(total=20)}
    if use_proxy:
        kwargs["proxy"] = PROXY_URL
        kwargs["proxy_headers"] = PROXY_HEADERS
    try:
        async with session.get(url, **kwargs) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            return None
    except Exception as e:
        print(f"[fetch] {url}: {e}", flush=True)
        return None

# Roblox helpers
async def roblox_user_by_name(session, username):
    url = "https://users.roblox.com/v1/usernames/users"
    try:
        async with session.post(
            url,
            json={"usernames": [username], "excludeBannedUsers": False},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15),
            proxy=PROXY_URL,
            proxy_headers=PROXY_HEADERS,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("data", [None])[0] if data.get("data") else None
            return None
    except Exception as e:
        print(f"[roblox_user_by_name] {e}", flush=True)
        return None

async def search_rolimons_item(session, item_name):
    data = await fetch(session, "https://www.rolimons.com/itemapi/itemdetails")
    if not data or "items" not in data:
        return None
    needle = item_name.lower().strip()
    best, best_score = None, 0
    for item_id, d in data["items"].items():
        name = d[0]
        name_lower = name.lower()
        if needle == name_lower: score = 100
        elif name_lower.startswith(needle): score = 90
        elif needle in name_lower: score = 80
        else: score = 0
        if score > best_score:
            best_score = score
            best = {"id": int(item_id), "name": name, "value": d[2], "demand": d[4], "trend": d[5], "rap": d[7]}
    return best if best_score > 0 else None

async def get_item_owners(session, asset_id, limit=5):
    url = f"https://inventory.roblox.com/v2/assets/{asset_id}/owners?limit={limit}&sortOrder=Asc"
    data = await fetch(session, url)
    return data.get("data", []) if data else []

# Commands
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ {bot.user} online", flush=True)

@bot.tree.command(name="verify", description="Verify your Roblox account on Rolimons")
@app_commands.describe(roblox_username="Your Roblox username")
async def verify(interaction: discord.Interaction, roblox_username: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    async with aiohttp.ClientSession() as session:
        user = await roblox_user_by_name(session, roblox_username)
        if not user:
            await interaction.followup.send(f"❌ User not found: **{roblox_username}**", ephemeral=True)
            return
    
    roblox_id = user["id"]
    roblox_name = user["name"]
    
    verified_users[interaction.user.id] = {"roblox_id": roblox_id, "roblox_name": roblox_name}
    
    embed = discord.Embed(
        title="✅ Verified!",
        description=f"**{roblox_name}** (`{roblox_id}`) is now linked to your Discord account.",
        color=0x57F287
    )
    embed.add_field(
        name="Available commands",
        value="/scrape — Look up item owners\n/tradead setup — Configure auto trade ads",
        inline=False
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="scrape", description="Look up item owners on Rolimons")
@app_commands.describe(
    item="Item name (e.g. 'Valkyrie Helm')",
    owners="How many owners (1-10, default 5)"
)
async def scrape(interaction: discord.Interaction, item: str, owners: app_commands.Range[int, 1, 10] = 5):
    await interaction.response.defer(thinking=True)
    
    async with aiohttp.ClientSession() as session:
        item_data = await search_rolimons_item(session, item)
        if not item_data:
            await interaction.followup.send(f"❌ Item not found: **{item}**")
            return
        
        owner_list = await get_item_owners(session, item_data["id"], limit=owners)
        if not owner_list:
            await interaction.followup.send(f"⚠️ Found **{item_data['name']}** but no owners")
            return
        
        embed = discord.Embed(
            title=f"🔍 {item_data['name']}",
            url=f"https://www.rolimons.com/item/{item_data['id']}",
            color=0x5865F2
        )
        embed.add_field(name="Value", value=f"R${item_data['value']:,}", inline=True)
        embed.add_field(name="Demand", value=DEMAND_LABELS.get(item_data['demand'], "?"), inline=True)
        
        lines = []
        for i, owner in enumerate(owner_list, 1):
            uid = owner.get("id") or (owner.get("owner") or {}).get("id")
            uname = owner.get("name") or (owner.get("owner") or {}).get("name") or "Unknown"
            lines.append(f"**{i}.** [{uname}](https://www.rolimons.com/player/{uid})")
        
        embed.add_field(name=f"👥 Top {len(lines)} Owners", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed)

async def main():
    await start_health_server()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
