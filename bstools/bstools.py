import asyncio
from typing import List, Optional, Union, Dict, Tuple

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import commands, checks, Config
from redbot.core.bot import Red

BASE_URL = "https://api.brawlstars.com/v1"
# CDN for Icons and Badges (Brawlify mirrors official assets)
CDN_ICON_URL = "https://cdn.brawlify.com/profile-icons/regular/{}.png"
CDN_BADGE_URL = "https://cdn.brawlify.com/club-badges/regular/{}.png"

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
    # auto-updating overview
    "overview_channel": None,   # int or None
    "overview_message": None,   # int or None
}

default_user = {
    # ["#TAG1", "#TAG2", ...]
    "brawlstars_accounts": [],
}

bstools_config.register_guild(**default_guild)
bstools_config.register_user(**default_user)

BRAWLER_EMOJIS = {
    "gigi": "<:gigi:1446711155874594816>",
    "ziggy": "<:ziggy:1446711159603593328>",
    "mina": "<:mina:1446711163252641898>",
    "trunk": "<:trunk:1446711166955946029>",
    "alli": "<:alli:1446711170059862270>",
    "kaze": "<:kaze:1446711172878307369>",
    "jaeyong": "<:jaeyong:1446711176204652585>",
    "finx": "<:finx:1446711179509629090>",
    "lumi": "<:lumi:1446711183183970508>",
    "ollie": "<:ollie:1446711186123915294>",
    "meeple": "<:meeple:1446711188993081344>",
    "buzzlightyear": "<:buzzlightyear:1446711192243667045>",
    "juju": "<:juju:1446711195406041182>",
    "shade": "<:shade:1446711198551638178>",
    "kenji": "<:kenji:1446711201789644822>",
    "moe": "<:moe:1446711205149540362>",
    "clancy": "<:clancy:1446711208647331920>",
    "berry": "<:berry:1446711212258754560>",
    "lily": "<:lily:1446711214850969722>",
    "draco": "<:draco:1446711218172858369>",
    "angelo": "<:angelo:1446711221402472579>",
    "melodie": "<:melodie:1446711224522899487>",
    "larrylawrie": "<:larrylawrie:1446711228188852305>",
    "kit": "<:kit:1446711231112024187>",
    "mico": "<:mico:1446711234106888299>",
    "charlie": "<:charlie:1446711237185634334>",
    "chuck": "<:chuck:1446711241161834718>",
    "pearl": "<:pearl:1446711245008011276>",
    "doug": "<:doug:1446711248212459622>",
    "cordelius": "<:cordelius:1446711250829705369>",
    "hank": "<:hank:1446711254269034707>",
    "maisie": "<:maisie:1446711257347526708>",
    "willow": "<:willow:1446720498854531145>",
    "rt": "<:rt:1446720502117695578>",
    "mandy": "<:mandy:1446720506064801972>",
    "gray": "<:gray:1446720509231239239>",
    "chester": "<:chester:1446720512100274268>",
    "buster": "<:buster:1446720515011252307>",
    "gus": "<:gus:1446720518437736652>",
    "sam": "<:sam:1446720521822801944>",
    "otis": "<:otis:1446720525220184145>",
    "bonnie": "<:bonnie:1446720530098028625>",
    "janet": "<:janet:1446720533096824956>",
    "eve": "<:eve:1446720536120922183>",
    "fang": "<:fang:1446720539384352848>",
    "lola": "<:lola:1446720542014046269>",
    "meg": "<:meg:1446720545482604716>",
    "ash": "<:ash:1446720548590850049>",
    "griff": "<:griff:1446720551937642608>",
    "buzz": "<:buzz:1446720555314315365>",
    "grom": "<:grom:1446720558267109376>",
    "squeak": "<:squeak:1446720561391603845>",
    "belle": "<:belle:1446720564600246463>",
    "stu": "<:stu:1446720568270389288>",
    "ruffs": "<:ruffs:1446720571566981261>",
    "edgar": "<:edgar:1446720574855450795>",
    "byron": "<:byron:1446720577736806480>",
    "lou": "<:lou:1446720581323067403>",
    "amber": "<:amber:1446720585081163956>",
    "colette": "<:colette:1446720588436607027>",
    "surge": "<:surge:1446720591921938544>",
    "sprout": "<:sprout:1446720595088769217>",
    "nani": "<:nani:1446720598242889759>",
    "gale": "<:gale:1446720601283629138>",
    "jacky": "<:jacky:1446720604387540993>",
    "max": "<:max:1446720607109779467>",
    "mrp": "<:mrp:1446720610888716288>",
    "emz": "<:emz:1446720614055542876>",
    "bea": "<:bea:1446720617062862998>",
    "sandy": "<:sandy:1446720620212650105>",
    "8bit": "<:8bit:1446720623530217594>",
    "bibi": "<:bibi:1446720626743185549>",
    "carl": "<:carl:1446720629889044560>",
    "rosa": "<:rosa:1446720633059807362>",
    "leon": "<:leon:1446720636306063451>",
    "tick": "<:tick:1446720646674645203>",
    "gene": "<:gene:1446720649925234789>",
    "frank": "<:frank:1446720652945129492>",
    "penny": "<:penny:1446720656136999015>",
    "darryl": "<:darryl:1446720659127537735>",
    "tara": "<:tara:1446720662147305493>",
    "pam": "<:pam:1446720665980895334>",
    "piper": "<:piper:1446735599531851858>",
    "bo": "<:bo:1446735602505613427>",
    "poco": "<:poco:1446735606238675075>",
    "crow": "<:crow:1446735610667729019>",
    "mortis": "<:mortis:1446735613746348122>",
    "elprimo": "<:elprimo:1446735616841744494>",
    "dynamike": "<:dynamike:1446735619798601799>",
    "nita": "<:nita:1446735623380537479>",
    "jessie": "<:jessie:1446735626182332557>",
    "barley": "<:barley:1446735629135380603>",
    "spike": "<:spike:1446735631697842269>",
    "rico": "<:rico:1446735635141497006>",
    "brock": "<:brock:1446735638123647169>",
    "bull": "<:bull:1446735641198071869>",
    "colt": "<:colt:1446735644901511308>",
    "shelly": "<:shelly:1446735648081051679>",
}



