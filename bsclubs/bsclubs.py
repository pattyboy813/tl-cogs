import asyncio
from typing import Dict, List, Optional, Tuple, Union

import aiohttp
import discord
from redbot.core import commands, checks, Config
from redbot.core.bot import Red

BASE_URL = "https://api.brawlstars.com/v1"

# Must match bstools' identifier and schema
BSTOOLS_CONFIG_ID = 0xB5B5B5B5

bstools_config = Config.get_conf(
    None,
    identifier=BSTOOLS_CONFIG_ID,
    force_registration=True,
)

default_guild = {
    "clubs": {},
}
default_user = {
    "brawlstars_accounts": [],
}

bstools_config.register_guild(**default_guild)
bstools_config.register_user(**default_user)

_VALID_TAG_CHARS = set("PYLQGRJCUV0289")


def format_tag(tag: str) -> str:
    return tag.strip("#").upper().replace("O", "0")


def verify_tag(tag: str) -> bool:
    if len(tag) > 15:
        return False
    return all(ch in _VALID_TAG_CHARS for ch in tag)


class BrawlStarsClubs(commands.Cog):

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = bstools_config

        self.session: Optional[aiohttp.ClientSession] = None

        # Command objects we add/remove on the bs group
        self._club_cmd_obj: Optional[commands.Command] = None
        self._clubs_cmd_obj: Optional[commands.Command] = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()

        # Get the existing bs group (from bstools)
        bs_group = self.bot.get_command("bs")
        if not isinstance(bs_group, commands.Group):
            print("[bsclubs] Warning: 'bs' group not found. Load bstools first.")
            return

        # --- define callbacks that discord.py will inspect ---

        async def club_callback(
            ctx: commands.Context,
            target: Optional[Union[discord.Member, discord.User, str]] = None,
        ):
            await self._club_impl(ctx, target)

        async def clubs_callback(ctx: commands.Context):
            await self._clubs_overview_impl(ctx)

        # Build Command objects from these callbacks
        self._club_cmd_obj = commands.Command(
            club_callback,
            name="club",
            help=(
                "Show the club of a Brawl Stars player.\n\n"
                "[p]bs club            → your main saved account\n"
                "[p]bs club @user      → that user's main saved account\n"
                "[p]bs club #PLAYERTAG → use the raw tag"
            ),
        )

        self._clubs_cmd_obj = commands.Command(
            clubs_callback,
            name="clubs",
            help="Overview of all tracked clubs in this server (admin only).",
        )
        # admin-only check
        self._clubs_cmd_obj.add_check(
            checks.admin_or_permissions(manage_guild=True).predicate
        )

        # Attach to existing `bs` group
        bs_group.add_command(self._club_cmd_obj)
        bs_group.add_command(self._clubs_cmd_obj)

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

        # Remove our subcommands from bs group if present
        bs_group = self.bot.get_command("bs")
        if isinstance(bs_group, commands.Group):
            if self._club_cmd_obj:
                bs_group.remove_command(self._club_cmd_obj.name)
            if self._clubs_cmd_obj:
                bs_group.remove_command(self._clubs_cmd_obj.name)

    # ----------------- API helpers -----------------

    async def _get_token(self) -> str:
        tokens = await self.bot.get_shared_api_tokens("brawlstars")
        token = tokens.get("token")
        if not token:
            raise RuntimeError(
                "No Brawl Stars API token found! "
                "Set one with:\n"
                "`[p]set api brawlstars token YOUR_API_TOKEN`"
            )
        return token

    async def _api_request(self, path: str, params: Dict = None) -> Optional[Dict]:
        token = await self._get_token()

        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

        headers = {"Authorization": f"Bearer {token}"}
        url = BASE_URL + path

        async with self.session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 404:
                return None
            text = await resp.text()
            raise RuntimeError(f"API error {resp.status}: {text[:300]}...")

    async def _get_player(self, tag: str) -> Optional[Dict]:
        tag = format_tag(tag)
        return await self._api_request(f"/players/%23{tag}")

    async def _get_club(self, tag: str) -> Optional[Dict]:
        tag = format_tag(tag)
        return await self._api_request(f"/clubs/%23{tag}")

    async def _get_main_tag_for_user(self, user_id: int) -> Optional[str]:
        accounts = await self.config.user_from_id(user_id).brawlstars_accounts()
        if not accounts:
            return None
        return accounts[0]

    # ----------------- Implementations used by callbacks -----------------

    async def _club_impl(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """
        Logic for `bs club`:

        - [p]bs club            → author's main saved account
        - [p]bs club @user      → that user's main saved account
        - [p]bs club #PLAYERTAG → raw tag
        """
        player_tag: Optional[str] = None
        source_user: Optional[discord.abc.User] = None

        # Case 1: @user mentioned
        if isinstance(target, (discord.Member, discord.User)):
            source_user = target
        # Case 2: nothing given → author's main account
        elif target is None:
            source_user = ctx.author
        # Case 3: raw tag string
        else:
            player_tag = target

        if source_user is not None:
            player_tag = await self._get_main_tag_for_user(source_user.id)
            if not player_tag:
                await ctx.send(
                    f"{source_user.mention} has no Brawl Stars accounts saved. "
                    "Use `[p]bs save <tag>` first."
                )
                return

        if not player_tag:
            await ctx.send("No valid player tag found.")
            return

        # Fetch player
        try:
            player_data = await self._get_player(player_tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player_data:
            await ctx.send("Could not find that player. Double-check the tag.")
            return

        club_info = player_data.get("club")
        if not club_info:
            await ctx.send("That player is not in a club.")
            return

        club_tag = club_info.get("tag")
        if not club_tag or not verify_tag(format_tag(club_tag)):
            await ctx.send("Could not determine the player's club tag.")
            return

        # Fetch club details
        try:
            club_data = await self._get_club(club_tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not club_data:
            await ctx.send("Could not fetch details for that club.")
            return

        embed = self._build_club_embed(club_data)
        player_name = player_data.get("name", "Unknown")
        embed.set_footer(
            text=f"Club of {player_name} (#{format_tag(player_tag)}) | Data from Brawl Stars API"
        )
        await ctx.send(embed=embed)

    async def _clubs_overview_impl(self, ctx: commands.Context):
        """
        Logic for `bs clubs` (admin-only).
        Uses clubs saved in bstools' [p]bs addclub.
        """
        clubs = await self.config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `[p]bs addclub` first.")
            return

        tasks: List[asyncio.Task] = []
        for _, tag in clubs.items():
            tasks.append(asyncio.create_task(self._get_club(tag)))

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        collected: List[Tuple[str, str, Dict]] = []
        for (shortname, tag), result in zip(clubs.items(), results):
            if isinstance(result, Exception) or not result:
                continue
            collected.append((shortname, tag, result))

        if not collected:
            await ctx.send("Could not fetch data for any clubs.")
            return

        overview_embed = self._build_overview_embed(collected)
        await ctx.send(embed=overview_embed)

        stats_embed = self._build_clubs_stats_embed(collected)
        await ctx.send(embed=stats_embed)

    # ----------------- Embed builders -----------------

    def _build_club_embed(self, data: Dict) -> discord.Embed:
        name = data.get("name", "Unknown Club")
        tag = data.get("tag", "#??????")
        trophies = data.get("trophies", 0)
        required = data.get("requiredTrophies", 0)
        description = data.get("description") or "No description set."

        members = data.get("members", [])
        total_members = len(members)
        max_members = data.get("maxMembers", 30)

        president = next((m for m in members if m.get("role") == "president"), None)
        vps = [m for m in members if m.get("role") == "vicePresident"]
        seniors = [m for m in members if m.get("role") == "senior"]

        embed = discord.Embed(
            title=f"{name} ({tag})",
            description=description,
            color=discord.Color.blurple(),
        )

        embed.add_field(name="Trophies", value=f"{trophies:,}", inline=True)
        embed.add_field(
            name="Required Trophies", value=f"{required:,}", inline=True
        )
        embed.add_field(
            name="Members", value=f"{total_members}/{max_members}", inline=True
        )

        embed.add_field(
            name="President",
            value=president.get("name") if president else "Unknown",
            inline=True,
        )
        embed.add_field(
            name="Vice Presidents", value=str(len(vps)), inline=True
        )
        embed.add_field(
            name="Seniors", value=str(len(seniors)), inline=True
        )

        embed.set_footer(text="Data from Supercell Brawl Stars API")
        return embed

    def _build_overview_embed(
        self, club_data: List[Tuple[str, str, Dict]]
    ) -> discord.Embed:
        total_clubs = len(club_data)
        total_trophies = 0
        total_members = 0
        total_required = 0
        total_vps = 0
        total_seniors = 0

        for _, _, data in club_data:
            total_trophies += data.get("trophies", 0)
            members = data.get("members", [])
            total_members += len(members)
            total_required += data.get("requiredTrophies", 0)
            total_vps += sum(1 for m in members if m.get("role") == "vicePresident")
            total_seniors += sum(1 for m in members if m.get("role") == "senior")

        def avg(x):
            return x / total_clubs if total_clubs else 0

        embed = discord.Embed(
            title="Overview | Brawl Stars Clubs",
            color=discord.Color.gold(),
        )

        embed.add_field(name="Total Clubs", value=str(total_clubs), inline=True)
        embed.add_field(
            name="Total Trophies", value=f"{total_trophies:,}", inline=True
        )
        embed.add_field(
            name="Total Members", value=str(total_members), inline=True
        )

        embed.add_field(
            name="Average Trophies", value=f"{avg(total_trophies):,.0f}", inline=True
        )
        embed.add_field(
            name="Average Required",
            value=f"{avg(total_required):,.0f}",
            inline=True,
        )
        embed.add_field(
            name="Average Members", value=f"{avg(total_members):,.1f}", inline=True
        )

        embed.add_field(
            name="Average Vice Presidents",
            value=f"{avg(total_vps):,.1f}", inline=True
        )
        embed.add_field(
            name="Average Seniors", value=f"{avg(total_seniors):,.1f}", inline=True
        )

        embed.set_footer(text="Data from Supercell Brawl Stars API")
        return embed

    def _build_clubs_stats_embed(
        self, club_data: List[Tuple[str, str, Dict]]
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Clubs Overview | Stats",
            color=discord.Color.blurple(),
        )

        lines = []
        for shortname, tag, data in club_data:
            name = data.get("name", shortname)
            trophies = data.get("trophies", 0)
            required = data.get("requiredTrophies", 0)
            members = data.get("members", [])
            max_members = data.get("maxMembers", 30)

            vps = sum(1 for m in members if m.get("role") == "vicePresident")
            seniors = sum(1 for m in members if m.get("role") == "senior")

            line = (
                f"**{name}** (`{shortname}` • {tag})\n"
                f"🏆 {trophies:,}  |  Req: {required:,}\n"
                f"👥 {len(members)}/{max_members}  |  VP: {vps}  |  Sr: {seniors}"
            )
            lines.append(line)

        embed.description = "\n\n".join(lines)
        return embed
