from __future__ import annotations

import asyncio
import datetime
import random
from typing import List, Optional

import discord
from discord.ext import tasks

from redbot.core import commands, Config, checks
from redbot.core.commands.converter import TimedeltaConverter
from redbot.core.utils.chat_formatting import humanize_timedelta
from redbot.core.utils.views import ConfirmView


class Giveaways(commands.Cog):
    """Simple interactive giveaways with nice embeds."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9876543210, force_registration=True)
        # Each guild stores list of giveaway dicts and a blacklist
        self.config.register_guild(giveaways=[], blacklist=[])
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    # -------------------------
    # Command group
    # -------------------------

    @commands.guild_only()
    @commands.group(name="giveaway", aliases=["gw"], invoke_without_command=True)
    async def giveaway_group(self, ctx: commands.Context):
        """Manage giveaways."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    # -------------------------
    # CREATE COMMAND (wizard)
    # -------------------------

    @giveaway_group.command(name="create")
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def giveaway_create(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ):
        """
        Start an interactive giveaway setup wizard.

        Example:
        [p]giveaway create
        [p]giveaway create #giveaways
        """
        target_channel = channel or ctx.channel

        # Small helper for waiting for replies
        def msg_check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        # ---- Step 1: Prize ----
        step1 = discord.Embed(
            title="🎉 Giveaway setup – Step 1/3",
            colour=await ctx.embed_colour(),
            description=(
                "**What prize are you giving away?**\n"
                "Type your answer below.\n\n"
                "Type `cancel` to abort."
            ),
        )
        await ctx.send(embed=step1)

        try:
            msg = await ctx.bot.wait_for("message", timeout=180.0, check=msg_check)
        except asyncio.TimeoutError:
            return await ctx.send("⏰ Timed out. Giveaway setup cancelled.")

        if msg.content.lower().strip() in ("cancel", "stop", "abort"):
            return await ctx.send("❌ Giveaway setup cancelled.")

        prize = msg.content.strip()

        # ---- Step 2: Duration ----
        converter = TimedeltaConverter()
        duration = None

        while duration is None:
            step2 = discord.Embed(
                title="🎉 Giveaway setup – Step 2/3",
                colour=await ctx.embed_colour(),
                description=(
                    "**How long should the giveaway run?**\n"
                    "Examples: `30m`, `2h`, `1 day 3 hours`\n\n"
                    "Type `cancel` to abort."
                ),
            )
            await ctx.send(embed=step2)

            try:
                msg = await ctx.bot.wait_for("message", timeout=180.0, check=msg_check)
            except asyncio.TimeoutError:
                return await ctx.send("⏰ Timed out. Giveaway setup cancelled.")

            content = msg.content.strip().lower()
            if content in ("cancel", "stop", "abort"):
                return await ctx.send("❌ Giveaway setup cancelled.")

            try:
                td = await converter.convert(ctx, content)
            except commands.BadArgument:
                await ctx.send(
                    "I couldn't understand that duration. "
                    "Try something like `30m`, `2h`, or `1 day 3 hours`."
                )
                continue

            if td.total_seconds() < 60:
                await ctx.send("Duration must be at least 1 minute.")
                continue

            duration = td

        # ---- Step 3: Winners ----
        winners = None
        while winners is None:
            step3 = discord.Embed(
                title="🎉 Giveaway setup – Step 3/3",
                colour=await ctx.embed_colour(),
                description=(
                    "**How many winners?**\n"
                    "Type a whole number (e.g. `1`, `3`).\n\n"
                    "Type `cancel` to abort."
                ),
            )
            await ctx.send(embed=step3)

            try:
                msg = await ctx.bot.wait_for("message", timeout=180.0, check=msg_check)
            except asyncio.TimeoutError:
                return await ctx.send("⏰ Timed out. Giveaway setup cancelled.")

            content = msg.content.strip().lower()
            if content in ("cancel", "stop", "abort"):
                return await ctx.send("❌ Giveaway setup cancelled.")

            try:
                value = int(content)
            except ValueError:
                await ctx.send("Please type a valid number (like `1` or `3`).")
                continue

            if value < 1:
                await ctx.send("Number of winners must be at least 1.")
                continue
            if value > 50:
                await ctx.send("Let's not go overboard; max 50 winners.")
                continue

            winners = value

        # ---- Summary & confirmation ----
        now = datetime.datetime.utcnow()
        end_time = now + duration

        colour = await ctx.embed_colour()
        preview = discord.Embed(
            title="🎉 Giveaway preview",
            colour=colour,
            description=(
                f"**Prize:** {prize}\n"
                f"**Winners:** {winners}\n"
                f"**Duration:** {humanize_timedelta(timedelta=duration)}\n"
                f"**Ends:** "
                f"{discord.utils.format_dt(end_time, style='R')} "
                f"({discord.utils.format_dt(end_time, style='F')})\n\n"
                f"Will be posted in {target_channel.mention}."
            ),
        )
        preview.set_footer(
            text=f"Hosted by {ctx.author.display_name}",
            icon_url=getattr(ctx.author.display_avatar, "url", discord.Embed.Empty),
        )

        view = ConfirmView(author=ctx.author)
        msg = await ctx.send("Does this look good?", embed=preview, view=view)
        await view.wait()

        if view.result is not True:
            try:
                await msg.edit(content="❌ Giveaway setup cancelled.", view=None)
            except discord.HTTPException:
                pass
            return

        # ---- Create the live giveaway message ----
        giveaway_embed = discord.Embed(
            title=f"🎉 Giveaway: {prize}",
            colour=colour,
            description=(
                "React with 🎉 to enter!\n\n"
                f"**Prize:** {prize}\n"
                f"**Winners:** {winners}\n"
                f"**Ends:** "
                f"{discord.utils.format_dt(end_time, style='R')} "
                f"({discord.utils.format_dt(end_time, style='F')})\n"
            ),
        )
        giveaway_embed.set_footer(
            text=f"Hosted by {ctx.author.display_name}",
            icon_url=getattr(ctx.author.display_avatar, "url", discord.Embed.Empty),
        )

        giveaway_message = await target_channel.send(embed=giveaway_embed)

        try:
            await giveaway_message.add_reaction("🎉")
        except discord.HTTPException:
            # If we can't add the reaction, it's not fatal; people can add it themselves.
            pass

        # Save to config
        guild_conf = self.config.guild(ctx.guild)
        giveaways = await guild_conf.giveaways()
        giveaways.append(
            {
                "channel_id": target_channel.id,
                "message_id": giveaway_message.id,
                "prize": prize,
                "host_id": ctx.author.id,
                "winners": winners,
                "end_time": end_time.timestamp(),  # unix timestamp
                "ended": False,
                "last_winners": [],  # filled when it ends
            }
        )
        await guild_conf.giveaways.set(giveaways)

        await ctx.send(
            f"✅ Giveaway created in {target_channel.mention}!\n"
            f"It will end {discord.utils.format_dt(end_time, style='R')}."
        )

    # -------------------------
    # REROLL COMMAND
    # -------------------------

    @giveaway_group.command(name="reroll")
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def giveaway_reroll(
        self,
        ctx: commands.Context,
        message: discord.Message,
        winners: Optional[int] = None,
    ):
        """
        Reroll winners for a finished giveaway.

        Examples:
        [p]giveaway reroll 123456789012345678
        [p]giveaway reroll 123456789012345678 3
        """
        guild_conf = self.config.guild(ctx.guild)
        giveaways = await guild_conf.giveaways()
        data = None
        for g in giveaways:
            if g.get("message_id") == message.id:
                data = g
                break

        if data is None:
            return await ctx.send("I can't find a stored giveaway for that message.")

        if not data.get("ended", False):
            return await ctx.send("That giveaway hasn’t ended yet.")

        original_winners_count = data.get("winners", 1)
        winners_count = winners or original_winners_count

        # Get blacklist for this guild
        blacklist_ids = await guild_conf.blacklist()

        # Get previous winners if we want to avoid rerolling the same people
        previous_winners_ids = data.get("last_winners", [])

        entries = await self._get_valid_entries(
            message=message,
            guild=ctx.guild,
            blacklist_ids=blacklist_ids,
        )

        # Exclude previous winners from the new roll
        entries = [u for u in entries if u.id not in previous_winners_ids]

        if not entries:
            return await ctx.send("No eligible entries to reroll from (after blacklist + previous winners).")

        if winners_count >= len(entries):
            new_winners = entries
        else:
            new_winners = random.sample(entries, winners_count)

        # Update stored winners
        data["last_winners"] = [u.id for u in new_winners]
        await guild_conf.giveaways.set(giveaways)

        # Announce reroll
        await self._announce_reroll(
            guild=ctx.guild,
            channel=message.channel,
            message=message,
            data=data,
            winners=new_winners,
        )

        await ctx.send("🔁 Rerolled winners!")

    # -------------------------
    # BLACKLIST COMMANDS
    # -------------------------

    @giveaway_group.group(name="blacklist", invoke_without_command=True)
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def giveaway_blacklist(self, ctx: commands.Context):
        """Manage giveaway blacklist."""
        await ctx.send_help(ctx.command)

    @giveaway_blacklist.command(name="add")
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def giveaway_blacklist_add(
        self,
        ctx: commands.Context,
        *users: discord.Member,
    ):
        """
        Add user(s) to the giveaway blacklist.

        Blacklisted users can't win giveaways.
        """
        if not users:
            return await ctx.send("Mention at least one user to blacklist.")

        guild_conf = self.config.guild(ctx.guild)
        blacklist = await guild_conf.blacklist()

        added = []
        for user in users:
            if user.id not in blacklist:
                blacklist.append(user.id)
                added.append(user)

        await guild_conf.blacklist.set(blacklist)

        if not added:
            return await ctx.send("Those users were already blacklisted.")

        mentions = ", ".join(u.mention for u in added)
        await ctx.send(f"🚫 Added to blacklist: {mentions}")

    @giveaway_blacklist.command(name="remove")
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def giveaway_blacklist_remove(
        self,
        ctx: commands.Context,
        *users: discord.Member,
    ):
        """
        Remove user(s) from the giveaway blacklist.
        """
        if not users:
            return await ctx.send("Mention at least one user to un-blacklist.")

        guild_conf = self.config.guild(ctx.guild)
        blacklist = await guild_conf.blacklist()

        removed = []
        for user in users:
            if user.id in blacklist:
                blacklist.remove(user.id)
                removed.append(user)

        await guild_conf.blacklist.set(blacklist)

        if not removed:
            return await ctx.send("None of those users were on the blacklist.")

        mentions = ", ".join(u.mention for u in removed)
        await ctx.send(f"✅ Removed from blacklist: {mentions}")

    @giveaway_blacklist.command(name="list")
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def giveaway_blacklist_list(self, ctx: commands.Context):
        """
        Show the current giveaway blacklist.
        """
        guild_conf = self.config.guild(ctx.guild)
        blacklist = await guild_conf.blacklist()

        if not blacklist:
            return await ctx.send("The blacklist is currently empty.")

        lines = []
        for uid in blacklist:
            member = ctx.guild.get_member(uid)
            if member:
                lines.append(f"- {member.mention} (`{uid}`)")
            else:
                lines.append(f"- Unknown user (`{uid}`)")

        embed = discord.Embed(
            title="🚫 Giveaway blacklist",
            colour=await ctx.embed_colour(),
            description="\n".join(lines),
        )
        await ctx.send(embed=embed)

    # -------------------------
    # BACKGROUND LOOP
    # -------------------------

    @tasks.loop(seconds=30)
    async def check_giveaways(self):
        """Periodically check for ended giveaways and pick winners."""
        now_ts = datetime.datetime.utcnow().timestamp()
        all_guilds = await self.config.all_guilds()

        for guild_id, gdata in all_guilds.items():
            giveaways = gdata.get("giveaways") or []
            if not giveaways:
                continue

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue

            changed = False
            guild_conf = self.config.guild_from_id(guild_id)
            blacklist_ids = await guild_conf.blacklist()

            for g in giveaways:
                if g.get("ended"):
                    continue

                end_time = g.get("end_time", 0)
                if end_time > now_ts:
                    continue  # not finished yet

                channel = guild.get_channel(g["channel_id"])
                if channel is None:
                    g["ended"] = True
                    changed = True
                    continue

                try:
                    message = await channel.fetch_message(g["message_id"])
                except discord.NotFound:
                    g["ended"] = True
                    changed = True
                    continue
                except discord.HTTPException:
                    # Temporary issue; try again next loop.
                    continue

                winners = await self._pick_winners(
                    guild=guild,
                    message=message,
                    winners_count=g.get("winners", 1),
                    blacklist_ids=blacklist_ids,
                )
                # store winner IDs on the giveaway data
                g["last_winners"] = [u.id for u in winners]
                await self._announce_winners(
                    guild=guild,
                    channel=channel,
                    message=message,
                    data=g,
                    winners=winners,
                )
                g["ended"] = True
                changed = True

            if changed:
                await guild_conf.giveaways.set(giveaways)

    @check_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_red_ready()

    # -------------------------
    # HELPERS
    # -------------------------

    async def _get_valid_entries(
        self,
        message: discord.Message,
        guild: discord.Guild,
        blacklist_ids: List[int],
    ) -> List[discord.User]:
        """Get all valid entrants from 🎉 reactions, excluding bots & blacklisted."""
        reaction = discord.utils.get(message.reactions, emoji="🎉")
        if not reaction:
            return []

        users = [u async for u in reaction.users() if not u.bot]

        # Deduplicate and apply blacklist
        unique: List[discord.User] = []
        seen = set()
        for u in users:
            if u.id in seen:
                continue
            if u.id in blacklist_ids:
                continue
            seen.add(u.id)
            unique.append(u)

        return unique

    async def _pick_winners(
        self,
        guild: discord.Guild,
        message: discord.Message,
        winners_count: int,
        blacklist_ids: List[int],
    ) -> List[discord.User]:
        entries = await self._get_valid_entries(
            message=message,
            guild=guild,
            blacklist_ids=blacklist_ids,
        )

        if not entries:
            return []

        if winners_count >= len(entries):
            return entries

        return random.sample(entries, winners_count)

    async def _announce_winners(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        message: discord.Message,
        data: dict,
        winners: List[discord.User],
    ):
        prize = data.get("prize", "a prize")
        host = guild.get_member(data.get("host_id"))
        winners_text = ", ".join(w.mention for w in winners) if winners else "No valid entries."

        # Update original embed
        if message.embeds:
            embed = message.embeds[0].copy()
        else:
            embed = discord.Embed()

        embed.title = f"🎉 Giveaway ended: {prize}"
        embed.colour = discord.Color.red()

        embed.clear_fields()
        embed.add_field(name="Winners", value=winners_text, inline=False)

        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

        # Announce in channel
        if winners:
            text = f"🎉 Congratulations {winners_text}! You won **{prize}**"
            if host:
                text += f" (host: {host.mention})"
        else:
            text = f"Nobody entered the giveaway for **{prize}**."

        try:
            await channel.send(text)
        except discord.HTTPException:
            pass

    async def _announce_reroll(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        message: discord.Message,
        data: dict,
        winners: List[discord.User],
    ):
        prize = data.get("prize", "a prize")
        winners_text = ", ".join(w.mention for w in winners) if winners else "No valid entries."

        # Update original embed with new winners list
        if message.embeds:
            embed = message.embeds[0].copy()
        else:
            embed = discord.Embed()

        embed.title = f"🎉 Giveaway ended (rerolled): {prize}"
        embed.colour = discord.Color.red()
        embed.clear_fields()
        embed.add_field(name="New winners", value=winners_text, inline=False)

        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

        if winners:
            text = f"🔁 Reroll! New winner(s): {winners_text} for **{prize}**"
        else:
            text = f"🔁 Reroll attempted for **{prize}**, but there were no eligible entries."

        try:
            await channel.send(text)
        except discord.HTTPException:
            pass
