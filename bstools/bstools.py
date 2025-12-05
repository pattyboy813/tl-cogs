import asyncio
from typing import List, Optional, Union, Dict, Tuple

import aiohttp
import discord
from redbot.core import commands, checks, Config
from redbot.core.bot import Red

BASE_URL = "https://api.brawlstars.com/v1"

# Shared config identifier
BSTOOLS_CONFIG_ID = 0xB5B5B5B5

bstools_config = Config.get_conf(
    None,
    identifier=BSTOOLS_CONFIG_ID,
    force_registration=True,
)

default_guild = {
    # clubs: mapping of club_tag -> {"tag": "#TAG", "name": "Club Name"}
    "clubs": {},
}

default_user = {
    # ["#TAG1", "#TAG2", ...]
    "brawlstars_accounts": [],
}

bstools_config.register_guild(**default_guild)
bstools_config.register_user(**default_user)


# ----------------- Exceptions / helpers -----------------

class InvalidTag(Exception):
    pass


class TagAlreadySaved(Exception):
    pass


class TagAlreadyExists(Exception):
    def __init__(self, user_id: int, message: str):
        self.user_id = user_id
        self.message = message
        super().__init__(message)


class MainAlreadySaved(Exception):
    pass


class InvalidArgument(Exception):
    pass


_VALID_TAG_CHARS = set("PYLQGRJCUV0289")


def format_tag(tag: str) -> str:
    return tag.strip("#").upper().replace("O", "0")


def verify_tag(tag: str) -> bool:
    if len(tag) > 15:
        return False
    return all(ch in _VALID_TAG_CHARS for ch in tag)


class TagStore:
    """Per-user Brawl Stars tag storage backed by Red's config."""

    def __init__(self, config: Config):
        self.config = config

    async def _get_accounts(self, user_id: int) -> List[str]:
        return await self.config.user_from_id(user_id).brawlstars_accounts()

    async def _set_accounts(self, user_id: int, accounts: List[str]):
        await self.config.user_from_id(user_id).brawlstars_accounts.set(accounts)

    async def account_count(self, user_id: int) -> int:
        accounts = await self._get_accounts(user_id)
        return len(accounts)

    async def get_all_tags(self, user_id: int) -> List[str]:
        return await self._get_accounts(user_id)

    async def save_tag(self, user_id: int, tag: str) -> int:
        tag = format_tag(tag)
        if not verify_tag(tag):
            raise InvalidTag

        accounts = await self._get_accounts(user_id)
        if tag in accounts:
            raise TagAlreadySaved

        # Check if another user has this tag
        all_users = await self.config.all_users()
        for uid_str, data in all_users.items():
            uid = int(uid_str)
            other_accounts = data.get("brawlstars_accounts", [])
            if tag in [format_tag(t) for t in other_accounts]:
                if uid != user_id:
                    raise TagAlreadyExists(uid, f"Tag is saved under another user: {uid}")

        accounts.append(tag)
        await self._set_accounts(user_id, accounts)
        return len(accounts)

    async def unlink_tag(self, user_id: int, account: int):
        accounts = await self._get_accounts(user_id)
        if account < 1 or account > len(accounts):
            raise InvalidArgument
        del accounts[account - 1]
        await self._set_accounts(user_id, accounts)

    async def switch_place(self, user_id: int, account1: int, account2: int):
        accounts = await self._get_accounts(user_id)
        n = len(accounts)
        if (
            account1 < 1 or account1 > n or
            account2 < 1 or account2 > n
        ):
            raise InvalidArgument

        accounts[account1 - 1], accounts[account2 - 1] = (
            accounts[account2 - 1],
            accounts[account1 - 1],
        )
        await self._set_accounts(user_id, accounts)

    async def move_user_id(self, old_user_id: int, new_user_id: int):
        old_accounts = await self._get_accounts(old_user_id)
        new_accounts = await self._get_accounts(new_user_id)
        if new_accounts:
            raise MainAlreadySaved

        await self._set_accounts(new_user_id, old_accounts)
        await self._set_accounts(old_user_id, [])
# ============================================================
#                 BRAWL STARS API HANDLING
# ============================================================