def get_brawler_emoji(name: str) -> str:
    """Returns the custom emoji if found, otherwise returns a generic shield."""
    clean_name = name.lower().replace(" ", "").replace(".", "")
    return BRAWLER_EMOJIS.get(clean_name, "🛡️")


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
        if account1 < 1 or account1 > n or account2 < 1 or account2 > n:
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
        # Tag might be with or without '#'
        clean_tag = format_tag(tag)
        return await self.request(f"/clubs/%23{clean_tag}")


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

        # Start background loop
        self.overview_update_loop.start()

    async def cog_load(self):
        """Ensure the API client is ready."""
        await self.api.start()
        self._ready = True

    def cog_unload(self):
        """Cancel tasks and close API session."""
        self.overview_update_loop.cancel()
        asyncio.create_task(self.api.close())

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
        icon_id = player.get("icon", {}).get("id")

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

        embed = self._build_save_embed(ctx.author, name, clean, idx, icon_id)
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

        await ctx.send("✅ **Success:** Accounts reordered.")
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

        await ctx.send("✅ **Success:** Account removed.")
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
            await ctx.send("Invalid tag format.")
            return None

        # Case 2: @user
        if isinstance(target, (discord.Member, discord.User)):
            tags = await self.tags.get_all_tags(target.id)
            if not tags:
                await ctx.send(f"⚠️ {target.display_name} has no saved accounts.")
                return None
            return tags[0]  # main

        # Case 3: author
        tags = await self.tags.get_all_tags(ctx.author.id)
        if not tags:
            await ctx.send("⚠️ You have no saved accounts. Use `bs save #TAG`.")
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
        Show the club of a player.
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
            await ctx.send(f"**{player.get('name')}** is not in a club.")
            return

        club_tag = club.get("tag")

        try:
            data = await self._get_club(club_tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not data:
            await ctx.send("Club data not found.")
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
        badge_id = data.get("badgeId")

        async with bstools_config.guild(ctx.guild).clubs() as clubs:
            clubs[club_tag] = {
                "tag": club_tag,
                "name": club_name,
            }

        embed = self._build_addclub_embed(club_name, club_tag, badge_id)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="delclub")
    async def bs_del_club(self, ctx: commands.Context, tag: str):
        """
        Remove a tracked club by tag.
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
        """
        clubs = await bstools_config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `bs admin addclub #TAG` first.")
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
            await ctx.send("No clubs tracked yet. Use `bs admin addclub #TAG` first.")
            return

        tasks_list: List[asyncio.Task] = []
        club_meta: List[Tuple[str, str]] = []  # (name, tag)

        for club in clubs.values():
            tag = club.get("tag")
            name = club.get("name", "Unknown Club")
            if not tag:
                continue
            club_meta.append((name, tag))
            tasks_list.append(asyncio.create_task(self._get_club(tag)))

        if not tasks_list:
            await ctx.send("No valid club entries found.")
            return

        try:
            results = await asyncio.gather(*tasks_list, return_exceptions=True)
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

    @bs_admin_group.command(name="setoverviewchannel")
    async def bs_set_overview_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """
        Set the channel where the automatic overview message will update every 10 minutes.
        """
        await bstools_config.guild(ctx.guild).overview_channel.set(channel.id)
        await bstools_config.guild(ctx.guild).overview_message.set(None)
        await ctx.send(f"📡 Overview updates will now be posted in {channel.mention}.")

    # ========================================================
    #                   BACKGROUND TASK
    # ========================================================

    @tasks.loop(minutes=10)
    async def overview_update_loop(self):
        """
        Automatically updates the family overview embed in each guild every 10 minutes.
        """
        await self.bot.wait_until_red_ready()

        for guild in self.bot.guilds:
            conf = bstools_config.guild(guild)

            channel_id = await conf.overview_channel()
            if not channel_id:
                continue  # no overview channel set

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            clubs = await conf.clubs()
            if not clubs:
                continue

            # Collect club meta and tasks
            club_meta: List[Tuple[str, str]] = []
            tasks_list: List[asyncio.Task] = []
            for club in clubs.values():
                tag = club.get("tag")
                name = club.get("name", "Unknown Club")
                if not tag:
                    continue
                club_meta.append((name, tag))
                tasks_list.append(asyncio.create_task(self._get_club(tag)))

            if not tasks_list:
                continue

            results = await asyncio.gather(*tasks_list, return_exceptions=True)

            collected: List[Tuple[str, str, Dict]] = []
            for (name, tag), result in zip(club_meta, results):
                if isinstance(result, Exception) or not result:
                    continue
                collected.append((name, tag, result))

            if not collected:
                continue

            overview_embed = self._build_overview_embed(collected)

            msg_id = await conf.overview_message()
            message: Optional[discord.Message] = None

            if msg_id:
                try:
                    message = await channel.fetch_message(msg_id)
                except discord.NotFound:
                    message = None

            if message:
                await message.edit(embed=overview_embed)
            else:
                new_msg = await channel.send(embed=overview_embed)
                await conf.overview_message.set(new_msg.id)

    @overview_update_loop.error
    async def overview_update_loop_error(self, error):
        # basic logging; you can make this fancier (log to channel, etc.)
        print(f"[BrawlStarsTools] overview_update_loop error: {error}")

    # ========================================================
    #                   EMBED BUILDERS
    # ========================================================

    # -----------------------------
    # Save tag embed
    # -----------------------------
