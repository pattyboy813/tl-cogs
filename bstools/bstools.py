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
    """Per-user Brawl Stars tag storage backed by bstools_config."""

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

    async def get_users_with_tag(self, tag: str):
        tag = format_tag(tag)
        if not verify_tag(tag):
            raise InvalidTag

        results = []
        all_users = await self.config.all_users()
        for uid_str, data in all_users.items():
            uid = int(uid_str)
            accounts = data.get("brawlstars_accounts", [])
            for idx, t in enumerate(accounts, start=1):
                if format_tag(t) == tag:
                    results.append((uid, idx))
        return results


# ----------------- Cog: BrawlStarsTools -----------------


class BrawlStarsTools(commands.Cog):
    """Brawl Stars tools: player tags, player/club views, and club admin utilities."""

    __author__ = "Pat+ChatGPT"
    __version__ = "2.1.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = bstools_config
        self.tags = TagStore(self.config)

        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # --------- API helpers ---------

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

    async def _api_request(self, path: str, params=None):
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

    async def _get_player(self, tag: str):
        tag = format_tag(tag)
        return await self._api_request(f"/players/%23{tag}")

    async def _get_club(self, tag: str):
        tag = format_tag(tag).lstrip("#")
        return await self._api_request(f"/clubs/%23{tag}")

    async def _get_main_tag_for_user(self, user_id: int) -> Optional[str]:
        tags = await self.tags.get_all_tags(user_id)
        return tags[0] if tags else None

    async def _resolve_player_tag(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]],
    ) -> Optional[str]:
        """Resolve a player tag from #TAG / @user / author."""
        player_tag: Optional[str] = None
        source_user: Optional[discord.abc.User] = None

        if isinstance(target, (discord.Member, discord.User)):
            source_user = target
        elif target is None:
            source_user = ctx.author
        else:
            player_tag = target

        if source_user is not None:
            player_tag = await self._get_main_tag_for_user(source_user.id)
            if not player_tag:
                await ctx.send(
                    f"{source_user.mention} has no Brawl Stars accounts saved. "
                    "Use `[p]bs save <tag>` first."
                )
                return None

        if not player_tag:
            await ctx.send("No valid player tag found.")
            return None

        return player_tag

    # --------- Command group ---------

    @commands.group(name="bs", invoke_without_command=True)
    async def bs_group(self, ctx: commands.Context):
        """Brawl Stars tools."""
        await ctx.send_help(ctx.command)

    # ========================================================
    # USER COMMANDS
    # ========================================================

    @bs_group.command(name="save")
    async def bs_save(self, ctx: commands.Context, tag: str, user: Optional[discord.User] = None):
        """Save a Brawl Stars player tag.

        [p]bs save #TAG
        [p]bs save #TAG @user   (mods only)
        """
        if user is not None and user != ctx.author:
            if not await self.bot.is_mod(ctx.author):
                await ctx.send("You need mod permission to save tags for others.")
                return
        if user is None:
            user = ctx.author

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Invalid tag or player not found.")
            return

        name = player.get("name", "Unknown")

        try:
            idx = await self.tags.save_tag(user.id, tag)
            embed = discord.Embed(
                color=discord.Color.green(),
                description="Use `[p]bs accounts` to see all accounts.",
            )
            embed.set_author(
                name=f"{name} (#{format_tag(tag)}) has been saved as account {idx}.",
                icon_url=user.display_avatar.url,
            )
            await ctx.send(embed=embed)
        except InvalidTag:
            await ctx.send("Invalid tag format.")
        except TagAlreadySaved:
            await ctx.send("That tag is already saved for this user.")
        except TagAlreadyExists as e:
            other = self.bot.get_user(e.user_id)
            mention = other.mention if other else f"`{e.user_id}`"
            embed = discord.Embed(
                title="Error",
                description=f"Tag is saved under another user: {mention}",
                color=discord.Color.red(),
            )
            await ctx.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )

    @bs_group.command(name="accounts")
    async def bs_accounts(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """List all saved Brawl Stars accounts."""
        if user is None:
            user = ctx.author

        tags = await self.tags.get_all_tags(user.id)
        embed = discord.Embed(
            title=f"{user.display_name}'s Brawl Stars Accounts",
            color=discord.Color.green(),
        )

        if not tags:
            embed.add_field(
                name="Accounts",
                value="No Brawl Stars accounts saved.\nUse `[p]bs save <tag>` to save one.",
            )
            await ctx.send(embed=embed)
            return

        lines = []
        for i, tag in enumerate(tags, start=1):
            try:
                data = await self._get_player(tag)
                name = data.get("name", "Unknown") if data else "Unknown"
            except RuntimeError:
                name = "Unknown (API error)"
            lines.append(f"{i}: {name} (#{format_tag(tag)})")

        embed.add_field(name="Accounts", value="\n".join(lines), inline=False)
        await ctx.send(embed=embed)

    @bs_group.command(name="switch")
    async def bs_switch(
        self,
        ctx: commands.Context,
        account_a: int,
        account_b: int,
        user: Optional[discord.Member] = None,
    ):
        """Swap positions of two saved accounts."""
        if user is not None and user != ctx.author:
            if not await self.bot.is_mod(ctx.author):
                await ctx.send("You need mod permissions to switch accounts for others.")
                return
        if user is None:
            user = ctx.author

        try:
            await self.tags.switch_place(user.id, account_a, account_b)
            await ctx.send("Done! Your accounts have been swapped.")
            await self.bs_accounts(ctx, user=user)
        except InvalidArgument:
            await ctx.send(
                "Invalid account numbers. Use `[p]bs accounts` to see valid indices."
            )

    @bs_group.command(name="unsave")
    async def bs_unsave(self, ctx: commands.Context, account: int, user: Optional[discord.User] = None):
        """Unsave a Brawl Stars account by index."""
        if user is not None and user != ctx.author:
            if not await self.bot.is_mod(ctx.author):
                await ctx.send("You need mod permissions to unsave accounts for others.")
                return
        if user is None:
            user = ctx.author

        try:
            await self.tags.unlink_tag(user.id, account)
            await ctx.send("Account unsaved.")
            await self.bs_accounts(ctx, user=user)
        except InvalidArgument:
            await ctx.send(
                "Invalid account index. Use `[p]bs accounts` to see valid indices."
            )

    @bs_group.command(name="accountowners")
    async def bs_account_owners(self, ctx: commands.Context, tag: str):
        """Show which users have this tag saved."""
        try:
            owners = await self.tags.get_users_with_tag(tag)
        except InvalidTag:
            await ctx.send("Invalid tag format.")
            return

        if not owners:
            await ctx.send("No one has this account saved.")
            return

        lines = []
        for uid, idx in owners:
            user = self.bot.get_user(uid)
            mention = user.mention if user else f"`{uid}`"
            lines.append(f"{mention} | account #{idx}")

        await ctx.send(
            "Users with this account:\n" + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )

    # ---------- PLAYER VIEW COMMAND ----------

    @bs_group.command(name="player")
    async def bs_player(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """
        Show a player's profile.

        - [p]bs player            → your main saved account
        - [p]bs player @user      → that user's main saved account
        - [p]bs player #PLAYERTAG → raw tag
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
            await ctx.send("Could not find that player. Double-check the tag.")
            return

        embed = self._build_player_embed(player)
        await ctx.send(embed=embed)

    # ---------- CLUB VIEW COMMAND (USER) ----------

    @bs_group.command(name="club")
    async def bs_club(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """
        Show the club of a Brawl Stars player.

        - [p]bs club            → your main saved account
        - [p]bs club @user      → that user's main saved account
        - [p]bs club #PLAYERTAG → raw tag
        """
        tag = await self._resolve_player_tag(ctx, target)
        if not tag:
            return

        try:
            player_data = await self._get_player(tag)
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
            text=f"Club of {player_name} (#{format_tag(tag)}) | Data from Brawl Stars API"
        )
        await ctx.send(embed=embed)

    # ---------- BRAWLERS VIEW COMMAND ----------

    @bs_group.command(name="brawlers")
    async def bs_brawlers(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """
        Show a summary of a player's brawlers.

        - [p]bs brawlers            → your main saved account
        - [p]bs brawlers @user      → that user's main saved account
        - [p]bs brawlers #PLAYERTAG → raw tag
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
            await ctx.send("Could not find that player. Double-check the tag.")
            return

        brawlers = player.get("brawlers", [])
        if not brawlers:
            await ctx.send("No brawlers data available for this player.")
            return

        # Sort by trophies descending and take top 10
        brawlers_sorted = sorted(brawlers, key=lambda b: b.get("trophies", 0), reverse=True)
        top = brawlers_sorted[:10]

        lines = []
        for b in top:
            name = b.get("name", "Unknown")
            trophies = b.get("trophies", 0)
            highest = b.get("highestTrophies", 0)
            power = b.get("power", 0)
            rank = b.get("rank", 0)
            lines.append(
                f"**{name}** – 🏆 {trophies} (PB {highest}) | ⭐ Power {power} | 🎖 Rank {rank}"
            )

        desc = "\n".join(lines)
        total = len(brawlers)

        embed = discord.Embed(
            title=f"{player.get('name', 'Unknown')}'s Top Brawlers",
            description=desc or "No brawler data.",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Showing top 10 of {total} brawler(s) | #{format_tag(tag)}")
        await ctx.send(embed=embed)

    # ========================================================
    # ADMIN SUBGROUP: !bs admin ...
    # ========================================================

    @bs_group.group(name="admin")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_admin_group(self, ctx: commands.Context):
        """Admin-only Brawl Stars tools."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @bs_admin_group.command(name="addclub")
    async def bs_add_club(self, ctx: commands.Context, tag: str):
        """Add a club to this server's tracked list (admin).

        Usage:
          [p]bs admin addclub #TAG
        """
        club_tag = "#" + format_tag(tag)
        if not verify_tag(format_tag(tag)):
            await ctx.send("Invalid club tag format.")
            return

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

        async with self.config.guild(ctx.guild).clubs() as clubs:
            clubs[club_tag] = {
                "tag": club_tag,
                "name": club_name,
            }

        await ctx.send(f"Added **{club_name}** (`{club_tag}`) to tracked clubs.")

    @bs_admin_group.command(name="delclub")
    async def bs_del_club(self, ctx: commands.Context, tag: str):
        """Remove a club from this server's tracked list (admin) by tag.

        Usage:
          [p]bs admin delclub #TAG
        """
        club_tag = "#" + format_tag(tag)
        async with self.config.guild(ctx.guild).clubs() as clubs:
            if club_tag not in clubs:
                await ctx.send("That club tag is not tracked.")
                return
            removed = clubs.pop(club_tag)
        await ctx.send(f"Removed **{removed.get('name', 'Unknown Club')}** (`{club_tag}`).")

    @bs_admin_group.command(name="listclubs")
    async def bs_list_clubs(self, ctx: commands.Context):
        """List all tracked clubs for this server (admin)."""
        clubs = await self.config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `[p]bs admin addclub #TAG` to add some.")
            return

        lines = []
        for club in clubs.values():
            # club is {"tag": "#XXXX", "name": "Club Name"}
            tag = club.get("tag", "#??????")
            name = club.get("name", "Unknown Club")
            lines.append(f"**{name}** → `{tag}`")

        embed = discord.Embed(
            title="Tracked Brawl Stars Clubs",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="refreshclubs")
    async def bs_refresh_clubs(self, ctx: commands.Context):
        """
        Refresh saved club names from the Brawl Stars API.

        Only updates stored `name` for each saved club; tags are left as-is.
        """
        clubs = await self.config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `[p]bs admin addclub #TAG` first.")
            return

        updated = 0
        failed = 0

        async with self.config.guild(ctx.guild).clubs() as clubs_conf:
            for key, club in list(clubs_conf.items()):
                tag = club.get("tag") or key
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

                new_name = data.get("name") or club.get("name", "Unknown Club")
                if new_name != club.get("name"):
                    clubs_conf[key]["name"] = new_name
                    updated += 1

        await ctx.send(f"Refreshed club names. Updated **{updated}** club(s), **{failed}** failed.")

    @bs_admin_group.command(name="account_transfer")
    async def bs_account_transfer(
        self,
        ctx: commands.Context,
        old: discord.User,
        new: discord.User,
    ):
        """Transfer all BS accounts from one user to another (admin)."""
        try:
            await self.tags.move_user_id(old.id, new.id)
        except MainAlreadySaved:
            await ctx.send(
                f"{new.mention} already has accounts. "
                "They must unsave them first with `[p]bs unsave`."
            )
            return

        await ctx.send("Transfer complete.")
        await self.bs_accounts(ctx, user=new)

    @bs_admin_group.command(name="clubs")
    async def bs_admin_clubs(self, ctx: commands.Context):
        """
        Overview of all tracked clubs (admin).

        Uses clubs from: [p]bs admin addclub #TAG
        """
        clubs = await self.config.guild(ctx.guild).clubs()
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
            await ctx.send("No valid club entries found in config.")
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
        await ctx.send(embed=overview_embed)

        stats_embed = self._build_clubs_stats_embed(collected)
        await ctx.send(embed=stats_embed)

    # ----------------- Embed builders -----------------

    def _build_player_embed(self, player: Dict) -> discord.Embed:
        name = player.get("name", "Unknown")
        tag = player.get("tag", "#??????")
        trophies = player.get("trophies", 0)
        highest = player.get("highestTrophies", 0)
        exp_level = player.get("expLevel", 0)
        solo_victories = player.get("soloVictories", 0)
        duo_victories = player.get("duoVictories", 0)
        trio_victories = player.get("3vs3Victories", 0)
        club = player.get("club") or {}
        club_name = club.get("name", "No Club")
        club_tag = club.get("tag", "")

        embed = discord.Embed(
            title=f"{name} ({tag})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Trophies", value=f"{trophies:,}", inline=True)
        embed.add_field(name="Highest Trophies", value=f"{highest:,}", inline=True)
        embed.add_field(name="EXP Level", value=str(exp_level), inline=True)

        embed.add_field(name="3v3 Wins", value=str(trio_victories), inline=True)
        embed.add_field(name="Solo Wins", value=str(solo_victories), inline=True)
        embed.add_field(name="Duo Wins", value=str(duo_victories), inline=True)

        club_line = club_name if not club_tag else f"{club_name} ({club_tag})"
        embed.add_field(name="Club", value=club_line, inline=False)

        embed.set_footer(text="Data from Supercell Brawl Stars API")
        return embed

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
            value=f"{avg(total_required):,.0f}", inline=True,
        )
        embed.add_field(
            name="Average Members", value=f"{avg(total_members):,.1f}", inline=True,
        )

        embed.add_field(
            name="Average Vice Presidents", value=f"{avg(total_vps):,.1f}", inline=True
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
        for name, tag, data in club_data:
            trophies = data.get("trophies", 0)
            required = data.get("requiredTrophies", 0)
            members = data.get("members", [])
            max_members = data.get("maxMembers", 30)

            vps = sum(1 for m in members if m.get("role") == "vicePresident")
            seniors = sum(1 for m in members if m.get("role") == "senior")

            line = (
                f"**{name}** ({tag})\n"
                f"🏆 {trophies:,}  |  Req: {required:,}\n"
                f"👥 {len(members)}/{max_members}  |  VP: {vps}  |  Sr: {seniors}"
            )
            lines.append(line)

        embed.description = "\n\n".join(lines)
        return embed