class BrawlStarsAPI:
    """
    Handles all requests to the Brawl Stars API using the shared Red API token.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.token: Optional[str] = None

    async def start(self):
        """Initialize HTTP session + load API token."""
        if self.session is None:
            self.session = aiohttp.ClientSession()

        tokens = await self.bot.get_shared_api_tokens("brawlstars")
        self.token = tokens.get("token")

        if not self.token:
            raise RuntimeError(
                "No Brawl Stars API token set.\n"
                "Use: `[p]set api brawlstars token,YOUR_TOKEN_HERE`"
            )

    async def close(self):
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None

    async def request(self, endpoint: str) -> Dict:
        """
        Sends a GET request.
        Example endpoint: '/players/%23TAG'
        """
        if self.session is None:
            await self.start()

        url = BASE_URL + endpoint
        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()

                if resp.status == 404:
                    return None  # Not found

                text = await resp.text()
                raise RuntimeError(f"API error {resp.status}: {text}")

        except aiohttp.ClientError as e:
            raise RuntimeError(f"Network error contacting Brawl Stars API: {e}")

    async def get_player(self, tag: str) -> Optional[Dict]:
        tag = format_tag(tag)
        return await self.request(f"/players/%23{tag}")

    async def get_club(self, tag: str) -> Optional[Dict]:
        tag = format_tag(tag)
        return await self.request(f"/clubs/%23{tag}")

# ============================================================
#                          MAIN COG
# ============================================================

class BrawlStarsTools(commands.Cog):
    """
    Unified Brawl Stars tools for players, brawlers, clubs, and admin management.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.api = BrawlStarsAPI(bot)
        self.tags = TagStore(bstools_config)
        self._ready = False

    async def cog_load(self):
        """Ensure the API client is ready."""
        await self.api.start()
        self._ready = True

    async def cog_unload(self):
        """Cleanup HTTP session."""
        await self.api.close()

    # -------------------------
    #     API wrappers
    # -------------------------

    async def _get_player(self, tag: str):
        try:
            return await self.api.get_player(tag)
        except RuntimeError as e:
            raise e

    async def _get_club(self, tag: str):
        try:
            return await self.api.get_club(tag)
        except RuntimeError as e:
            raise e

    # ========================================================
    #            BASE COMMAND GROUPS (bs + bs admin)
    # ========================================================

    @commands.group(name="bs")
    async def bs_group(self, ctx: commands.Context):
        """Brawl Stars tools and player commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @bs_group.group(name="admin")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_admin_group(self, ctx: commands.Context):
        """Admin commands for managing clubs + data."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    # ========================================================
    #                   USER COMMANDS
    # ========================================================

    # --------------------------------------------------------
    #  bs save
    # --------------------------------------------------------
    @bs_group.command(name="save")
    async def bs_save(self, ctx: commands.Context, tag: str):
        """
        Save your Brawl Stars player tag.
        Usage:
          [p]bs save #TAG
        """
        clean = format_tag(tag)
        if not verify_tag(clean):
            await ctx.send("Invalid tag.")
            return

        # Validate tag by hitting API
        try:
            player = await self._get_player(clean)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found. Double-check the tag.")
            return

        name = player.get("name", "Unknown Player")

        try:
            idx = await self.tags.save_tag(ctx.author.id, clean)
        except TagAlreadySaved:
            await ctx.send("You already saved this tag.")
            return
        except TagAlreadyExists as e:
            other = ctx.guild.get_member(e.user_id) or f"User ID {e.user_id}"
            await ctx.send(f"This tag is already saved by **{other}**.")
            return
        except InvalidTag:
            await ctx.send("Invalid tag.")
            return

        # Embed handled in Part 6
        embed = self._build_save_embed(ctx.author, name, clean, idx)
        await ctx.send(embed=embed)

    # --------------------------------------------------------
    #  bs accounts
    # --------------------------------------------------------
    @bs_group.command(name="accounts")
    async def bs_accounts(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """
        List saved Brawl Stars accounts.
        """
        user = user or ctx.author
        tags = await self.tags.get_all_tags(user.id)

        # Embed handled in Part 6
        embed = await self._build_accounts_embed(user, tags)
        await ctx.send(embed=embed)

    # --------------------------------------------------------
    #  bs switch
    # --------------------------------------------------------
    @bs_group.command(name="switch")
    async def bs_switch(self, ctx: commands.Context, account1: int, account2: int):
        """
        Swap the order of two saved accounts.
        """
        try:
            await self.tags.switch_place(ctx.author.id, account1, account2)
        except InvalidArgument:
            await ctx.send("Invalid account positions.")
            return

        await ctx.send("Accounts reordered.")
        tags = await self.tags.get_all_tags(ctx.author.id)
        embed = await self._build_accounts_embed(ctx.author, tags)
        await ctx.send(embed=embed)

    # --------------------------------------------------------
    #  bs unsave
    # --------------------------------------------------------
    @bs_group.command(name="unsave")
    async def bs_unsave(self, ctx: commands.Context, account: int):
        """
        Remove a saved account by its index.
        """
        try:
            await self.tags.unlink_tag(ctx.author.id, account)
        except InvalidArgument:
            await ctx.send("Invalid account number.")
            return

        await ctx.send("Account removed.")
        tags = await self.tags.get_all_tags(ctx.author.id)
        embed = await self._build_accounts_embed(ctx.author, tags)
        await ctx.send(embed=embed)

    # ========================================================
    #        PLAYER / CLUB / BRAWLERS DATA COMMANDS
    # ========================================================

    # Utility: resolve @user / #tag / none
    async def _resolve_player_tag(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]],
    ) -> Optional[str]:

        # Case 1: raw tag
        if isinstance(target, str):
            clean = format_tag(target)
            if verify_tag(clean):
                return clean
            await ctx.send("Invalid tag.")
            return None

        # Case 2: @user
        if isinstance(target, (discord.Member, discord.User)):
            tags = await self.tags.get_all_tags(target.id)
            if not tags:
                await ctx.send("That user has no saved accounts.")
                return None
            return tags[0]  # main

        # Case 3: author
        tags = await self.tags.get_all_tags(ctx.author.id)
        if not tags:
            await ctx.send("You have no saved accounts. Use `[p]bs save #TAG`.")
            return None
        return tags[0]

    # --------------------------------------------------------
    #  bs player
    # --------------------------------------------------------
    @bs_group.command(name="player")
    async def bs_player(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """
        Show detailed stats for a Brawl Stars player.

        Accepts:
        - [p]bs player
        - [p]bs player @user
        - [p]bs player #TAG
        """
        tag = await self._resolve_player_tag(ctx, target)
        if not tag:
            return

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found.")
            return

        embed = self._build_player_embed(player)
        await ctx.send(embed=embed)

    # --------------------------------------------------------
    #  bs club  (user command)
    # --------------------------------------------------------
    @bs_group.command(name="club")
    async def bs_club(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """
        Show the club of a player by:
        - Saved main account
        - @user's main account
        - #TAG
        """
        tag = await self._resolve_player_tag(ctx, target)
        if not tag:
            return

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found.")
            return

        club = player.get("club")
        if not club:
            await ctx.send("This player is not in a club.")
            return

        club_tag = club.get("tag")

        try:
            data = await self._get_club(club_tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not data:
            await ctx.send("Club not found.")
            return

        embed = self._build_club_embed(data)
        await ctx.send(embed=embed)

    # --------------------------------------------------------
    #  bs brawlers
    # --------------------------------------------------------
    @bs_group.command(name="brawlers")
    async def bs_brawlers(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """
        Show a player's top brawlers.
        """
        tag = await self._resolve_player_tag(ctx, target)
        if not tag:
            return

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found.")
            return

        embed = self._build_brawlers_embed(player)
        await ctx.send(embed=embed)

    # ========================================================
    #                   ADMIN COMMANDS
    # ========================================================

    @bs_admin_group.command(name="addclub")
    async def bs_add_club(self, ctx: commands.Context, tag: str):
        """
        Add a club to this server's tracked list (by tag only).

        Usage:
          [p]bs admin addclub #TAG
        """
        clean = format_tag(tag)
        if not verify_tag(clean):
            await ctx.send("Invalid club tag.")
            return

        club_tag = f"#{clean}"

        # Fetch club from API to get the real name
        try:
            data = await self._get_club(club_tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not data:
            await ctx.send("Club not found. Double-check the tag.")
            return

        club_name = data.get("name", "Unknown Club")

        async with bstools_config.guild(ctx.guild).clubs() as clubs:
            clubs[club_tag] = {
                "tag": club_tag,
                "name": club_name,
            }

        embed = self._build_addclub_embed(club_name, club_tag)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="delclub")
    async def bs_del_club(self, ctx: commands.Context, tag: str):
        """
        Remove a tracked club by tag.

        Usage:
          [p]bs admin delclub #TAG
        """
        clean = format_tag(tag)
        club_tag = f"#{clean}"

        async with bstools_config.guild(ctx.guild).clubs() as clubs:
            if club_tag not in clubs:
                await ctx.send("That club tag is not currently tracked.")
                return
            removed = clubs.pop(club_tag)

        name = removed.get("name", "Unknown Club")
        embed = self._build_delclub_embed(name, club_tag)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="listclubs")
    async def bs_list_clubs(self, ctx: commands.Context):
        """
        List all tracked clubs for this server.
        """
        clubs = await bstools_config.guild(ctx.guild).clubs()
        embed = self._build_listclubs_embed(clubs)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="refreshclubs")
    async def bs_refresh_clubs(self, ctx: commands.Context):
        """
        Refresh saved club names from the API.
        Tags remain the same; only names are updated.
        """
        clubs = await bstools_config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `[p]bs admin addclub #TAG` first.")
            return

        updated = 0
        failed = 0

        async with bstools_config.guild(ctx.guild).clubs() as clubs_conf:
            for club_tag, club_data in list(clubs_conf.items()):
                tag = club_data.get("tag") or club_tag
                if not tag:
                    failed += 1
                    continue

                try:
                    data = await self._get_club(tag)
                except RuntimeError:
                    failed += 1
                    continue

                if not data:
                    failed += 1
                    continue

                new_name = data.get("name") or club_data.get("name", "Unknown Club")
                if new_name != club_data.get("name"):
                    clubs_conf[club_tag]["name"] = new_name
                    updated += 1

        embed = self._build_refreshclubs_embed(updated, failed)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="clubs")
    async def bs_admin_clubs(self, ctx: commands.Context):
        """
        Overview of all tracked clubs using live API data.
        """
        clubs = await bstools_config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `[p]bs admin addclub #TAG` first.")
            return

        tasks: List[asyncio.Task] = []
        club_meta: List[Tuple[str, str]] = []  # (name, tag)

        for club in clubs.values():
            tag = club.get("tag")
            name = club.get("name", "Unknown Club")
            if not tag:
                continue
            club_meta.append((name, tag))
            tasks.append(asyncio.create_task(self._get_club(tag)))

        if not tasks:
            await ctx.send("No valid club entries found.")
            return

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        collected: List[Tuple[str, str, Dict]] = []
        for (name, tag), result in zip(club_meta, results):
            if isinstance(result, Exception) or not result:
                continue
            collected.append((name, tag, result))

        if not collected:
            await ctx.send("Could not fetch data for any clubs.")
            return

        overview_embed = self._build_overview_embed(collected)
        detail_embed = self._build_clubs_stats_embed(collected)

        await ctx.send(embed=overview_embed)
        await ctx.send(embed=detail_embed)

    # ========================================================
    #                   EMBED BUILDERS
    # ========================================================

    # -----------------------------
    # Save tag embed
    # -----------------------------
    def _build_save_embed(self, user: discord.User, name: str, tag: str, idx: int):
        embed = discord.Embed(
            title="✨ Brawl Stars Account Linked",
            description=f"Saved as **Account #{idx}**\n\n**{name}** (`#{format_tag(tag)}`)",
            color=discord.Color.from_rgb(0, 200, 140)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="Use bs accounts to view your saved accounts.")
        return embed

    # -----------------------------
    # Accounts list embed
    # -----------------------------
    async def _build_accounts_embed(self, user: discord.Member, tags: List[str]):
        embed = discord.Embed(
            title=f"{user.display_name}'s Brawl Stars Accounts",
            color=discord.Color.from_rgb(0, 170, 255),
        )

        if not tags:
            embed.description = (
                "❌ **No accounts saved.**\n"
                "Use `bs save #TAG` to link your account."
            )
            return embed

        lines = []
        for i, tag in enumerate(tags, start=1):
            try:
                data = await self._get_player(tag)
                name = data.get("name", "Unknown")
            except RuntimeError:
                name = "Unknown (API error)"

            is_main = " **(Main)**" if i == 1 else ""
            lines.append(f"**{i}. {name}{is_main}**\n`#{format_tag(tag)}`")

        embed.description = "\n\n".join(lines)
        embed.set_footer(text="Main account is always slot #1.")
        return embed

    # -----------------------------
    # Player profile embed
    # -----------------------------
    def _build_player_embed(self, player: Dict):
        name = player.get("name", "Unknown")
        tag = player.get("tag", "#??????")
        trophies = player.get("trophies", 0)
        highest = player.get("highestTrophies", 0)
        exp_level = player.get("expLevel", 0)

        solo = player.get("soloVictories", 0)
        duo = player.get("duoVictories", 0)
        trio = player.get("3vs3Victories", 0)

        club = player.get("club") or {}
        club_line = "Not in a club"
        if club:
            c_name = club.get("name", "Unknown Club")
            c_tag = club.get("tag", "")
            club_line = f"**{c_name}** (`{c_tag}`)"

        embed = discord.Embed(
            title=f"{name}",
            description=f"`{tag}`",
            color=discord.Color.from_rgb(240, 200, 50),
        )

        embed.add_field(
            name="🏆 Trophies",
            value=f"**{trophies:,}**\n(PB: {highest:,})",
            inline=True,
        )
        embed.add_field(
            name="⭐ Level",
            value=f"**{exp_level}**",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        victories = (
            f"👥 **3v3 Wins:** {trio:,}\n"
            f"🧍 **Solo Wins:** {solo:,}\n"
            f"👫 **Duo Wins:** {duo:,}"
        )
        embed.add_field(name="📊 Victories", value=victories, inline=False)
        embed.add_field(name="🏰 Club", value=club_line, inline=False)

        embed.set_footer(text="Player stats from the Brawl Stars API")
        return embed

    # -----------------------------
    # Club embed (user command)
    # -----------------------------
    def _build_club_embed(self, data: Dict):
        name = data.get("name", "Unknown Club")
        tag = data.get("tag", "#??????")
        trophies = data.get("trophies", 0)
        required = data.get("requiredTrophies", 0)
        desc = data.get("description") or "*No description provided.*"

        members = data.get("members", [])
        max_members = data.get("maxMembers", 30)

        president = next((m for m in members if m.get("role") == "president"), None)
        vps = [m for m in members if m.get("role") == "vicePresident"]
        seniors = [m for m in members if m.get("role") == "senior"]

        embed = discord.Embed(
            title=name,
            description=f"`{tag}`\n\n{desc}",
            color=discord.Color.from_rgb(0, 200, 255),
        )

        embed.add_field(name="🏆 Trophies", value=f"**{trophies:,}**", inline=True)
        embed.add_field(name="🔓 Required", value=f"**{required:,}**", inline=True)
        embed.add_field(
            name="👥 Members",
            value=f"**{len(members)}/{max_members}**",
            inline=True,
        )

        pres_display = "Unknown"
        if president:
            pres_display = f"{president.get('name')} (`{president.get('tag')}`)"

        leadership = (
            f"👑 **President:** {pres_display}\n"
            f"🛡 **Vice Presidents:** {len(vps)}\n"
            f"🎖 **Seniors:** {len(seniors)}"
        )
        embed.add_field(name="🧭 Leadership", value=leadership, inline=False)

        embed.set_footer(text="Club information from Brawl Stars API")
        return embed

    # -----------------------------
    # Brawlers embed
    # -----------------------------
    def _build_brawlers_embed(self, player: Dict):
        name = player.get("name", "Unknown")
        tag = player.get("tag", "#??????")

        brawlers = player.get("brawlers", [])
        if not brawlers:
            return discord.Embed(
                title=f"{name}'s Brawlers",
                description="No brawlers found.",
                color=discord.Color.red(),
            )

        brawlers = sorted(brawlers, key=lambda b: b.get("trophies", 0), reverse=True)
        top = brawlers[:10]

        lines = []
        for b in top:
            lines.append(
                f"**{b.get('name')}**\n"
                f"🏆 {b.get('trophies')} (PB {b.get('highestTrophies')}) • "
                f"⭐ Power {b.get('power')} • 🎖 Rank {b.get('rank')}"
            )

        embed = discord.Embed(
            title=f"{name}'s Top Brawlers",
            description="\n\n".join(lines),
            color=discord.Color.from_rgb(255, 120, 50),
        )
        embed.set_footer(text=f"#{format_tag(tag)} • Showing top 10")
        return embed

    # -----------------------------
    # Add club embed
    # -----------------------------
    def _build_addclub_embed(self, name: str, tag: str):
        embed = discord.Embed(
            title="🏰 Club Added",
            description=f"Tracking **{name}** (`{tag}`)",
            color=discord.Color.green(),
        )
        return embed

    # -----------------------------
    # Delete club embed
    # -----------------------------
    def _build_delclub_embed(self, name: str, tag: str):
        embed = discord.Embed(
            title="🗑 Club Removed",
            description=f"Stopped tracking **{name}** (`{tag}`)",
            color=discord.Color.red(),
        )
        return embed

    # -----------------------------
    # List clubs embed
    # -----------------------------
    def _build_listclubs_embed(self, clubs: Dict[str, Dict]):
        embed = discord.Embed(
            title="📜 Tracked Brawl Stars Clubs",
            color=discord.Color.from_rgb(60, 60, 60),
        )

        if not clubs:
            embed.description = (
                "No clubs saved.\n"
                "Use `bs admin addclub #TAG` to start tracking clubs."
            )
            return embed

        lines = []
        for data in clubs.values():
            name = data.get("name", "Unknown")
            tag = data.get("tag", "#??????")
            lines.append(f"**{name}**\n`{tag}`")

        embed.description = "\n\n".join(lines)
        return embed

    # -----------------------------
    # Refresh clubs embed
    # -----------------------------
    def _build_refreshclubs_embed(self, updated: int, failed: int):
        color = discord.Color.green() if failed == 0 else discord.Color.orange()
        embed = discord.Embed(
            title="🔄 Clubs Updated",
            description=(
                f"Updated: **{updated}** club(s)\n"
                f"Failed: **{failed}** club(s)"
            ),
            color=color,
        )
        return embed

    # -----------------------------
    # Overview embed (admin)
    # -----------------------------
    def _build_overview_embed(self, club_data: List[Tuple[str, str, Dict]]):
        total = len(club_data)
        total_trophies = sum(d.get("trophies", 0) for _, _, d in club_data)
        total_members = sum(len(d.get("members", [])) for _, _, d in club_data)
        total_required = sum(d.get("requiredTrophies", 0) for _, _, d in club_data)

        def avg(x): return x / total if total else 0

        embed = discord.Embed(
            title="📊 Clubs Overview",
            description=f"Tracking **{total}** clubs",
            color=discord.Color.purple(),
        )

        embed.add_field(name="🏆 Total Trophies", value=f"{total_trophies:,}", inline=True)
        embed.add_field(name="👥 Total Members", value=f"{total_members}", inline=True)
        embed.add_field(name="🔓 Total Required", value=f"{total_required:,}", inline=True)

        embed.add_field(name="📈 Avg Trophies", value=f"{avg(total_trophies):,.0f}", inline=True)
        embed.add_field(name="📈 Avg Members", value=f"{avg(total_members):,.1f}", inline=True)
        embed.add_field(name="📈 Avg Required", value=f"{avg(total_required):,.0f}", inline=True)

        embed.set_footer(text="Overall club performance summary")
        return embed

    # -----------------------------
    # Clubs stats embed
    # -----------------------------
    def _build_clubs_stats_embed(self, club_data: List[Tuple[str, str, Dict]]):
        embed = discord.Embed(
            title="📋 Clubs Detail Breakdown",
            color=discord.Color.from_rgb(30, 30, 30),
        )

        lines = []
        for name, tag, data in club_data:
            trophies = data.get("trophies", 0)
            required = data.get("requiredTrophies", 0)
            members = data.get("members", [])
            max_members = data.get("maxMembers", 30)
            vps = sum(1 for m in members if m.get("role") == "vicePresident")
            seniors = sum(1 for m in members if m.get("role") == "senior")

            lines.append(
                f"**{name}** (`{tag}`)\n"
                f"🏆 {trophies:,} • 🔓 Req {required:,}\n"
                f"👥 {len(members)}/{max_members} • 🛡 VP {vps} • 🎖 Sr {seniors}"
            )

        embed.description = "\n\n".join(lines) if lines else "No club data."
        return embed