def _build_save_embed(user: discord.User, name: str, tag: str, idx: int, icon_id: Optional[int]) -> discord.Embed:
    """Return an embed acknowledging a successfully saved tag.

    The embed borrows colours from the original and calls out the slot
    number. If an icon ID is provided, it uses the official avatar as
    the thumbnail to reinforce identity.

    Parameters
    ----------
    user:
        The Discord user who invoked the command.
    name:
        The in‑game player name.
    tag:
        The Brawl Stars tag (cleaned of # when passed in).
    idx:
        The index at which the tag was saved.
    icon_id:
        Optional ID for the player’s avatar. Can be ``None`` if unknown.
    """
    embed = discord.Embed(
        title="Account Linked!",
        description=(
            f"✅ **{name}** has been linked to your Discord account.\n"
            f"Saved into slot **#{idx}** – use `bs accounts` to view all"
        ),
        color=discord.Color.green(),
    )
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    if icon_id:
        embed.set_thumbnail(url=CDN_ICON_URL.format(icon_id))
    embed.set_footer(text=f"Tag: #{format_tag(tag)}")
    return embed


async def _build_accounts_embed(
    ctx_user: discord.Member,
    tags: List[str],
    fetch_player,
) -> discord.Embed:
    """Build an embed summarising a member’s saved accounts.

    This embed displays each linked tag along with high level stats pulled
    from the API: name, trophy count, unlocked brawlers and the primary
    victory metrics. The first entry is marked as the main account.

    Parameters
    ----------
    ctx_user:
        The Discord member whose accounts are being listed.
    tags:
        A list of raw tags (without ``#``) saved for the user.
    fetch_player:
        A coroutine that accepts a tag and returns the player data dict.
    """
    embed = discord.Embed(
        title=f"🎮 {ctx_user.display_name}'s Linked Accounts",
        color=discord.Color.from_rgb(44, 130, 201),
    )
    embed.set_thumbnail(url=ctx_user.display_avatar.url)

    if not tags:
        embed.description = (
            "⚠️ **No accounts saved.**\n\n"
            "Use `bs save #TAG` to link your Brawl Stars profile."
        )
        return embed

    lines: List[str] = []
    for i, tag in enumerate(tags, start=1):
        try:
            data: Optional[Dict] = await fetch_player(tag)
        except Exception:
            data = None

        if not data:
            name = "Unknown (API Error)"
            trophies = 0
            brawler_count = 0
            trio = solo = duo = 0
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
            f"`#{format_tag(tag)}`"
        )

    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Use 'bs switch <num1> <num2>' to reorder accounts")
    return embed


