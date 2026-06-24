import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import os
import json
import random
import string
from typing import Optional
from aiohttp import web

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN  = os.environ["DISCORD_TOKEN"]
PROXY_USERNAME = os.environ["PROXY_USERNAME"]
PROXY_PASSWORD = os.environ["PROXY_PASSWORD"]
PROXY_HOST     = os.environ.get("PROXY_HOST", "p.webshare.io")
PROXY_PORT     = int(os.environ.get("PROXY_PORT", "80"))
PORT           = int(os.environ.get("PORT", "8080"))
# ─────────────────────────────────────────────────────────────────────────────

PROXY_URL  = f"http://{PROXY_HOST}:{PROXY_PORT}"
PROXY_AUTH = aiohttp.BasicAuth(PROXY_USERNAME, PROXY_PASSWORD)

DEMAND_LABELS = {-1: "Unassigned", 0: "Terrible", 1: "Low", 2: "Normal", 3: "High", 4: "Amazing"}
TREND_LABELS  = {-1: "Unassigned", 0: "Lowering", 1: "Unstable", 2: "Stable", 3: "Raising", 4: "Fluctuating"}

# ─── IN-MEMORY STORES ────────────────────────────────────────────────────────
# verified_users: { discord_user_id (int) -> { roblox_id, roblox_name } }
verified_users: dict[int, dict] = {}

# pending_verifications: { discord_user_id (int) -> { roblox_id, roblox_name, phrase } }
pending_verifications: dict[int, dict] = {}

# trade_ad_tasks: { discord_user_id (int) -> asyncio.Task }
trade_ad_tasks: dict[int, asyncio.Task] = {}

# trade_ad_config: { discord_user_id (int) -> { offering, wanting, interval_mins, channel_id } }
trade_ad_config: dict[int, dict] = {}

# ─── DISCORD BOT ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ─── HEALTH SERVER ───────────────────────────────────────────────────────────
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
    print(f"🌐 Health server on port {PORT}")


# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────
async def fetch(
    session: aiohttp.ClientSession,
    url: str,
    use_proxy: bool = True,
    headers: dict = None,
) -> dict | None:
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        h.update(headers)

    # Try with proxy first, fall back to direct if proxy fails
    attempts = [(True, PROXY_URL), (False, None)] if use_proxy else [(False, None)]

    for with_proxy, proxy_url in attempts:
        kwargs = {"headers": h, "timeout": aiohttp.ClientTimeout(total=20)}
        if with_proxy and proxy_url:
            kwargs["proxy"] = proxy_url
            kwargs["proxy_auth"] = PROXY_AUTH
        try:
            async with session.get(url, **kwargs) as resp:
                label = "proxy" if with_proxy else "direct"
                if resp.status == 200:
                    print(f"[{label} ✓] {url}")
                    return await resp.json(content_type=None)
                else:
                    print(f"[{label} HTTP {resp.status}] {url}")
                    # Don't fallback on 4xx — those are real errors
                    if resp.status < 500:
                        return None
        except Exception as e:
            print(f"[{'proxy' if with_proxy else 'direct'} error] {url} → {e}")
            # Continue to next attempt
            continue

    return None


# ─── ROBLOX / ROLIMONS HELPERS ───────────────────────────────────────────────
async def roblox_user_by_name(session: aiohttp.ClientSession, username: str) -> dict | None:
    """Resolve Roblox username → { id, name, description }"""
    url = "https://users.roblox.com/v1/usernames/users"
    try:
        async with session.post(
            url,
            json={"usernames": [username], "excludeBannedUsers": False},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15),
            proxy=PROXY_URL,
            proxy_auth=PROXY_AUTH,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("data"):
                return data["data"][0]
            return None
    except Exception as e:
        print(f"[roblox_user_by_name] {e}")
        return None


async def roblox_user_profile(session: aiohttp.ClientSession, user_id: int) -> dict | None:
    return await fetch(session, f"https://users.roblox.com/v1/users/{user_id}")


async def rolimons_player_info(session: aiohttp.ClientSession, user_id: int) -> dict | None:
    return await fetch(session, f"https://www.rolimons.com/api/playerinfo/{user_id}")


