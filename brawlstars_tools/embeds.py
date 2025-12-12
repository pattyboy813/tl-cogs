# bstools/embeds.py
from typing import List, Dict, Optional, Tuple

import discord

from .constants import CDN_ICON_URL, CDN_BADGE_URL, get_brawler_emoji, BRAWLER_EMOJIS


def _progress_bar(current: int, maximum: int, length: int = 16) -> str:
    """Return a unicode progress bar like ▰▰▰▰▱▱▱."""
    if maximum <= 0:
        return "▱" * length
    ratio = max(0.0, min(current / maximum, 1.0))
    filled = int(ratio * length)
    empty = length - filled
    return "▰" * filled + "▱" * empty


# -------------------- SAVE / ACCOUNTS --------------------


def build_save_embed(user: discord.User, name: str, tag: str, idx: int, icon_id: int):
    bs_tag = tag.strip("#")
    embed = discord.Embed(
        title="✅ Account Linked",
        description=(
            f"**{name}** has been linked to your Discord account.\n"
            f"Saved into slot **#{idx}** – use `bs accounts` to view all."
        ),
        color=discord.Color.green(),
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    if icon_id:
        embed.set_thumbnail(url=CDN_ICON_URL.format(icon_id))
    embed.set_footer(text=f"Tag: #{bs_tag}")
    return embed


async def build_accounts_embed(get_player, user: discord.Member, tags: List[str]):
    embed = discord.Embed(
        title=f"🎮 {user.display_name}'s Linked Accounts",
        color=discord.Color.from_rgb(44, 130, 201),
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    if not tags:
        embed.description = (
            "⚠️ **No accounts saved.**\n\n"
            "Use `bs save #TAG` to link your Brawl Stars profile."
        )
        return embed

    lines: List[str] = []
    for i, tag in enumerate(tags, start=1):
        try:
            data: Optional[Dict] = await get_player(tag)
        except RuntimeError:
            data = None

        if not data:
            name = "Unknown (API Error)"
            trophies = 0
            brawler_count = 0
            solo = duo = trio = 0
        else:
            name = data.get("name", "Unknown")
            trophies = data.get("trophies", 0)
            brawlers: List[Dict] = data.get("brawlers", []) or []
            brawler_count = len(brawlers)
            solo = data.get("soloVictories", 0)
            duo = data.get("duoVictories", 0)
            trio = data.get("3vs3Victories", 0)

        is_main = " ⭐ **(Main)**" if i == 1 else ""
        lines.append(
            f"**{i}. {name}**{is_main}\n"
            f"🏆 {trophies:,} • 🔓 {brawler_count} brawlers\n"
            f"🥊 3v3: {trio:,} • 👤 Solo: {solo:,} • 👥 Duo: {duo:,}\n"
            f"`#{tag.strip('#')}`"
        )

    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Use `bs switch <num1> <num2>` to reorder accounts")
    return embed


# -------------------- PLAYER --------------------


def build_player_embed(bot_user: discord.User, player: Dict):
    name: str = player.get("name", "Unknown")
    tag: str = player.get("tag", "#??????")
    trophies: int = player.get("trophies", 0)
    highest: int = player.get("highestTrophies", 0)
    exp_level: int = player.get("expLevel", 0)
    exp_points: int = player.get("expPoints", 0)
    icon_id: Optional[int] = player.get("icon", {}).get("id")

    bs_tag = tag.strip("#")

    brawlers: List[Dict] = player.get("brawlers", []) or []
    brawler_count: int = len(brawlers)
    total_brawler_count: int = len(BRAWLER_EMOJIS)
    total_brawler_trophies: int = sum(b.get("trophies", 0) for b in brawlers) if brawlers else 0
    avg_brawler_trophies: float = total_brawler_trophies / brawler_count if brawler_count else 0

    top_brawlers = sorted(brawlers, key=lambda b: b.get("trophies", 0), reverse=True)[:3]

    solo = player.get("soloVictories", 0)
    duo = player.get("duoVictories", 0)
    trio = player.get("3vs3Victories", 0)
    total_wins = solo + duo + trio

    champ_qualified = player.get("isQualifiedFromChampionshipChallenge", False)
    rr_best = player.get("bestRoboRumbleTime")
    big_best = player.get("bestTimeAsBigBrawler")

    embed = discord.Embed(
        color=discord.Color.from_rgb(255, 180, 60),
        description=f"🆔 `#{bs_tag}` • ⭐ Level **{exp_level}** • 🏆 **{trophies:,}** trophies",
    )

    author_icon = CDN_ICON_URL.format(icon_id) if icon_id else None
    embed.set_author(
        name=f"{name}",
        icon_url=author_icon or discord.Embed.Empty,
        url=f"https://brawlstats.com/profile/{bs_tag}",
    )

    if icon_id:
        embed.set_thumbnail(url=CDN_ICON_URL.format(icon_id))

    max_ref = max(highest, trophies)
    bar = _progress_bar(trophies, max_ref or trophies or 1)

    trophies_value = (
        f"**Current:** {trophies:,}\n"
        f"**Best:** {highest:,}\n"
        f"{bar}\n"
        f"Avg per brawler: **{avg_brawler_trophies:,.0f}**"
    )
    embed.add_field(name="🏆 Trophies", value=trophies_value, inline=False)

    wins_value = (
        f"Total wins: **{total_wins:,}**\n"
        f"🥊 3v3: {trio:,}\n"
        f"👤 Solo: {solo:,}\n"
        f"👥 Duo: {duo:,}"
    )
    embed.add_field(name="🎮 Matches & Wins", value=wins_value, inline=True)

    brawler_value = (
        f"Unlocked: **{brawler_count}/{total_brawler_count}**\n"
        f"Total brawler trophies: **{total_brawler_trophies:,}**\n"
        f"Average trophies: **{avg_brawler_trophies:,.0f}**"
    )
    embed.add_field(name="🧬 Brawlers", value=brawler_value, inline=True)

    if top_brawlers:
        lines: List[str] = []
        for idx, b in enumerate(top_brawlers, start=1):
            b_name = b.get("name", "Unknown")
            b_trophies = b.get("trophies", 0)
            b_power = b.get("power", 0)
            b_rank = b.get("rank", 0)
            gadgets = len(b.get("gadgets", []) or [])
            sps = len(b.get("starPowers", []) or [])
            gears = len(b.get("gears", []) or [])
            emoji = get_brawler_emoji(b_name)
            lines.append(
                f"**{idx}. {emoji} {b_name.title()}** `R{b_rank}`\n"
                f"🏆 {b_trophies:,} • ⚡ P{b_power} • ✨ SP {sps} • 🎯 Gad {gadgets} • ⚙️ Gear {gears}"
            )

        embed.add_field(name="🥇 Top Brawlers", value="\n\n".join(lines), inline=False)

    comp_lines: List[str] = []
    comp_lines.append("🏆 Championship: " + ("✅ Qualified" if champ_qualified else "❌ Not qualified"))
    if rr_best:
        comp_lines.append(f"🤖 Robo Rumble best: `{rr_best}`")
    if big_best:
        comp_lines.append(f"🧱 Big Brawler best: `{big_best}`")
    if comp_lines:
        embed.add_field(name="🎯 Competitive", value="\n".join(comp_lines), inline=False)

    club: Optional[Dict] = player.get("club")
    if club:
        c_name = club.get("name", "Unknown")
        c_tag = club.get("tag", "")
        embed.add_field(
            name="🛡️ Club",
            value=f"**{c_name}**\n`{c_tag}`",
            inline=False,
        )
    else:
        embed.add_field(name="🛡️ Club", value="Not in a club", inline=False)

    footer_icon = bot_user.display_avatar.url if getattr(bot_user, "display_avatar", None) else None
    embed.set_footer(text="TLG Revamp 2025 • Player Overview", icon_url=footer_icon)

    return embed


# -------------------- CLUB --------------------


def build_club_embed(data: Dict):
    name = data.get("name", "Unknown Club")
    tag = data.get("tag", "#??????")
    trophies = data.get("trophies", 0)
    required = data.get("requiredTrophies", 0)
    desc = data.get("description") or "No description."
    badge_id = data.get("badgeId")
    club_type = data.get("type", "unknown").title()

    members: List[Dict] = data.get("members", []) or []
    max_members: int = data.get("maxMembers", 30)

    roles: Dict[str, List[Dict]] = {"president": [], "vicePresident": [], "senior": []}
    for m in members:
        r = m.get("role")
        if r in roles:
            roles[r].append(m)

    avg_trophies = (
        sum(m.get("trophies", 0) for m in members) / len(members)
        if members
        else 0
    )
    top_member: Optional[Dict] = max(members, key=lambda m: m.get("trophies", 0)) if members else None

    embed = discord.Embed(color=discord.Color.from_rgb(220, 53, 69))

    if badge_id:
        embed.set_thumbnail(url=CDN_BADGE_URL.format(badge_id))

    embed.set_author(name=f"{name} ({tag})")
    embed.description = f"*{desc}*"

    embed.add_field(name="🏆 Trophies", value=f"**{trophies:,}**", inline=True)
    embed.add_field(name="🚪 Required", value=f"{required:,}", inline=True)
    embed.add_field(
        name="👥 Members",
        value=f"**{len(members)}**/{max_members}\n⚙️ Type: **{club_type}**",
        inline=True,
    )

    if members:
        embed.add_field(
            name="📊 Avg Trophies/Member",
            value=f"{avg_trophies:,.0f}",
            inline=True,
        )

    pres = roles["president"][0] if roles["president"] else None
    pres_text = (
        f"👑 **{pres['name']}**\n🏆 {pres.get('trophies', 0):,}" if pres else "None"
    )

    embed.add_field(
        name="Leadership",
        value=(
            f"{pres_text}\n"
            f"🛡️ VPs: **{len(roles['vicePresident'])}**\n"
            f"🎖️ Seniors: **{len(roles['senior'])}**"
        ),
        inline=False,
    )

    if top_member:
        tm_name = top_member.get("name", "Unknown")
        tm_trophies = top_member.get("trophies", 0)
        embed.add_field(
            name="🥇 Top Member",
            value=f"**{tm_name}**\n🏆 {tm_trophies:,}",
            inline=False,
        )

    embed.set_footer(text="TLG Revamp 2025 • Club Statistics")
    return embed


# -------------------- BRAWLERS --------------------


def build_brawlers_embed(player: Dict):
    name = player.get("name", "Unknown")
    icon_id: Optional[int] = player.get("icon", {}).get("id")
    brawlers: List[Dict] = player.get("brawlers", []) or []

    if not brawlers:
        return discord.Embed(
            description="❌ No brawler data available.",
            color=discord.Color.red(),
        )

    sorted_brawlers = sorted(brawlers, key=lambda b: b.get("trophies", 0), reverse=True)
    top_15 = sorted_brawlers[:15]

    embed = discord.Embed(
        title=f"{name}'s Top Brawlers",
        color=discord.Color.from_rgb(155, 89, 182),
    )
    if icon_id:
        embed.set_thumbnail(url=CDN_ICON_URL.format(icon_id))

    lines: List[str] = []
    for b in top_15:
        b_name = b.get("name", "Unknown")
        b_trophies = b.get("trophies", 0)
        b_power = b.get("power", 0)
        b_rank = b.get("rank", 0)

        gadgets = len(b.get("gadgets", []) or [])
        star_powers = len(b.get("starPowers", []) or [])
        gears = len(b.get("gears", []) or [])

        emoji = get_brawler_emoji(b_name)
        lines.append(
            f"{emoji} **{b_name.title()}** `R{b_rank}`\n"
            f"🏆 {b_trophies} • ⚡ P{b_power}\n"
            f"✨ SP {star_powers} • 🎯 Gad {gadgets} • ⚙️ Gear {gears}"
        )

    columns: List[List[str]] = [[], [], []]
    for idx, line in enumerate(lines):
        columns[idx % 3].append(line)

    for col_idx, col_lines in enumerate(columns):
        if not col_lines:
            continue
        embed.add_field(
            name=f"Top Brawlers {col_idx + 1}",
            value="\n\n".join(col_lines),
            inline=True,
        )

    embed.set_footer(text=f"Showing Top {len(top_15)} Brawlers")
    return embed


# -------------------- ADMIN / OVERVIEW --------------------


def build_addclub_embed(name: str, tag: str, badge_id: int):
    embed = discord.Embed(
        title="🏰 Tracking Started",
        description=f"Successfully added **{name}** (`{tag}`) to the server club list.",
        color=discord.Color.green(),
    )
    if badge_id:
        embed.set_thumbnail(url=CDN_BADGE_URL.format(badge_id))
    embed.set_footer(text="Use 'bs admin clubs' to view all tracked clubs")
    return embed


def build_delclub_embed(name: str, tag: str):
    embed = discord.Embed(
        title="🗑️ Tracking Stopped",
        description=f"Removed **{name}** (`{tag}`) from the server club list.",
        color=discord.Color.dark_grey(),
    )
    return embed


def build_listclubs_embed(clubs: Dict[str, Dict]):
    embed = discord.Embed(
        title="📜 Tracked Clubs",
        color=discord.Color.from_rgb(52, 152, 219),
    )

    if not clubs:
        embed.description = "No clubs are currently being tracked."
        return embed

    list_text = ""
    for data in clubs.values():
        name = data.get("name", "Unknown")
        tag = data.get("tag", "#??????")
        list_text += f"**{name}** • `{tag}`\n"

    embed.description = list_text
    return embed


def build_refreshclubs_embed(updated: int, failed: int):
    embed = discord.Embed(
        description=(
            "🔄 **Refreshed Club Data**\n\n"
            f"Updated: `{updated}`\nFailed: `{failed}`"
        ),
        color=discord.Color.blue(),
    )
    return embed


def build_overview_embed(club_data: List[Tuple[str, str, Dict]]):
    total_clubs = len(club_data)
    total_trophies = sum(d.get("trophies", 0) for _, _, d in club_data)

    total_members = 0
    total_capacity = 0
    total_required = 0

    total_vp = 0
    total_senior = 0
    total_online = 0

    for _, _, data in club_data:
        members = data.get("members", []) or []
        max_members = data.get("maxMembers", 30)
        req = data.get("requiredTrophies", 0)

        total_members += len(members)
        total_capacity += max_members
        total_required += req

        for m in members:
            role = m.get("role")
            if role == "vicePresident":
                total_vp += 1
            elif role == "senior":
                total_senior += 1

            if m.get("isOnline"):
                total_online += 1

    if total_clubs > 0:
        avg_trophies = total_trophies / total_clubs
        avg_required = total_required / total_clubs
        avg_members = total_members / total_clubs
        avg_vp = total_vp / total_clubs
        avg_senior = total_senior / total_clubs
        avg_online = total_online / total_clubs
    else:
        avg_trophies = avg_required = avg_members = avg_vp = avg_senior = avg_online = 0

    embed = discord.Embed(
        title="Overview from Threat Level | Overview",
        color=discord.Color.from_rgb(52, 152, 219),
        description="Aggregated statistics for all tracked clubs.",
    )

    embed.add_field(name="🏰 Total Clubs", value=f"**{total_clubs}**", inline=True)
    embed.add_field(
        name="🏆 Total Trophies", value=f"**{total_trophies:,}**", inline=True
    )
    embed.add_field(
        name="🧑‍🤝‍🧑 Members",
        value=f"**{total_members}**/{total_capacity}",
        inline=True,
    )

    embed.add_field(
        name="📊 Average Trophies", value=f"{avg_trophies:,.0f}", inline=True
    )
    embed.add_field(
        name="📥 Average Required", value=f"{avg_required:,.0f}", inline=True
    )
    embed.add_field(
        name="🦺 Average Vice-Presidents", value=f"{avg_vp:.1f}", inline=True
    )

    embed.add_field(
        name="🎖️ Average Seniors", value=f"{avg_senior:.1f}", inline=True
    )
    embed.add_field(
        name="🟢 Average Online", value=f"{avg_online:.1f}", inline=True
    )
    embed.add_field(
        name="👥 Average Members", value=f"{avg_members:.1f}", inline=True
    )

    embed.set_footer(text="Updating every 10 minutes • Live data from Brawl Stars API")
    return embed


def build_clubs_stats_embed(club_data: List[Tuple[str, str, Dict]]):
    embed = discord.Embed(
        title="📋 Detailed Club Statistics",
        color=discord.Color.dark_grey(),
    )

    for name, tag, data in club_data:
        trophies = data.get("trophies", 0)
        req = data.get("requiredTrophies", 0)
        members = data.get("members", []) or []
        max_m = data.get("maxMembers", 30)

        member_count = len(members)
        avg_member_trophies = (
            sum(m.get("trophies", 0) for m in members) / member_count
            if member_count
            else 0
        )

        stats = (
            f"`{tag}`\n"
            f"🏆 **{trophies:,}** | 📥 Req: {req:,}\n"
            f"👥 **{member_count}/{max_m}** Members\n"
            f"📊 Avg/Member: **{avg_member_trophies:,.0f}**"
        )

        embed.add_field(name=f"🛡️ {name}", value=stats, inline=True)

    if not club_data:
        embed.description = "No data available."

    return embed