def _build_player_embed(player: Dict) -> discord.Embed:
    """Construct a detailed player profile embed.

    The layout is inspired by Brawlstats: a concise header with a link to
    the official stats page, grouped fields for trophies, victories and
    brawler info, plus optional club and competitive metrics. Numeric
    values are formatted with commas for readability.
    """
    # Basic data extraction
    name: str = player.get("name", "Unknown")
    tag: str = player.get("tag", "#??????")
    trophies: int = player.get("trophies", 0)
    highest: int = player.get("highestTrophies", 0)
    exp_level: int = player.get("expLevel", 0)
    exp_points: int = player.get("expPoints", 0)
    icon_id: Optional[int] = player.get("icon", {}).get("id")

    brawlers: List[Dict] = player.get("brawlers", []) or []
    brawler_count: int = len(brawlers)
    total_brawler_trophies: int = sum(b.get("trophies", 0) for b in brawlers) if brawlers else 0
    avg_brawler_trophies: float = total_brawler_trophies / brawler_count if brawler_count else 0
    total_brawler_count: int = len(BRAWLER_EMOJIS)  # approximate total brawlers

    top_brawler: Optional[Dict] = max(brawlers, key=lambda b: b.get("trophies", 0)) if brawlers else None

    solo = player.get("soloVictories", 0)
    duo = player.get("duoVictories", 0)
    trio = player.get("3vs3Victories", 0)

    champ_qualified = player.get("isQualifiedFromChampionshipChallenge", False)
    rr_best = player.get("bestRoboRumbleTime")
    big_best = player.get("bestTimeAsBigBrawler")

    # Build embed
    embed = discord.Embed(color=discord.Color.from_rgb(250, 166, 26))

    # Author with link to Brawlstats (strip '#' for URL)
    bs_tag = tag.strip("#")
    author_icon = CDN_ICON_URL.format(icon_id) if icon_id else None
    embed.set_author(
        name=f"{name} (#{bs_tag})",
        icon_url=author_icon,
        url=f"https://brawlstats.com/profile/{bs_tag}",
    )

    if icon_id:
        embed.set_thumbnail(url=CDN_ICON_URL.format(icon_id))

    # Trophy/level stats
    embed.add_field(name="🏆 Trophies", value=f"**{trophies:,}**", inline=True)
    embed.add_field(name="📈 Highest", value=f"{highest:,}", inline=True)
    embed.add_field(name="⭐ Experience", value=f"Lvl {exp_level}\n{exp_points:,} XP", inline=True)

    # Victories
    embed.add_field(
        name="🥊 Victories",
        value=f"3v3: {trio:,}\nSolo: {solo:,}\nDuo: {duo:,}",
        inline=True,
    )

    # Brawler summary
    embed.add_field(
        name="🔓 Brawlers",
        value=f"{brawler_count}/{total_brawler_count} unlocked\nAvg 🏆 {avg_brawler_trophies:,.0f}",
        inline=True,
    )

    # Top brawler info
    if top_brawler:
        tb_name = top_brawler.get("name", "Unknown")
        tb_trophies = top_brawler.get("trophies", 0)
        tb_power = top_brawler.get("power", 0)
        tb_emoji = get_brawler_emoji(tb_name)
        embed.add_field(
            name="🥇 Top Brawler",
            value=(
                f"{tb_emoji} **{tb_name.title()}**\n"
                f"🏆 {tb_trophies:,} • ⚡ P{tb_power}"
            ),
            inline=True,
        )

    # Competitive metrics
    competitive_lines: List[str] = []
    champ_text = "✅ Qualified" if champ_qualified else "❌ Not Qualified"
    competitive_lines.append(f"🏆 Championship: {champ_text}")
    if rr_best:
        competitive_lines.append(f"🤖 Robo Rumble: `{rr_best}`")
    if big_best:
        competitive_lines.append(f"🧱 Big Brawler: `{big_best}`")
    embed.add_field(name="🎯 Competitive", value="\n".join(competitive_lines), inline=True)

    # Club info
    club: Optional[Dict] = player.get("club")
    if club:
        c_name = club.get("name", "Unknown")
        c_tag = club.get("tag", "")
        bs_club_tag = c_tag.strip("#")
        embed.add_field(
            name="🛡️ Club",
            value=f"[{c_name}](https://brawlstats.com/clubs/{bs_club_tag})\n`{c_tag}`",
            inline=False,
        )
    else:
        embed.add_field(name="🛡️ Club", value="Not in a club", inline=False)

    embed.set_footer(text="Powered by the Brawl Stars API • TLG Revamp 2025")
    return embed


