"""Main cog combining user commands, admin commands and background tasks.

This class wires up the various helper modules into a single cog for
installation into a Redbot instance.  It exposes groups of commands for
players and administrators as well as a scheduled task that keeps club
statistics up to date.  For brevity only a handful of commands are
included here; see the original monolithic file for a comprehensive
implementation.  Additional commands can be added by following the
patterns established below: resolve a tag, call into the API and then
construct an embed via the builders.
"""

from __future__ import annotations

from typing import Optional, Union, Dict, List, Tuple
import asyncio

import discord
from discord.ext import commands, tasks
from redbot.core import commands as rcommands
from redbot.core import checks, Config
from redbot.core.bot import Red

from .api import BrawlStarsAPI
from .utils import (
    TagStore,
    InvalidTag,
    TagAlreadySaved,
    TagAlreadyExists,
    InvalidArgument,
    format_tag,
    verify_tag,
)
from .constants import (
    BSTOOLS_CONFIG_ID,
    default_guild,
    default_user,
    CLUB_ROLE_CONFIG,
)
from . import embed_builders


# Register configuration at import time.  This ensures the same config is
# reused across reloads and the object isn't recreated every time the cog
# is loaded.
bstools_config = Config.get_conf(
    None,
    identifier=BSTOOLS_CONFIG_ID,
    force_registration=True,
)
bstools_config.register_guild(**default_guild)
bstools_config.register_user(**default_user)


class BrawlStarsTools(commands.Cog):
    """Unified Brawl Stars tools for players, brawlers, clubs and admin management."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.api = BrawlStarsAPI(bot)
        self.tags = TagStore(bstools_config)
        self._ready = False
        self.overview_update_loop.start()

    async def cog_load(self):
        """Ensure the API client is ready when the cog loads."""
        await self.api.start()
        self._ready = True

    def cog_unload(self):
        """Cancel tasks and close the API session when unloading the cog."""
        self.overview_update_loop.cancel()
        asyncio.create_task(self.api.close())

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    async def _get_player(self, tag: str) -> Optional[Dict]:
        try:
            return await self.api.get_player(tag)
        except RuntimeError as e:
            raise e

    async def _get_club(self, tag: str) -> Optional[Dict]:
        try:
            return await self.api.get_club(tag)
        except RuntimeError as e:
            raise e

    async def _resolve_player_tag(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]],
    ) -> Optional[str]:
        """Resolve the tag from a raw string or a Discord user."""
        # Case 1: raw tag passed explicitly
        if isinstance(target, str):
            clean = format_tag(target)
            if verify_tag(clean):
                return clean
            await ctx.send("Invalid tag format.")
            return None
        # Case 2: user mention
        if isinstance(target, (discord.Member, discord.User)):
            tags = await self.tags.get_all_tags(target.id)
            if not tags:
                await ctx.send(f"⚠️ {target.display_name} has no saved accounts.")
                return None
            return tags[0]
        # Case 3: default to command author
        tags = await self.tags.get_all_tags(ctx.author.id)
        if not tags:
            await ctx.send("⚠️ You have no saved accounts. Use `bs save #TAG`.")
            return None
        return tags[0]

    # ------------------------------------------------------------------
    # Command groups
    # ------------------------------------------------------------------
    @commands.group(name="bs")
    async def bs_group(self, ctx: commands.Context):
        """Parent command for Brawl Stars utilities."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @bs_group.command(name="save")
    async def bs_save(self, ctx: commands.Context, tag: str):
        """Save your Brawl Stars player tag."""
        clean = format_tag(tag)
        if not verify_tag(clean):
            await ctx.send("Invalid tag.")
            return
        try:
            player = await self._get_player(clean)
        except RuntimeError as e:
            await ctx.send(str(e))
            return
        if not player:
            await ctx.send("Player not found. Double‑check the tag.")
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
        embed = embed_builders.build_save_embed(ctx.author, name, clean, idx, icon_id)
        await ctx.send(embed=embed)

    @bs_group.command(name="accounts")
    async def bs_accounts(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """List saved Brawl Stars accounts."""
        target = user or ctx.author
        tags = await self.tags.get_all_tags(target.id)
        embed = await embed_builders.build_accounts_embed(self.bot, target, tags)
        await ctx.send(embed=embed)

    @bs_group.command(name="player")
    async def bs_player(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """Show detailed stats for a Brawl Stars player."""
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
        embed = embed_builders.build_player_embed(self.bot, player)
        await ctx.send(embed=embed)

    @bs_group.command(name="brawlers")
    async def bs_brawlers(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        """Show a player's top brawlers."""
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
        embed = embed_builders.build_brawlers_embed(player)
        await ctx.send(embed=embed)

    # Additional commands (admin, club, ticketing, etc.) would follow similar patterns
    # by delegating to helper methods and embed builders.

    # ------------------------------------------------------------------
    # Background task
    # ------------------------------------------------------------------
    @tasks.loop(minutes=10)
    async def overview_update_loop(self):
        """Periodically update the live overview embed in each guild."""
        await self.bot.wait_until_red_ready()
        for guild in self.bot.guilds:
            conf = bstools_config.guild(guild)
            channel_id = await conf.overview_channel()
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            clubs = await conf.clubs()
            if not clubs:
                continue
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
            overview_embed = embed_builders.build_overview_embed(collected)
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
        print(f"[BrawlStarsTools] overview_update_loop error: {error}")