async def rolimons_verify_phrase(session: aiohttp.ClientSession, user_id: int) -> str | None:
    """
    Fetch the verification phrase Rolimons wants pasted into the user's bio.
    Rolimons generates this at /api/verification/generate for a given user ID.
    """
    data = await fetch(session, f"https://www.rolimons.com/api/verification/generate/{user_id}")
    if not data:
        return None
    return data.get("phrase") or data.get("verification_phrase") or None


async def check_rolimons_verified(session: aiohttp.ClientSession, user_id: int, phrase: str) -> bool:
    """
    Ask Rolimons to check if the phrase appears in the user's Roblox bio.
    Uses Rolimons' verification check endpoint.
    """
    data = await fetch(session, f"https://www.rolimons.com/api/verification/check/{user_id}")
    if not data:
        return False
    return data.get("verified", False) or data.get("success", False)


async def search_rolimons_item(session: aiohttp.ClientSession, item_name: str) -> dict | None:
    data = await fetch(session, "https://www.rolimons.com/itemapi/itemdetails")
    if not data or "items" not in data:
        return None
    needle = item_name.lower().strip()
    best, best_score = None, 0
    for item_id, d in data["items"].items():
        name = d[0]
        name_lower = name.lower()
        if needle == name_lower:           score = 100
        elif name_lower.startswith(needle): score = 90
        elif needle in name_lower:          score = 80
        elif all(w in name_lower for w in needle.split()): score = 60
        else:                               score = 0
        if score > best_score:
            best_score = score
            best = {"id": int(item_id), "name": name, "value": d[2],
                    "demand": d[4], "trend": d[5], "rap": d[7]}
    return best if best_score > 0 else None


async def get_item_owners(session: aiohttp.ClientSession, asset_id: int, limit: int = 5) -> list[dict]:
    url = f"https://inventory.roblox.com/v2/assets/{asset_id}/owners?limit={limit}&sortOrder=Asc"
    data = await fetch(session, url)
    return data.get("data", []) if data else []


async def get_rolimons_player_trade_ads(session: aiohttp.ClientSession, user_id: int) -> int | None:
    data = await rolimons_player_info(session, user_id)
    if not data:
        return None
    return (data.get("trade_ad_count")
            or data.get("tradeAdCount")
            or (data.get("player_data") or {}).get("trade_ad_count")
            or None)


# ─── TRADE AD RENDER ─────────────────────────────────────────────────────────
def build_trade_ad_embed(
    roblox_name: str,
    roblox_id: int,
    offering: str,
    wanting: str,
    interval_mins: int,
    is_preview: bool = False,
) -> discord.Embed:
    title = "📋 Trade Ad Preview" if is_preview else "📋 Trade Ad Posted"
    embed = discord.Embed(title=title, color=0x00C8FF)
    embed.set_author(
        name=roblox_name,
        url=f"https://www.rolimons.com/player/{roblox_id}",
        icon_url=f"https://www.roblox.com/headshot-thumbnail/image?userId={roblox_id}&width=150&height=150&format=png",
    )
    embed.add_field(name="✅ Offering", value=offering or "Not set", inline=False)
    embed.add_field(name="🔍 Wanting", value=wanting or "Not set", inline=False)
    embed.add_field(name="🔁 Interval", value=f"Every {interval_mins} min", inline=True)
    embed.add_field(
        name="🔗 Profile",
        value=f"[Rolimons](https://www.rolimons.com/player/{roblox_id})",
        inline=True,
    )
    embed.set_footer(text="RoHelper • Auto Trade Ads")
    return embed


# ─── AUTO-POSTER LOOP ────────────────────────────────────────────────────────
async def trade_ad_loop(discord_user_id: int, channel: discord.TextChannel):
    cfg = trade_ad_config[discord_user_id]
    user_data = verified_users[discord_user_id]
    while True:
        embed = build_trade_ad_embed(
            roblox_name=user_data["roblox_name"],
            roblox_id=user_data["roblox_id"],
            offering=cfg["offering"],
            wanting=cfg["wanting"],
            interval_mins=cfg["interval_mins"],
        )
        await channel.send(embed=embed)
        await asyncio.sleep(cfg["interval_mins"] * 60)