def _build_club_embed(data: Dict) -> discord.Embed:
    """Create a rich embed representing a club.

    This design offers a quick glance at the club’s totals, capacity and
    leadership. A link to the club’s Brawlstats page is included on the
    author line when possible.
    """
    name = data.get("name", "Unknown Club")
    tag = data.get("tag", "#??????")
    trophies = data.get("trophies", 0)
    required = data.get("requiredTrophies", 0)
    desc = data.get("description") or "No description."
    badge_id = data.get("badgeId")
    club_type = data.get("type", "unknown").title()

    members: List[Dict] = data.get("members", []) or []
    max_members: int = data.get("maxMembers", 30)

    # Roles counting
    roles: Dict[str, List[Dict]] = {"president": [], "vicePresident": [], "senior": []}
    for m in members:
        r = m.get("role")
        if r in roles:
            roles[r].append(m)

    # Averages
    avg_trophies = (
        sum(m.get("trophies", 0) for m in members) / len(members)
        if members
        else 0
    )
    top_member: Optional[Dict] = max(members, key=lambda m: m.get("trophies", 0)) if members else None

    # Construct embed
    embed = discord.Embed(color=discord.Color.from_rgb(220, 53, 69))
    bs_club_tag = tag.strip("#")
    author_icon = CDN_BADGE_URL.format(badge_id) if badge_id else None
    embed.set_author(
        name=f"{name} (#{bs_club_tag})",
        icon_url=author_icon,
        url=f"https://brawlstats.com/clubs/{bs_club_tag}",
    )

    if badge_id:
        embed.set_thumbnail(url=CDN_BADGE_URL.format(badge_id))

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

    # Leadership
    pres = roles["president"][0] if roles["president"] else None
    pres_text = (
        f"👑 **{pres['name']}**\n🏆 {pres.get('trophies', 0):,}"
        if pres
        else "None"
    )
    embed.add_field(
        name="Leadership",
        value=(
            f"{pres_text}\n"
            f"🛡️ VPs: **{len(roles['vicePresident'])}**\n"
            f"🎖️ Seniors: **{len(roles['senior'])}**"
        ),
        inline=True,
    )

    if top_member:
        tm_name = top_member.get("name", "Unknown")
        tm_trophies = top_member.get("trophies", 0)
        embed.add_field(
            name="🥇 Top Member",
            value=f"**{tm_name}**\n🏆 {tm_trophies:,}",
            inline=True,
        )

    embed.set_footer(text="Powered by the Brawl Stars API • Club Statistics")
    return embed


