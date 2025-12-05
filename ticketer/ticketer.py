from __future__ import annotations

import io
import logging
import textwrap
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

log = logging.getLogger("red.tlg_tickets")


# ========================================================================== #
#  PANEL VIEW (main "Open Ticket" button)
# ========================================================================== #


class TicketPanelView(discord.ui.View):
    """Persistent view for the 'Open Ticket' button."""

    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Open Ticket",
        style=discord.ButtonStyle.green,
        emoji="🎫",
        custom_id="tlg_ticket_open_button",
    )
    async def open_ticket_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user

        if guild is None or not isinstance(user, discord.Member):
            return

        try:
            msg = await self.cog.handle_open_ticket(guild, user)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            log.exception("Error creating ticket: %s", e)
            await interaction.followup.send(
                "Something broke trying to make your ticket, ping staff about it.",
                ephemeral=True,
            )


# ========================================================================== #
#  PER-TICKET CONTROLS VIEW (Close / Lock / Transcript)
# ========================================================================== #


class TicketControlsView(discord.ui.View):
    """Persistent view for per-ticket control buttons."""

    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    # --- Close button -------------------------------------------------- #

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        emoji="🛑",
        custom_id="tlg_ticket_close_button",
    )
    async def close_ticket_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        user = interaction.user

        if guild is None or not isinstance(channel, discord.TextChannel):
            return

        settings = await self.cog._get_guild_settings(guild)
        info = await self.cog._get_ticket_info(guild, channel)
        if not info:
            await interaction.followup.send(
                "This doesn't look like a ticket channel I know about.", ephemeral=True
            )
            return

        is_owner = user.id == info.get("owner_id")
        is_staff = self.cog._is_staff_member(user, settings)

        if not (is_owner or is_staff):
            await interaction.followup.send(
                "Only the ticket opener or staff can close this ticket.",
                ephemeral=True,
            )
            return

        await interaction.followup.send("Closing this ticket…", ephemeral=True)
        reason = f"Closed via button by {user} ({user.id})"
        await self.cog._close_ticket_channel(channel, closed_by=user, reason=reason)

    # --- Lock button --------------------------------------------------- #

    @discord.ui.button(
        label="Lock",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="tlg_ticket_lock_button",
    )
    async def lock_ticket_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        user = interaction.user

        if guild is None or not isinstance(channel, discord.TextChannel):
            return

        settings = await self.cog._get_guild_settings(guild)
        info = await self.cog._get_ticket_info(guild, channel)
        if not info:
            await interaction.followup.send(
                "This doesn't look like a ticket channel I know about.", ephemeral=True
            )
            return

        # Lock is staff-only
        if not self.cog._is_staff_member(user, settings):
            await interaction.followup.send(
                "Only staff can lock or unlock tickets.", ephemeral=True
            )
            return

        locked = bool(info.get("locked", False))
        new_locked = not locked

        # Update config flag
        async with self.cog.config.guild(guild).tickets() as tickets:
            chan_key = str(channel.id)
            if chan_key in tickets:
                tickets[chan_key]["locked"] = new_locked

        await self.cog._set_ticket_lock_state(channel, settings, locked=new_locked)

        if new_locked:
            await interaction.followup.send(
                "Ticket locked – only staff can talk now.", ephemeral=True
            )
            try:
                await channel.send(f"🔒 Ticket locked by {user.mention}.")
            except discord.HTTPException:
                pass
        else:
            await interaction.followup.send(
                "Ticket unlocked – participants can talk again.", ephemeral=True
            )
            try:
                await channel.send(f"🔓 Ticket unlocked by {user.mention}.")
            except discord.HTTPException:
                pass

    # --- Transcript button -------------------------------------------- #

    @discord.ui.button(
        label="Transcript",
        style=discord.ButtonStyle.secondary,
        emoji="📜",
        custom_id="tlg_ticket_transcript_button",
    )
    async def transcript_ticket_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        user = interaction.user

        if guild is None or not isinstance(channel, discord.TextChannel):
            return

        settings = await self.cog._get_guild_settings(guild)
        info = await self.cog._get_ticket_info(guild, channel)
        if not info:
            await interaction.followup.send(
                "This doesn't look like a ticket channel I know about.", ephemeral=True
            )
            return

        is_owner = user.id == info.get("owner_id")
        is_staff = self.cog._is_staff_member(user, settings)

        if not (is_owner or is_staff):
            await interaction.followup.send(
                "Only the ticket opener or staff can request the transcript.",
                ephemeral=True,
            )
            return

        owner_id = info.get("owner_id")
        owner = guild.get_member(owner_id)
        if not owner:
            await interaction.followup.send(
                "I couldn't find the ticket owner to DM them. Weird.",
                ephemeral=True,
            )
            return

        text = await self.cog._make_transcript_text(channel)
        file_for_dm = discord.File(
            io.StringIO(text), filename=f"ticket-{channel.id}.txt"
        )

        embed = discord.Embed(
            title=f"Ticket Transcript - {channel.name}",
            description=(
                f"Here's a copy of your ticket from **{guild.name}**.\n\n"
                "This is just a log for your records; you don't need to reply."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        try:
            await owner.send(embed=embed)
            await owner.send(file=file_for_dm)
        except discord.HTTPException:
            await interaction.followup.send(
                "I couldn't DM the ticket owner (they might have DMs off).",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Transcript sent to {owner.mention} via DM.", ephemeral=True
        )


# ========================================================================== #
#  MAIN COG
# ========================================================================== #


class Tickets(commands.Cog):
    """TLG Tickets - button-based ticket system for Threat Level Gaming."""

    __author__ = "you + a chatgpt goblin"
    __version__ = "1.2.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xDEADBEF2, force_registration=True
        )

        default_guild = {
            "ticket_category_id": None,
            "support_role_ids": [],
            "log_channel_id": None,
            "max_open_tickets": 2,
            # channel_id -> {owner_id, created_at, open, locked}
            "tickets": {},
        }
        self.config.register_guild(**default_guild)

        self._panel_view: Optional[TicketPanelView] = None
        self._controls_view: Optional[TicketControlsView] = None

    # ------------------------------------------------------------------ #
    # Red life cycle
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        # Persistent views so buttons keep working after restart
        self._panel_view = TicketPanelView(self)
        self._controls_view = TicketControlsView(self)

        self.bot.add_view(self._panel_view)
        self.bot.add_view(self._controls_view)

        log.info("TLG Tickets cog loaded, panel & control views registered.")

    async def cog_unload(self) -> None:
        log.info("TLG Tickets cog unloaded.")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _get_guild_settings(self, guild: discord.Guild) -> Dict[str, Any]:
        return await self.config.guild(guild).all()

    async def _set_guild_ticket_data(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        owner: discord.Member,
    ):
        now_ts = int(datetime.now(timezone.utc).timestamp())
        async with self.config.guild(guild).tickets() as tickets:
            tickets[str(channel.id)] = {
                "owner_id": owner.id,
                "created_at": now_ts,
                "open": True,
                "locked": False,
            }

    async def _mark_ticket_closed(self, guild: discord.Guild, channel: discord.TextChannel):
        async with self.config.guild(guild).tickets() as tickets:
            chan_key = str(channel.id)
            if chan_key in tickets:
                tickets[chan_key]["open"] = False

    async def _get_ticket_info(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> Optional[Dict[str, Any]]:
        tickets = await self.config.guild(guild).tickets()
        return tickets.get(str(channel.id))

    async def _count_open_tickets_for_user(self, guild: discord.Guild, user: discord.Member) -> int:
        tickets = await self.config.guild(guild).tickets()
        count = 0
        for data in tickets.values():
            if not isinstance(data, dict):
                continue
            if data.get("open") and data.get("owner_id") == user.id:
                count += 1
        return count

    def _is_staff_member(self, member: discord.Member, settings: Dict[str, Any]) -> bool:
        support_role_ids = set(settings.get("support_role_ids") or [])
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        return any(r.id in support_role_ids for r in member.roles)

    async def _set_ticket_lock_state(
        self,
        channel: discord.TextChannel,
        settings: Dict[str, Any],
        *,
        locked: bool,
    ) -> None:
        """
        locked=True: only staff can send messages (others read-only).
        locked=False: non-staff participants can send again.
        """
        support_role_ids = set(settings.get("support_role_ids") or [])

        for target, overwrites in channel.overwrites.items():
            # Only toggle per-member overwrites (owner + added users)
            if not isinstance(target, discord.Member):
                continue

            # Staff users can always talk
            if self._is_staff_member(target, settings):
                continue

            ow = channel.overwrites_for(target)
            if locked:
                ow.send_messages = False
            else:
                ow.send_messages = True

            try:
                await channel.set_permissions(
                    target,
                    overwrite=ow,
                    reason="Ticket locked" if locked else "Ticket unlocked",
                )
            except discord.HTTPException:
                continue

    async def _make_transcript_text(
        self,
        channel: discord.TextChannel,
        limit: int = 1000,
    ) -> str:
        """Build a readable text transcript from recent channel history."""
        lines: list[str] = []

        header_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"Ticket transcript for {channel.guild.name} / #{channel.name}")
        lines.append(f"Exported at {header_time}")
        lines.append("-" * 60)

        async for msg in channel.history(limit=limit, oldest_first=True):
            timestamp = msg.created_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            author_name = msg.author.display_name
            content = msg.clean_content or ""
            content = content.replace("\r\n", "\n").replace("\r", "\n")

            if content:
                for line in content.split("\n"):
                    lines.append(f"[{timestamp} UTC] {author_name}: {line}")
            else:
                lines.append(f"[{timestamp} UTC] {author_name}: (no text)")

            for attachment in msg.attachments:
                lines.append(
                    f"[{timestamp} UTC] {author_name}: [attachment] "
                    f"{attachment.filename} <{attachment.url}>"
                )

        if not lines:
            lines.append("No messages in this ticket.")

        return "\n".join(lines)

    async def _close_ticket_channel(
        self,
        channel: discord.TextChannel,
        *,
        closed_by: discord.abc.User,
        reason: str,
    ) -> None:
        """Common close logic used by command and button."""
        guild = channel.guild
        settings = await self._get_guild_settings(guild)
        info = await self._get_ticket_info(guild, channel)

        if not info:
            # Worst case, just delete the channel
            try:
                await channel.delete(reason=reason)
            except discord.HTTPException:
                pass
            return

        owner_id = info.get("owner_id")
        owner = guild.get_member(owner_id) or f"<@{owner_id}>"

        await self._mark_ticket_closed(guild, channel)

        transcript_text = await self._make_transcript_text(channel)
        file_for_log = discord.File(
            io.StringIO(transcript_text),
            filename=f"ticket-{channel.id}.txt",
        )

        log_chan_id = settings.get("log_channel_id")
        log_chan = guild.get_channel(log_chan_id) if log_chan_id else None

        reason_text = reason or "No reason provided."

        # Log to staff channel if configured
        if isinstance(log_chan, discord.TextChannel):
            embed = discord.Embed(
                title="Ticket closed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Channel", value=channel.mention)
            embed.add_field(name="Owner", value=str(owner), inline=False)
            embed.add_field(name="Closed by", value=str(closed_by), inline=False)
            embed.add_field(
                name="Reason",
                value=textwrap.shorten(reason_text, width=200),
                inline=False,
            )
            await log_chan.send(embed=embed, file=file_for_log)
        else:
            # Drop transcript in the ticket channel if no log
            try:
                await channel.send(
                    "No log channel is set; dropping transcript here instead.",
                    file=file_for_log,
                )
            except discord.HTTPException:
                pass

        # DM transcript to ticket owner (if we can)
        if isinstance(owner, discord.Member):
            try:
                dm_embed = discord.Embed(
                    title="Your ticket has been closed",
                    description=(
                        f"Server: **{guild.name}**\n"
                        f"Channel: `{channel.name}`\n"
                        f"Closed by: **{closed_by}**\n\n"
                        "I've attached a text transcript of the conversation "
                        "for your records."
                    ),
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(timezone.utc),
                )

                # Send the nice embed first
                await owner.send(embed=dm_embed)

                # Then send the transcript file as a second DM
                file_for_dm = discord.File(
                    io.StringIO(transcript_text),
                    filename=f"ticket-{channel.id}.txt",
                )
                await owner.send(file=file_for_dm)

            except discord.HTTPException:
                log.info(
                    "Couldn't DM transcript to %s (%s) – probably closed DMs.",
                    owner,
                    getattr(owner, "id", "unknown"),
                )

        # Notify in channel & delete
        try:
            await channel.send(
                "This ticket is now closed. I'll delete this channel in 5 seconds."
            )
        except discord.HTTPException:
            pass

        await discord.utils.sleep_until(
            datetime.now(timezone.utc) + timedelta(seconds=5)
        )

        try:
            await channel.delete(reason=reason_text)
        except discord.HTTPException as e:
            log.warning("Failed to delete ticket channel %s: %s", channel.id, e)

    # ------------------------------------------------------------------ #
    # Ticket creation logic
    # ------------------------------------------------------------------ #

    async def handle_open_ticket(self, guild: discord.Guild, user: discord.Member) -> str:
        """Called by the panel button to actually create the ticket."""
        settings = await self._get_guild_settings(guild)

        category_id = settings.get("ticket_category_id")
        if not category_id:
            return "Tickets aren't set up properly yet. Ping an admin."

        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return "The configured ticket category is missing or not a category. Ping an admin."

        max_open = settings.get("max_open_tickets") or 2
        current_open = await self._count_open_tickets_for_user(guild, user)
        if max_open > 0 and current_open >= max_open:
            return (
                f"You already have {current_open} open ticket(s). "
                "Please close one before making another."
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }

        support_role_ids = settings.get("support_role_ids") or []
        for rid in support_role_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    manage_messages=True,
                )

        base_name = f"ticket-{user.name}".replace(" ", "-")
        if len(base_name) > 90:
            base_name = base_name[:90]
        suffix = str(user.id)[-4:]
        chan_name = f"{base_name}-{suffix}"

        channel = await guild.create_text_channel(
            name=chan_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {user} ({user.id})",
        )

        await self._set_guild_ticket_data(guild, channel, user)

        # Intro embed with controls
        embed = discord.Embed(
            title="Ticket opened",
            description=(
                f"Hey {user.mention}, thanks for opening a ticket.\n\n"
                "A staff member will be with you when they can."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Controls",
            value=(
                "Use the buttons below to manage this ticket:\n"
                "🛑 **Close** – close the ticket\n"
                "🔒 **Lock** – staff-only chat\n"
                "📜 **Transcript** – DM a copy of the ticket"
            ),
            inline=False,
        )
        embed.set_footer(text="Threat Level Gaming | Support Ticket")

        await channel.send(embed=embed, view=self._controls_view)

        return f"Ticket created: {channel.mention}"

    # ------------------------------------------------------------------ #
    # Config commands
    # ------------------------------------------------------------------ #

    @commands.group(name="ticketset")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def ticketset(self, ctx: commands.Context):
        """Configure ticket settings for this server."""
        if ctx.invoked_subcommand is None:
            settings = await self._get_guild_settings(ctx.guild)
            cat = ctx.guild.get_channel(settings.get("ticket_category_id") or 0)
            log_ch = ctx.guild.get_channel(settings.get("log_channel_id") or 0)
            support_roles = [
                ctx.guild.get_role(rid)
                for rid in settings.get("support_role_ids") or []
                if ctx.guild.get_role(rid)
            ]
            max_open = settings.get("max_open_tickets") or 2

            desc_lines = [
                f"Ticket category: {cat.mention if isinstance(cat, discord.CategoryChannel) else 'Not set'}",
                f"Log channel: {log_ch.mention if isinstance(log_ch, discord.TextChannel) else 'Not set'}",
                f"Support roles: {', '.join(r.mention for r in support_roles) if support_roles else 'None'}",
                f"Max open tickets per user: {max_open if max_open > 0 else 'Unlimited'}",
            ]
            await ctx.send("Current ticket settings:\n" + "\n".join(desc_lines))

    @ticketset.command(name="category")
    async def ticketset_category(
        self,
        ctx: commands.Context,
        category: discord.CategoryChannel,
    ):
        """Set the category where new tickets are created."""
        await self.config.guild(ctx.guild).ticket_category_id.set(category.id)
        await ctx.send(f"Ticket category set to {category.mention}.")

    @ticketset.command(name="logchannel")
    async def ticketset_logchannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ):
        """Set the channel where ticket logs/transcripts will be sent."""
        await self.config.guild(ctx.guild).log_channel_id.set(channel.id)
        await ctx.send(f"Ticket log channel set to {channel.mention}.")

    @ticketset.command(name="addsupport")
    async def ticketset_addsupport(
        self,
        ctx: commands.Context,
        role: discord.Role,
    ):
        """Add a support role that can see all tickets."""
        async with self.config.guild(ctx.guild).support_role_ids() as roles:
            if role.id in roles:
                await ctx.send("That role is already a support role.")
                return
            roles.append(role.id)

        await ctx.send(f"{role.mention} added as a support role.")

    @ticketset.command(name="remsupport")
    async def ticketset_remsupport(
        self,
        ctx: commands.Context,
        role: discord.Role,
    ):
        """Remove a support role."""
        async with self.config.guild(ctx.guild).support_role_ids() as roles:
            if role.id not in roles:
                await ctx.send("That role is not currently a support role.")
                return
            roles.remove(role.id)

        await ctx.send(f"{role.mention} removed from support roles.")

    @ticketset.command(name="maxopen")
    async def ticketset_maxopen(
        self,
        ctx: commands.Context,
        count: int,
    ):
        """Set max open tickets per user (0 = unlimited)."""
        if count < 0:
            count = 0
        await self.config.guild(ctx.guild).max_open_tickets.set(count)
        if count == 0:
            await ctx.send("Users can now open unlimited tickets (pray for your mods).")
        else:
            await ctx.send(
                f"Users can now have up to **{count}** open tickets at a time."
            )

    @ticketset.command(name="panel")
    async def ticketset_panel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Post a ticket panel with an 'Open Ticket' button."""
        if channel is None:
            channel = ctx.channel

        settings = await self._get_guild_settings(ctx.guild)
        category_id = settings.get("ticket_category_id")
        if not category_id:
            await ctx.send("Set a ticket category first with `ticketset category`.")
            return

        embed = discord.Embed(
            title="Need help? Open a ticket.",
            description=(
                "Click the button below to open a private ticket with staff.\n\n"
                "Use this for:\n"
                "- Support with TLG events / scrims / tournaments\n"
                "- Reporting issues\n"
                "- Anything you don't want to post in public channels"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Threat Level Gaming | Ticket System")

        await channel.send(embed=embed, view=self._panel_view)
        await ctx.send(f"Ticket panel posted in {channel.mention}.")

    # ------------------------------------------------------------------ #
    # Ticket channel commands (fallback / extra control)
    # ------------------------------------------------------------------ #

    def _is_ticket_channel(self, channel: discord.TextChannel, settings: Dict[str, Any]) -> bool:
        return str(channel.id) in (settings.get("tickets") or {})

    async def _ensure_ticket_channel(
        self,
        ctx: commands.Context,
        require_owner_or_staff: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Check if current channel is a ticket and, optionally, if user can manage it."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command only works in text channels.")
            return None

        settings = await self._get_guild_settings(ctx.guild)
        if not self._is_ticket_channel(ctx.channel, settings):
            await ctx.send("This doesn't look like a ticket channel.")
            return None

        info = await self._get_ticket_info(ctx.guild, ctx.channel)
        if not info:
            await ctx.send("I couldn't find data for this ticket. Weird.")
            return None

        if not require_owner_or_staff:
            return info

        is_owner = ctx.author.id == info.get("owner_id")
        is_staff = self._is_staff_member(ctx.author, settings)

        if not (is_owner or is_staff):
            await ctx.send("Only the ticket owner or staff can do that.")
            return None

        return info

    @commands.command(name="close")
    @commands.guild_only()
    async def ticket_close(self, ctx: commands.Context, *, reason: str = ""):
        """Close this ticket, log it, DM transcript, and delete the channel."""
        info = await self._ensure_ticket_channel(ctx)
        if not info:
            return

        reason_text = (
            reason or f"Closed via command by {ctx.author} ({ctx.author.id})"
        )
        await self._close_ticket_channel(
            ctx.channel, closed_by=ctx.author, reason=reason_text
        )

    @commands.command(name="add")
    @commands.guild_only()
    async def ticket_add(self, ctx: commands.Context, member: discord.Member):
        """Add a user to this ticket."""
        info = await self._ensure_ticket_channel(ctx)
        if not info:
            return

        channel: discord.TextChannel = ctx.channel  # type: ignore
        overwrites = channel.overwrites_for(member)
        overwrites.view_channel = True
        overwrites.send_messages = True
        overwrites.read_message_history = True
        overwrites.attach_files = True
        overwrites.embed_links = True

        await channel.set_permissions(
            member,
            overwrite=overwrites,
            reason=f"Added to ticket by {ctx.author}",
        )
        await ctx.send(f"{member.mention} added to this ticket.")

    @commands.command(name="remove")
    @commands.guild_only()
    async def ticket_remove(self, ctx: commands.Context, member: discord.Member):
        """Remove a user from this ticket."""
        info = await self._ensure_ticket_channel(ctx)
        if not info:
            return

        channel: discord.TextChannel = ctx.channel  # type: ignore
        await channel.set_permissions(
            member,
            overwrite=None,
            reason=f"Removed from ticket by {ctx.author}",
        )
        await ctx.send(f"{member.mention} removed from this ticket.")

    @commands.command(name="rename")
    @commands.guild_only()
    async def ticket_rename(self, ctx: commands.Context, *, new_name: str):
        """Rename this ticket channel."""
        info = await self._ensure_ticket_channel(ctx)
        if not info:
            return

        new_name = new_name.replace(" ", "-")
        if len(new_name) > 90:
            new_name = new_name[:90]

        channel: discord.TextChannel = ctx.channel  # type: ignore
        try:
            await channel.edit(
                name=new_name, reason=f"Ticket renamed by {ctx.author}"
            )
        except discord.HTTPException:
            await ctx.send("Couldn't rename the channel, Discord said no.")
            return

        await ctx.send(f"Channel renamed to **{new_name}**.")

    @commands.command(name="transcript")
    @commands.guild_only()
    async def ticket_transcript(self, ctx: commands.Context):
        """Generate and send a transcript of this ticket (to the channel)."""
        info = await self._ensure_ticket_channel(ctx, require_owner_or_staff=False)
        if not info:
            return

        channel: discord.TextChannel = ctx.channel  # type: ignore
        text = await self._make_transcript_text(channel)
        file = discord.File(
            io.StringIO(text), filename=f"ticket-{channel.id}.txt"
        )
        await ctx.send("Transcript for this ticket:", file=file)


async def setup(bot: Red):
    await bot.add_cog(Tickets(bot))