# ─── COMMANDS HELP EMBED ─────────────────────────────────────────────────────
def commands_embed() -> discord.Embed:
    embed = discord.Embed(
        title="✅ Verified! Here's what I can do",
        color=0x57F287,
    )
    embed.add_field(
        name="/scrape",
        value="Look up owners of any Roblox limited via Rolimons\n`item` · `owners` · `trade_ads`",
        inline=False,
    )
    embed.add_field(
        name="/tradead setup",
        value="Set what you're offering, wanting, and how often to post",
        inline=False,
    )
    embed.add_field(
        name="/tradead preview",
        value="See a render of your trade ad before it goes live",
        inline=False,
    )
    embed.add_field(
        name="/tradead start",
        value="Begin auto-posting your trade ad in this channel",
        inline=False,
    )
    embed.add_field(
        name="/tradead stop",
        value="Pause the auto-poster",
        inline=False,
    )
    embed.add_field(
        name="/tradead status",
        value="Check if the auto-poster is running",
        inline=False,
    )
    embed.set_footer(text="RoHelper • Rolimons verified")
    return embed


# ─── VERIFY BUTTON VIEW ──────────────────────────────────────────────────────
class VerifyButton(discord.ui.View):
    def __init__(self, discord_user_id: int):
        super().__init__(timeout=300)  # 5 min to verify
        self.discord_user_id = discord_user_id

    @discord.ui.button(label="✅ I've pasted it — Verify me!", style=discord.ButtonStyle.success)
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_user_id:
            await interaction.response.send_message("❌ This isn't your verification.", ephemeral=True)
            return

        pending = pending_verifications.get(self.discord_user_id)
        if not pending:
            await interaction.response.send_message("❌ No pending verification found. Run `/verify` again.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        async with aiohttp.ClientSession() as session:
            verified = await check_rolimons_verified(session, pending["roblox_id"], pending["phrase"])

        if verified:
            verified_users[self.discord_user_id] = {
                "roblox_id":   pending["roblox_id"],
                "roblox_name": pending["roblox_name"],
            }
            del pending_verifications[self.discord_user_id]
            self.verify_btn.disabled = True
            await interaction.followup.send(
                content=f"🎉 **{pending['roblox_name']}** is now verified!",
                embed=commands_embed(),
            )
        else:
            await interaction.followup.send(
                "❌ Couldn't verify yet. Make sure the phrase is **saved** in your Roblox bio, then try again.",
                ephemeral=True,
            )


# ─── /verify ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="verify", description="Link your Roblox account via Rolimons verification")
@app_commands.describe(roblox_username="Your Roblox username")
async def verify(interaction: discord.Interaction, roblox_username: str):
    await interaction.response.defer(thinking=True, ephemeral=True)

    async with aiohttp.ClientSession() as session:
        # 1. Resolve username → user ID
        user = await roblox_user_by_name(session, roblox_username)
        if not user:
            await interaction.followup.send(f"❌ Couldn't find Roblox user **{roblox_username}**.", ephemeral=True)
            return

        roblox_id   = user["id"]
        roblox_name = user["name"]

        # 2. Get verification phrase from Rolimons
        phrase = await rolimons_verify_phrase(session, roblox_id)

        # Fallback: Rolimons may not have a generate endpoint publicly;
        # in that case we guide the user to the Rolimons site manually.
        if not phrase:
            await interaction.followup.send(
                f"⚠️ Couldn't auto-fetch the phrase from Rolimons.\n"
                f"Go to **https://www.rolimons.com/verifyme** manually, copy the phrase, "
                f"paste it in your Roblox bio, then run `/verify` again.",
                ephemeral=True,
            )
            return

    # 3. Store pending
    pending_verifications[interaction.user.id] = {
        "roblox_id":   roblox_id,
        "roblox_name": roblox_name,
        "phrase":      phrase,
    }

    embed = discord.Embed(
        title="🔐 Verify your Roblox account",
        description=(
            f"**Account found:** {roblox_name} (`{roblox_id}`)\n\n"
            f"**Step 1 —** Copy this phrase:\n"
            f"```{phrase}```\n"
            f"**Step 2 —** Go to your [Roblox profile](https://www.roblox.com/users/{roblox_id}/profile) "
            f"→ Edit → paste it into your **About / Bio** section and save.\n\n"
            f"**Step 3 —** Click the button below."
        ),
        color=0xFEE75C,
    )
    embed.set_footer(text="You have 5 minutes to complete verification.")

    await interaction.followup.send(embed=embed, view=VerifyButton(interaction.user.id), ephemeral=True)


# ─── /scrape ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="scrape", description="Look up a Roblox limited item's owners via Rolimons")
@app_commands.describe(
    item      = "Name of the limited (e.g. 'Valkyrie Helm')",
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
        item_data = await search_rolimons_item(session, item)
        if not item_data:
            await interaction.followup.send(f"❌ Couldn't find **{item}** on Rolimons.\n• Check spelling (e.g. `Valkyrie Helm`, `Clockwork Headphones`)\n• The item must be a **limited** tracked by Rolimons\n• If spelling looks right, Rolimons may be temporarily down")
            return

        asset_id   = item_data["id"]
        item_name  = item_data["name"]
        item_value = item_data["value"]
        rap        = item_data["rap"]
        demand_str = DEMAND_LABELS.get(item_data["demand"], "?")
        trend_str  = TREND_LABELS.get(item_data["trend"], "?")

        owner_list = await get_item_owners(session, asset_id, limit=owners)
        if not owner_list:
            await interaction.followup.send(
                f"⚠️ Found **{item_name}** (`{asset_id}`) on Rolimons but couldn't fetch owners.\n"
                "The item may be non-transferable or Roblox is rate-limiting."
            )
            return

        embed = discord.Embed(
            title=f"🔍 {item_name}",
            url=f"https://www.rolimons.com/item/{asset_id}",
            color=0x5865F2,
        )
        embed.add_field(name="Item ID", value=f"`{asset_id}`", inline=True)
        embed.add_field(name="Value",   value=f"R${item_value:,}" if item_value > 0 else "Untracked", inline=True)
        embed.add_field(name="RAP",     value=f"R${rap:,}" if rap > 0 else "N/A", inline=True)
        embed.add_field(name="Demand",  value=demand_str, inline=True)
        embed.add_field(name="Trend",   value=trend_str,  inline=True)
        embed.add_field(name="\u200b",  value="\u200b",   inline=True)

        async def build_line(i: int, owner: dict) -> str:
            uid   = owner.get("id") or (owner.get("owner") or {}).get("id")
            uname = owner.get("name") or (owner.get("owner") or {}).get("name") or "Unknown"
            line  = f"**{i}.** [{uname}](https://www.roblox.com/users/{uid}/profile) — [Rolimons](https://www.rolimons.com/player/{uid})"
            if trade_ads and uid:
                count = await get_rolimons_player_trade_ads(session, uid)
                line += f" • 📋 {count} trade ad{'s' if count != 1 else ''}" if count is not None else " • 📋 N/A"
            return line

        lines = await asyncio.gather(*[build_line(i, o) for i, o in enumerate(owner_list, 1)])
        embed.add_field(
            name=f"👥 Top {len(lines)} Owner{'s' if len(lines) != 1 else ''}",
            value="\n".join(lines) or "None found",
            inline=False,
        )
        embed.set_footer(text="Rolimons × Roblox API • via Webshare proxy")
        await interaction.followup.send(embed=embed)


# ─── /tradead ────────────────────────────────────────────────────────────────
tradead_group = app_commands.Group(name="tradead", description="Manage your auto trade ad poster")

@tradead_group.command(name="setup", description="Configure your trade ad (offering, wanting, interval)")
@app_commands.describe(
    offering      = "What you're offering (e.g. 'Valkyrie Helm + adds')",
    wanting       = "What you want (e.g. 'Clockwork Headphones or better')",
    interval_mins = "How often to post in minutes (min 5, default 30)",
)
async def tradead_setup(
    interaction: discord.Interaction,
    offering: str,
    wanting: str,
    interval_mins: app_commands.Range[int, 5, 1440] = 30,
):
    if interaction.user.id not in verified_users:
        await interaction.response.send_message("❌ You need to `/verify` your Roblox account first.", ephemeral=True)
        return

    trade_ad_config[interaction.user.id] = {
        "offering":      offering,
        "wanting":       wanting,
        "interval_mins": interval_mins,
        "channel_id":    interaction.channel_id,
    }

    user_data = verified_users[interaction.user.id]
    embed = build_trade_ad_embed(
        roblox_name=user_data["roblox_name"],
        roblox_id=user_data["roblox_id"],
        offering=offering,
        wanting=wanting,
        interval_mins=interval_mins,
        is_preview=True,
    )
    await interaction.response.send_message(
        "✅ Trade ad configured! Here's a **preview** — run `/tradead start` to go live:",
        embed=embed,
        ephemeral=True,
    )


@tradead_group.command(name="preview", description="Preview your current trade ad")
async def tradead_preview(interaction: discord.Interaction):
    if interaction.user.id not in verified_users:
        await interaction.response.send_message("❌ You need to `/verify` first.", ephemeral=True)
        return
    cfg = trade_ad_config.get(interaction.user.id)
    if not cfg:
        await interaction.response.send_message("❌ No trade ad configured. Run `/tradead setup` first.", ephemeral=True)
        return

    user_data = verified_users[interaction.user.id]
    embed = build_trade_ad_embed(
        roblox_name=user_data["roblox_name"],
        roblox_id=user_data["roblox_id"],
        offering=cfg["offering"],
        wanting=cfg["wanting"],
        interval_mins=cfg["interval_mins"],
        is_preview=True,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tradead_group.command(name="start", description="Start auto-posting your trade ad in this channel")
async def tradead_start(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in verified_users:
        await interaction.response.send_message("❌ You need to `/verify` first.", ephemeral=True)
        return
    if uid not in trade_ad_config:
        await interaction.response.send_message("❌ Run `/tradead setup` first.", ephemeral=True)
        return
    if uid in trade_ad_tasks and not trade_ad_tasks[uid].done():
        await interaction.response.send_message("⚠️ Auto-poster is already running. Use `/tradead stop` first.", ephemeral=True)
        return

    trade_ad_config[uid]["channel_id"] = interaction.channel_id
    task = asyncio.create_task(trade_ad_loop(uid, interaction.channel))
    trade_ad_tasks[uid] = task

    cfg = trade_ad_config[uid]
    await interaction.response.send_message(
        f"✅ Auto-poster started! Posting every **{cfg['interval_mins']} min** in this channel.",
        ephemeral=True,
    )


@tradead_group.command(name="stop", description="Stop the auto trade ad poster")
async def tradead_stop(interaction: discord.Interaction):
    uid = interaction.user.id
    task = trade_ad_tasks.get(uid)
    if not task or task.done():
        await interaction.response.send_message("ℹ️ Auto-poster isn't running.", ephemeral=True)
        return
    task.cancel()
    del trade_ad_tasks[uid]
    await interaction.response.send_message("⏹️ Auto-poster stopped.", ephemeral=True)


@tradead_group.command(name="status", description="Check if your auto-poster is running")
async def tradead_status(interaction: discord.Interaction):
    uid = interaction.user.id
    cfg = trade_ad_config.get(uid)
    task = trade_ad_tasks.get(uid)
    running = task and not task.done()

    embed = discord.Embed(title="📊 Trade Ad Status", color=0x57F287 if running else 0xED4245)
    embed.add_field(name="Status", value="🟢 Running" if running else "🔴 Stopped", inline=True)
    if cfg:
        embed.add_field(name="Interval", value=f"{cfg['interval_mins']} min", inline=True)
        embed.add_field(name="Offering", value=cfg["offering"], inline=False)
        embed.add_field(name="Wanting",  value=cfg["wanting"],  inline=False)
    else:
        embed.add_field(name="Config", value="Not set — run `/tradead setup`", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(tradead_group)


# ─── ON READY ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ {bot.user} online — commands synced.")


# ─── ENTRYPOINT ──────────────────────────────────────────────────────────────
async def main():
    await start_health_server()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