def _build_brawlers_embed(player: Dict) -> discord.Embed:
    """Return an embed showcasing the player's top brawlers.

    To avoid overly long descriptions, the top 15 brawlers are spread
    across three columns. Each line displays the brawler’s emoji, name,
    rank, power level, trophies and counts of star powers, gadgets and
    gears.
    """
    name = player.get("name", "Unknown")
    icon_id: Optional[int] = player.get("icon", {}).get("id")
    brawlers: List[Dict] = player.get("brawlers", []) or []

    if not brawlers:
        return discord.Embed(
            description="❌ No brawler data available.",
            color=discord.Color.red(),
        )

    # Sort and take top 15
    sorted_brawlers = sorted(brawlers, key=lambda b: b.get("trophies", 0), reverse=True)
    top_15 = sorted_brawlers[:15]

    # Build embed
    embed = discord.Embed(
        title=f"{name}'s Top Brawlers", color=discord.Color.from_rgb(155, 89, 182)
    )
    if icon_id:
        embed.set_thumbnail(url=CDN_ICON_URL.format(icon_id))

    # Prepare lines and split into three columns
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

    # Divide lines into three roughly equal groups
    columns: List[List[str]] = [[], [], []]
    for idx, line in enumerate(lines):
        columns[idx % 3].append(line)

    # Add fields for each column
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


def _build_addclub_embed(name: str, tag: str, badge_id: Optional[int]) -> discord.Embed:
    """Embed confirming that a club has been added for tracking."""
    embed = discord.Embed(
        title="🏰 Tracking Started",
        description=f"Successfully added **{name}** (`{tag}`) to the server club list.",
        color=discord.Color.green(),
    )
    if badge_id:
        embed.set_thumbnail(url=CDN_BADGE_URL.format(badge_id))
    embed.set_footer(text="Use 'bs admin clubs' to view all tracked clubs")
    return embed


def _build_delclub_embed(name: str, tag: str) -> discord.Embed:
    """Embed confirming that a club has been removed from tracking."""
    embed = discord.Embed(
        title="🗑️ Tracking Stopped",
        description=f"Removed **{name}** (`{tag}`) from the server club list.",
        color=discord.Color.dark_grey(),
    )
    return embed


def _build_listclubs_embed(clubs: Dict[str, Dict]) -> discord.Embed:
    """Create a list embed showing all tracked clubs."""
    embed = discord.Embed(
        title="📜 Tracked Clubs",
        color=discord.Color.from_rgb(52, 152, 219),
    )
    if not clubs:
        embed.description = "No clubs are currently being tracked."
        return embed

    club_lines: List[str] = []
    for data in clubs.values():
        name = data.get("name", "Unknown")
        tag = data.get("tag", "#??????")
        club_lines.append(f"**{name}** • `{tag}`")

    embed.description = "\n".join(club_lines)
    return embed


