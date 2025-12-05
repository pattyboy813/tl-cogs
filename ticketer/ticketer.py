from __future__ import annotations

import io
import logging
import textwrap
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

log = logging.getLogger("red.tlg_tickets")


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
        # Defer reply so we don't hit "interaction failed"
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


class Tickets(commands.Cog):
    """TLG Tickets - button-based ticket system for Threat Level Gaming."""

    __author__ = "you + a chatgpt goblin"
    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xDEADBEF2, force_registration=True
        )

        default_guild = {
            "ticket_category_id": None,     # where ticket channels are created
            "support_role_ids": [],         # roles that can see all tickets
            "log_channel_id": None,         # where transcripts + logs go
            "max_open_tickets": 2,          # per user
            "tickets": {},                  # channel_id -> {owner_id, created_at, open}
        }
        self.config.register_guild(**default_guild)

        # Register global persistent view at runtime in cog_load
        self._panel_view: Optional[TicketPanelView] = None

    # ------------------------------------------------------------------ #
    # Red life cycle
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        # Register persistent view so buttons work after restart
        self._panel_view = TicketPanelView(self)
        self.bot.add_view(self._panel_view)
        log.info("TLG Tickets cog loaded, panel view registered.")

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
        for chan_id, data in tickets.items():
            if not isinstance(data, dict):
                continue
            if data.get("open") and data.get("owner_id") == user.id:
                count += 1
        return count

    # ------------------------------------------------------------------ #
    # Ticket creation logic
    # ------------------------------------------------------------------ #

    async def handle_open_ticket(self, guild: discord.Guild, user: discord.Member) -> str:
        """Called by the button view to actually create the ticket."""
        settings = await self._get_guild_settings(guild)

        category_id = settings.get("ticket_category_id")
        if not category_id:
            return "Tickets aren't set up properly yet. Ping an admin."

        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return "The configured ticket category is missing or not a category. Ping an admin."

        max_open = settings.get("max_open_tickets") or 2
        current_open = await self._count_open_tickets_for_user(guild, user)
        if current_open >= max_open:
            return f"You already have {current_open} open ticket(s). Please close one before making another."

        # Create overwrites
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

        # Channel name
        base_name = f"ticket-{user.name}".replace(" ", "-")
        if len(base_name) > 90:
            base_name = base_name[:90]
        # Make it slightly more unique
        suffix = str(user.id)[-4:]
        chan_name = f"{base_name}-{suffix}"

        # Create channel
        channel = await guild.create_text_channel(
            name=chan_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {user} ({user.id})",
        )

        await self._set_guild_ticket_data(guild, channel, user)

        # Send intro message
        intro = (
            f"Hey {user.mention}, thanks for opening a ticket.\n\n"
            "A staff member will be with you when they can.\n\n"
            "Commands (inside this channel):\n"
            "`[p]close` – close this ticket\n"
            "`[p]add @user` – add someone to the ticket\n"
            "`[p]remove @user` – remove someone from the ticket\n"
            "`[p]rename <new-name>` – rename the channel\n"
            "`[p]transcript` – save a log of this ticket"
        )

        await channel.send(intro)

        return f"Ticket created: {channel.mention}"

    async def _make_transcript(
        self,
        channel: discord.TextChannel,
        limit: int = 1000,
    ) -> discord.File:
        """Build a simple text transcript file from recent channel history."""
        lines = []

        async for msg in channel.history(limit=limit, oldest_first=True):
            created = msg.created_at.replace(tzinfo=timezone.utc).isoformat()
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content.replace("\n", "\\n")
            lines.append(f"[{created}] {author}: {content}")
            for attachment in msg.attachments:
                lines.append(f"[{created}] ATTACHMENT: {attachment.url}")

        if not lines:
            lines.append("No messages in ticket.")

        text = "\n".join(lines)
        buf = io.StringIO(text)
        return discord.File(buf, filename=f"ticket-{channel.id}.txt")

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
                f"Max open tickets per user: {max_open}",
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
        """Set max open tickets per user (0 = unlimited, but don't do that)."""
        if count < 0:
            count = 0
        await self.config.guild(ctx.guild).max_open_tickets.set(count)
        if count == 0:
            await ctx.send("Users can now open unlimited tickets (pray for your mods).")
        else:
            await ctx.send(f"Users can now have up to **{count}** open tickets at a time.")

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
    # Ticket channel commands
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
        support_role_ids = set(settings.get("support_role_ids") or [])
        is_staff = any(r.id in support_role_ids for r in ctx.author.roles) or ctx.author.guild_permissions.manage_guild

        if not (is_owner or is_staff):
            await ctx.send("Only the ticket owner or staff can do that.")
            return None

        return info

    @commands.command(name="close")
    @commands.guild_only()
    async def ticket_close(self, ctx: commands.Context, *, reason: str = ""):
        """
        Close this ticket, log it, and delete the channel.
        """
        info = await self._ensure_ticket_channel(ctx)
        if not info:
            return

        guild = ctx.guild
        channel = ctx.channel
        settings = await self._get_guild_settings(guild)

        owner_id = info.get("owner_id")
        owner = guild.get_member(owner_id) or f"<@{owner_id}>"

        # Mark closed in config
        await self._mark_ticket_closed(guild, channel)

        # Make transcript
        transcript_file = await self._make_transcript(channel)

        log_chan_id = settings.get("log_channel_id")
        log_chan = guild.get_channel(log_chan_id) if log_chan_id else None

        reason_text = reason or "No reason provided."

        if isinstance(log_chan, discord.TextChannel):
            embed = discord.Embed(
                title="Ticket closed",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Channel", value=channel.mention)
            embed.add_field(name="Owner", value=str(owner), inline=False)
            embed.add_field(name="Closed by", value=str(ctx.author), inline=False)
            embed.add_field(name="Reason", value=textwrap.shorten(reason_text, width=200), inline=False)
            await log_chan.send(embed=embed, file=transcript_file)
        else:
            # Drop transcript in the channel itself if no log
            await ctx.send("No log channel set; dropping transcript here instead.", file=transcript_file)

        try:
            await ctx.send("Closing ticket in 5 seconds...")
        except discord.HTTPException:
            pass

        await discord.utils.sleep_until(datetime.now(timezone.utc) + timedelta(seconds=5))

        try:
            await channel.delete(reason=f"Ticket closed by {ctx.author} - {reason_text}")
        except discord.HTTPException as e:
            log.warning("Failed to delete ticket channel %s: %s", channel.id, e)

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

        await channel.set_permissions(member, overwrite=overwrites, reason=f"Added to ticket by {ctx.author}")
        await ctx.send(f"{member.mention} added to this ticket.")

    @commands.command(name="remove")
    @commands.guild_only()
    async def ticket_remove(self, ctx: commands.Context, member: discord.Member):
        """Remove a user from this ticket."""
        info = await self._ensure_ticket_channel(ctx)
        if not info:
            return

        channel: discord.TextChannel = ctx.channel  # type: ignore
        await channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {ctx.author}")
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
            await channel.edit(name=new_name, reason=f"Ticket renamed by {ctx.author}")
        except discord.HTTPException:
            await ctx.send("Couldn't rename the channel, Discord said no.")
            return

        await ctx.send(f"Channel renamed to **{new_name}**.")

    @commands.command(name="transcript")
    @commands.guild_only()
    async def ticket_transcript(self, ctx: commands.Context):
        """Generate and send a transcript of this ticket."""
        info = await self._ensure_ticket_channel(ctx, require_owner_or_staff=False)
        if not info:
            return

        channel: discord.TextChannel = ctx.channel  # type: ignore
        file = await self._make_transcript(channel)
        await ctx.send("Transcript for this ticket:", file=file)