def _build_refreshclubs_embed(updated: int, failed: int) -> discord.Embed:
    """Embed summarising the outcome of a clubs refresh."""
    embed = discord.Embed(
        description=(
            "🔄 **Refreshed Club Data**\n\n"
            f"Updated: `{updated}`\nFailed: `{failed}`"
        ),
        color=discord.Color.blue(),
    )
    return embed


def _build_overview_embed(club_data: List[Tuple[str, str, Dict]]) -> discord.Embed:
    """Aggregate statistics from multiple clubs into a single overview embed."""
    total_clubs = len(club_data)
    total_trophies = sum(data.get("trophies", 0) for _, _, data in club_data)

    total_members = 0
    total_capacity = 0
    total_required = 0
    total_vp = 0
    total_senior = 0
    total_online = 0

    for _, _, data in club_data:
        members: List[Dict] = data.get("members", []) or []
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

    # Compute averages safely
    def safe_avg(total: float, count: int) -> float:
        return (total / count) if count else 0

    avg_trophies = safe_avg(total_trophies, total_clubs)
    avg_required = safe_avg(total_required, total_clubs)
    avg_members = safe_avg(total_members, total_clubs)
    avg_vp = safe_avg(total_vp, total_clubs)
    avg_senior = safe_avg(total_senior, total_clubs)
    avg_online = safe_avg(total_online, total_clubs)

    embed = discord.Embed(
        title="🏰 Clan Family Overview",
        description="Aggregated statistics for all tracked clubs.",
        color=discord.Color.from_rgb(52, 152, 219),
    )

    # Totals
    embed.add_field(name="Total Clubs", value=f"**{total_clubs}**", inline=True)
    embed.add_field(name="Total Trophies", value=f"**{total_trophies:,}**", inline=True)
    embed.add_field(
        name="Members",
        value=f"**{total_members}**/{total_capacity}",
        inline=True,
    )

    # Averages
    embed.add_field(name="Avg Trophies", value=f"{avg_trophies:,.0f}", inline=True)
    embed.add_field(name="Avg Required", value=f"{avg_required:,.0f}", inline=True)
    embed.add_field(name="Avg Vice Presidents", value=f"{avg_vp:.1f}", inline=True)
    embed.add_field(name="Avg Seniors", value=f"{avg_senior:.1f}", inline=True)
    embed.add_field(name="Avg Online", value=f"{avg_online:.1f}", inline=True)
    embed.add_field(name="Avg Members", value=f"{avg_members:.1f}", inline=True)

    embed.set_footer(text="Updating every 10 minutes • Live data from Brawl Stars API")
    return embed


def _build_clubs_stats_embed(club_data: List[Tuple[str, str, Dict]]) -> discord.Embed:
    """Detailed statistics for each club in the overview.

    Each club is represented as a field with its tag, total trophies,
    required trophies, member count/capacity and the average trophies per
    member. The embed uses a darker colour to differentiate it from the
    overview.
    """
    embed = discord.Embed(
        title="📋 Detailed Club Statistics",
        color=discord.Color.dark_grey(),
    )
    for name, tag, data in club_data:
        trophies = data.get("trophies", 0)
        req = data.get("requiredTrophies", 0)
        members: List[Dict] = data.get("members", []) or []
        max_m = data.get("maxMembers", 30)
        member_count = len(members)
        avg_member_trophies = (
            sum(m.get("trophies", 0) for m in members) / member_count
            if member_count
            else 0
        )
        field_value = (
            f"`{tag}`\n"
            f"🏆 **{trophies:,}** | 📥 Req: {req:,}\n"
            f"👥 **{member_count}/{max_m}** Members\n"
            f"📊 Avg/Member: **{avg_member_trophies:,.0f}**"
        )
        embed.add_field(name=f"🛡️ {name}", value=field_value, inline=True)

    if not club_data:
        embed.description = "No data available."
    return embed

