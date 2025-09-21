from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Union, Iterable

import discord
from discord.ext import tasks

from redbot.core import commands, checks, Config, modlog
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list, escape, inline

from .models import Event, EventSettings, GuildSettings
from .defaults import initial_guild_settings, DEFAULTS
from .theme import EmbedFactory, Theme

_ = Translator("ModLogV2", __file__)
GuildChannel = Union[discord.abc.GuildChannel, discord.Thread]


# ---- interactive setup UI (uses Select for channels for broad compat) -------
class SetupView(discord.ui.View):
    def __init__(self, cog: "ModLogV2", guild: discord.Guild, author_id: int):
        super().__init__(timeout=600)  # 10 minutes
        self.cog = cog
        self.guild = guild
        self.author_id = author_id
        self.selected_events: list[str] = []
        self._last_channel_id: Optional[int] = None

        # populate event selector options
        self.event_select.options = [
            discord.SelectOption(label=e.value, value=e.value) for e in Event
        ]

        # populate channel picker with up to 25 text/news channels
        chs = [c for c in guild.channels if isinstance(c, discord.TextChannel) or getattr(c, "type", None) == discord.ChannelType.news]
        self.channel_picker.options = [
            discord.SelectOption(label=f"#{c.name}", value=str(c.id)) for c in chs[:25]
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("Only the person who opened this can use it."),
                ephemeral=True,
            )
            return False
        return True

    def _mutate_selected(self, mutator):
        gs = self.cog._gs(self.guild)
        changed = 0
        for raw in self.selected_events:
            try:
                ev = Event.from_str(raw)
            except Exception:
                continue
            mutator(gs.events[ev])
            changed += 1
        return changed

    async def _save_and_ack(self, interaction: discord.Interaction, msg: str):
        await self.cog._save(self.guild)
        await interaction.response.edit_message(content=msg, view=self)

    @discord.ui.select(
        placeholder="Pick one or more events…",
        min_values=1,
        max_values=25,
        row=0
    )
    async def event_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_events = select.values
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="Pick a modlog channel (optional)",
        min_values=0,
        max_values=1,
        row=1
    )
    async def channel_picker(self, interaction: discord.Interaction, select: discord.ui.Select):
        # values contain stringified channel IDs from the options we set in __init__
        self._last_channel_id = int(select.values[0]) if select.values else None
        await interaction.response.defer()

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success, row=2)
    async def btn_enable(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self.selected_events:
            return await interaction.response.send_message(_("Select events first."), ephemeral=True)
        n = self._mutate_selected(lambda es: setattr(es, "enabled", True))
        await self._save_and_ack(interaction, _("Enabled {n} event(s).").format(n=n))

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger, row=2)
    async def btn_disable(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self.selected_events:
            return await interaction.response.send_message(_("Select events first."), ephemeral=True)
        n = self._mutate_selected(lambda es: setattr(es, "enabled", False))
        await self._save_and_ack(interaction, _("Disabled {n} event(s).").format(n=n))

    @discord.ui.button(label="Embeds On/Off", style=discord.ButtonStyle.primary, row=2)
    async def btn_embeds(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self.selected_events:
            return await interaction.response.send_message(_("Select events first."), ephemeral=True)
        def flip(es: EventSettings):
            es.embed = not bool(es.embed)
        n = self._mutate_selected(flip)
        await self._save_and_ack(interaction, _("Toggled embeds for {n} event(s).").format(n=n))

    @discord.ui.button(label="Set Channel", style=discord.ButtonStyle.secondary, row=3)
    async def btn_set_channel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        ch_id = self._last_channel_id
        if not self.selected_events:
            return await interaction.response.send_message(_("Select events first."), ephemeral=True)
        if not ch_id:
            return await interaction.response.send_message(_("Pick a channel above first."), ephemeral=True)
        n = self._mutate_selected(lambda es: setattr(es, "channel", ch_id))
        await self._save_and_ack(interaction, _("Set channel for {n} event(s).").format(n=n))

    @discord.ui.button(label="Reset Channel", style=discord.ButtonStyle.secondary, row=3)
    async def btn_reset_channel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self.selected_events:
            return await interaction.response.send_message(_("Select events first."), ephemeral=True)
        n = self._mutate_selected(lambda es: setattr(es, "channel", None))
        await self._save_and_ack(interaction, _("Reset channel for {n} event(s).").format(n=n))

    @discord.ui.button(label="Show Summary", style=discord.ButtonStyle.primary, row=4)
    async def btn_summary(self, interaction: discord.Interaction, _button: discord.ui.Button):
        text = self.cog._settings_text(self.guild)
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=4)
    async def btn_close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=_("Setup closed."), view=self)
        self.stop()


# ---- main cog ---------------------------------------------------------------
@cog_i18n(_)
class ModLogV2(commands.Cog):
    """Extended moderation logs — modern embeds + interactive setup."""

    __author__ = ["RePulsar", "TrustyJAID", "you"]
    __version__ = "5.0.1"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=154457677895, force_registration=True)
        self.config.register_guild(**initial_guild_settings().to_dict())
        self.config.register_global(version="0.0.0")
        self._cache: dict[int, GuildSettings] = {}
        self.theme = Theme()
        self.emb = EmbedFactory(self.theme)

        self.invite_refresh.start()

    # ---------- lifecycle ----------
    async def cog_load(self) -> None:
        await self._load_all()

    def cog_unload(self) -> None:
        self.invite_refresh.cancel()

    async def _load_all(self) -> None:
        all_data = await self.config.all_guilds()
        stored = await self.config.version()
        for gid, data in all_data.items():
            try:
                gs = GuildSettings.from_dict(data)
            except Exception:
                # migrate from legacy flat config shape
                gs = initial_guild_settings()
                for e, defaults in DEFAULTS.items():
                    flat = data.get(e.value) or {}
                    merged = {**vars(defaults), **flat}
                    gs.events[e] = EventSettings(**merged)
                gs.ignored_channels = list(data.get("ignored_channels", []))
                gs.invite_links = dict(data.get("invite_links", {}))
            for e, defaults in DEFAULTS.items():
                gs.events.setdefault(e, EventSettings(**vars(defaults)))
            self._cache[gid] = gs
        if stored != self.__version__:
            for gid, gs in self._cache.items():
                await self.config.guild_from_id(gid).set(gs.to_dict())
            await self.config.version.set(self.__version__)

    # ---------- helpers ----------
    def _gs(self, guild: discord.Guild) -> GuildSettings:
        if guild.id not in self._cache:
            self._cache[guild.id] = initial_guild_settings()
        for e, defaults in DEFAULTS.items():
            self._cache[guild.id].events.setdefault(e, EventSettings(**vars(defaults)))
        return self._cache[guild.id]

    def _es(self, guild: discord.Guild, event: Event) -> EventSettings:
        return self._gs(guild).events[event]

    async def _save(self, guild: discord.Guild) -> None:
        await self.config.guild(guild).set(self._gs(guild).to_dict())

    async def _modlog_channel(self, guild: discord.Guild, event: Event) -> Optional[GuildChannel]:
        es = self._es(guild, event)
        get_ch = getattr(guild, "get_channel_or_thread", guild.get_channel)
        ch: Optional[GuildChannel] = get_ch(es.channel) if es.channel else None
        if ch is None:
            try:
                ch = await modlog.get_modlog_channel(guild)
            except RuntimeError:
                return None
        if not ch or not ch.permissions_for(guild.me).send_messages:
            return None
        return ch

    def _ignored(self, guild: discord.Guild, channel: Optional[GuildChannel]) -> bool:
        if channel is None:
            return False
        ignored = set(self._gs(guild).ignored_channels)
        cid = getattr(channel, "id", None)
        if cid in ignored:
            return True
        parent = getattr(channel, "parent", None)
        if parent and getattr(parent, "id", None) in ignored:
            return True
        category = getattr(channel, "category", None)
        if category and getattr(category, "id", None) in ignored:
            return True
        return False

    async def _event_colour(self, guild: discord.Guild, event: Event, role: Optional[discord.Role] = None) -> discord.Colour:
        es = self._es(guild, event)
        if es.colour is not None:
            return discord.Colour(es.colour)
        defaults = {
            Event.MESSAGE_EDIT: discord.Colour.orange(),
            Event.MESSAGE_DELETE: discord.Colour.dark_red(),
            Event.USER_CHANGE: discord.Colour.greyple(),
            Event.ROLE_CHANGE: (role.colour if role else discord.Colour.blue()),
            Event.ROLE_CREATE: discord.Colour.blue(),
            Event.ROLE_DELETE: discord.Colour.dark_blue(),
            Event.VOICE_CHANGE: discord.Colour.magenta(),
            Event.USER_JOIN: discord.Colour.green(),
            Event.USER_LEFT: discord.Colour.dark_green(),
            Event.CHANNEL_CHANGE: discord.Colour.teal(),
            Event.CHANNEL_CREATE: discord.Colour.teal(),
            Event.CHANNEL_DELETE: discord.Colour.dark_teal(),
            Event.GUILD_CHANGE: discord.Colour.blurple(),
            Event.EMOJI_CHANGE: discord.Colour.gold(),
            Event.COMMANDS_USED: guild.me.colour if guild.me and guild.me.colour.value else discord.Colour.blurple(),
            Event.INVITE_CREATED: discord.Colour.blurple(),
            Event.INVITE_DELETED: discord.Colour.blurple(),
            Event.AUTOMOD_RULE_CREATE: discord.Colour.from_rgb(87,242,135),
            Event.AUTOMOD_RULE_UPDATE: discord.Colour.gold(),
            Event.AUTOMOD_RULE_DELETE: discord.Colour.red(),
            Event.AUTOMOD_ACTION:      discord.Colour.from_rgb(88,101,242),
        }
        return defaults[event]

    def _settings_text(self, guild: discord.Guild) -> str:
        gs = self._gs(guild)
        modlog_channel = _("Not Set")
        try:
            ch = self.bot.loop.run_until_complete(modlog.get_modlog_channel(guild))  # best effort
            if ch:
                modlog_channel = ch.mention
        except Exception:
            pass
        lines = [f"**ModLogV2** v{self.__version__}", _("Modlog channel: {c}").format(c=modlog_channel), ""]
        get_ch = getattr(guild, "get_channel_or_thread", guild.get_channel)
        for e in Event:
            es = gs.events[e]
            row = f"{e.value}: **{es.enabled}**"
            if es.channel:
                ch = get_ch(es.channel)
                if ch:
                    row += f" → {ch.mention}"
            lines.append(row)
        if gs.ignored_channels:
            mentions = []
            for cid in list(gs.ignored_channels):
                ch = get_ch(cid)
                if ch:
                    mentions.append(ch.mention)
            if mentions:
                lines.append(_("\nIgnored: ") + ", ".join(mentions))
        return "\n".join(lines)

    # ---------- commands ----------
    @checks.admin_or_permissions(manage_channels=True)
    @commands.group(name="modlogv2", aliases=["modlogs2"])
    @commands.guild_only()
    async def grp(self, ctx: commands.Context):
        """Configure ModLogV2 settings."""
        if ctx.invoked_subcommand is None:
            await self._show_settings(ctx)

    @grp.command(name="setup")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def setup(self, ctx: commands.Context):
        """Interactive setup: pick events, set channels, toggle embeds."""
        view = SetupView(self, ctx.guild, ctx.author.id)
        msg = (
            "**ModLogV2 Setup**\n"
            "1) Use the dropdown to select one or more events.\n"
            "2) (Optional) pick a channel.\n"
            "3) Click a button (Enable/Disable/Embeds/Set/Reset).\n"
            "Tip: Run this again anytime."
        )
        await ctx.send(msg, view=view)

    async def _bulk_update(self, ctx: commands.Context, events: Iterable[str], mutator, ok: str):
        if not events:
            return await ctx.send(_("You must provide at least one event name."))
        bad = []
        for raw in events:
            try:
                ev = Event.from_str(raw)
            except Exception:
                bad.append(raw)
                continue
            mutator(self._es(ctx.guild, ev))
        await self._save(ctx.guild)
        good = [inline(e) for e in events if e not in bad]
        if good:
            await ctx.send(_("{ev} — {msg}").format(ev=humanize_list(good), msg=ok))
        if bad:
            await ctx.send(_("Unknown events: ") + ", ".join(inline(b) for b in bad))

    @grp.command(name="toggle")
    async def toggle(self, ctx: commands.Context, set_to: bool, *events: str):
        await self._bulk_update(ctx, events, lambda es: setattr(es, "enabled", set_to), _("logs set to {s}").format(s=set_to))

    @grp.command(name="embeds")
    async def embeds(self, ctx: commands.Context, set_to: bool, *events: str):
        await self._bulk_update(ctx, events, lambda es: setattr(es, "embed", set_to), _("embeds {s}").format(s="on" if set_to else "off"))

    @grp.command(name="colour", aliases=["color"])
    async def colour(self, ctx: commands.Context, colour: discord.Colour, *events: str):
        await self._bulk_update(ctx, events, lambda es: setattr(es, "colour", colour.value), _("colour set to {c}").format(c=str(colour)))

    @grp.command(name="emoji")
    @commands.bot_has_permissions(add_reactions=True)
    async def emoji(self, ctx: commands.Context, emoji: str, *events: str):
        try:
            await ctx.message.add_reaction(emoji)
        except discord.HTTPException:
            return await ctx.send(_("That emoji can’t be used."))
        await self._bulk_update(ctx, events, lambda es: setattr(es, "emoji", emoji), _("emoji set."))

    @grp.command(name="channel")
    async def channel(self, ctx: commands.Context, channel: discord.TextChannel, *events: str):
        await self._bulk_update(ctx, events, lambda es: setattr(es, "channel", channel.id), _("channel set to {c}").format(c=channel.mention))

    @grp.command(name="resetchannel")
    async def resetchannel(self, ctx: commands.Context, *events: str):
        await self._bulk_update(ctx, events, lambda es: setattr(es, "channel", None), _("channel reset."))

    @grp.command(name="all")
    async def all(self, ctx: commands.Context, set_to: bool):
        gs = self._gs(ctx.guild)
        for es in gs.events.values():
            es.enabled = set_to
        await self._save(ctx.guild)
        await self._show_settings(ctx)

    @grp.command(name="flag")
    async def flag(self, ctx: commands.Context, event: str, flag: str, value: bool):
        """Flip a boolean flag on an event: bots, bulk_enabled, bulk_individual, cached_only, nicknames, embed, enabled."""
        try:
            ev = Event.from_str(event)
        except Exception:
            return await ctx.send(_("Unknown event."))
        es = self._es(ctx.guild, ev)
        if not hasattr(es, flag):
            return await ctx.send(_("That flag doesn't exist for this event."))
        setattr(es, flag, value)
        await self._save(ctx.guild)
        await ctx.send(_("{e}.{f} → {v}").format(e=inline(ev.value), f=inline(flag), v=value))

    @grp.command(name="commandlevel", aliases=["commandslevel"])
    async def commandlevel(self, ctx: commands.Context, *levels: str):
        """Set which privilege levels to log for commands_used: MOD ADMIN BOT_OWNER GUILD_OWNER NONE"""
        valid = {"MOD","ADMIN","BOT_OWNER","GUILD_OWNER","NONE"}
        if not levels:
            return await ctx.send(_("Provide at least one level."))
        lv = [x.upper() for x in levels]
        bad = [x for x in lv if x not in valid]
        if bad:
            return await ctx.send(_("Unknown levels: ") + ", ".join(inline(b) for b in bad))
        es = self._es(ctx.guild, Event.COMMANDS_USED)
        es.privs = lv
        await self._save(ctx.guild)
        await ctx.send(_("Command levels set to: ") + humanize_list([inline(x) for x in lv]))

    @grp.command(name="ignore")
    async def ignore(self, ctx: commands.Context, channel: Union[discord.TextChannel, discord.CategoryChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.Thread]):
        gs = self._gs(ctx.guild)
        if channel.id not in gs.ignored_channels:
            gs.ignored_channels.append(channel.id)
            await self._save(ctx.guild)
            return await ctx.send(_("Now ignoring ") + channel.mention)
        await ctx.send(channel.mention + _(" is already ignored."))

    @grp.command(name="unignore")
    async def unignore(self, ctx: commands.Context, channel: Union[discord.TextChannel, discord.CategoryChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.Thread]):
        gs = self._gs(ctx.guild)
        if channel.id in gs.ignored_channels:
            gs.ignored_channels.remove(channel.id)
            await self._save(ctx.guild)
            return await ctx.send(_("Now tracking ") + channel.mention)
        await ctx.send(channel.mention + _(" is not ignored."))

    async def _show_settings(self, ctx: commands.Context):
        await ctx.maybe_send_embed(self._settings_text(ctx.guild))

    # ---------- background: invite snapshot ----------
    @tasks.loop(minutes=5.0, reconnect=True)
    async def invite_refresh(self):
        await self.bot.wait_until_ready()
        for gid, gs in list(self._cache.items()):
            guild = self.bot.get_guild(gid)
            if not guild or not gs.events[Event.USER_JOIN].enabled:
                continue
            if not guild.me.guild_permissions.manage_guild:
                continue
            try:
                invites = await guild.invites()
            except Exception:
                continue
            snapshot = {}
            for inv in invites:
                created_at = getattr(inv, "created_at", datetime.utcnow())
                channel = getattr(inv, "channel", discord.Object(id=0))
                inviter = getattr(inv, "inviter", discord.Object(id=0))
                snapshot[inv.code] = {
                    "uses": getattr(inv, "uses", 0),
                    "max_age": getattr(inv, "max_age", None),
                    "created_at": created_at.timestamp(),
                    "max_uses": getattr(inv, "max_uses", None),
                    "temporary": getattr(inv, "temporary", False),
                    "inviter": inviter.id,
                    "channel": channel.id,
                }
            gs.invite_links = snapshot
            await self.config.guild_from_id(gid).invite_links.set(snapshot)

    # ---------- listeners (parity + AutoMod) ----------
    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        guild = ctx.guild
        if guild is None:
            return
        es = self._es(guild, Event.COMMANDS_USED)
        if not es.enabled:
            return
        if self._ignored(guild, ctx.channel):
            return
        ch = await self._modlog_channel(guild, Event.COMMANDS_USED)
        if not ch:
            return

        embed_links = ch.permissions_for(guild.me).embed_links and es.embed
        command = ctx.message.content.replace(ctx.prefix, "")
        cmd = ctx.bot.get_command(command)
        if not cmd:
            return
        try:
            priv = cmd.requires.privilege_level.name
        except Exception:
            return
        if es.privs and priv not in es.privs:
            return

        async def role_line():
            if priv == "MOD":
                try:
                    mods = await ctx.bot.db.guild(guild).mod_role()
                except AttributeError:
                    mods = await ctx.bot.get_mod_roles(guild)
                roles = [guild.get_role(r) for r in mods]
                names = ", ".join(r.mention for r in roles if r)
                return names or _("Not Set") + "\nMOD"
            if priv == "ADMIN":
                try:
                    admins = await ctx.bot.db.guild(guild).admin_role()
                except AttributeError:
                    admins = await ctx.bot.get_admin_roles(guild)
                roles = [guild.get_role(r) for r in admins]
                names = ", ".join(r.mention for r in roles if r)
                return names or _("Not Set") + "\nADMIN"
            if priv == "BOT_OWNER":
                return f"<@!{ctx.bot.owner_id}>\nBOT_OWNER"
            if priv == "GUILD_OWNER":
                return guild.owner.mention + "\nGUILD_OWNER"
            return "everyone\nNONE"

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.COMMANDS_USED), ts=ctx.message.created_at)
            self.emb.author(emb, title=_("Command Used"), icon_url=ctx.author.display_avatar.url)
            self.emb.add_fields(emb, [
                (_("User"), self.emb.userline(ctx.author), True),
                (_("Channel"), self.emb.ch_mention(ctx.channel), True),
                (_("Can Run"), str(True), True),
                (_("Required Role"), await role_line(), False),
                (_("Command"), f"```\n{ctx.message.content[:1000]}\n```", False),
            ])
            self.emb.footer_ids(emb, user=ctx.author)
            await ch.send(embed=emb)
        else:
            msg = _("{e} `{t}` {u} used:\n> {cmd}").format(
                e=es.emoji or "🤖",
                t=ctx.message.created_at.strftime("%H:%M:%S"),
                u=str(ctx.author),
                cmd=ctx.message.content,
            )
            await ch.send(msg[:2000])

    @commands.Cog.listener("on_raw_message_delete")
    async def _raw_delete(self, payload: discord.RawMessageDeleteEvent):
        gid = payload.guild_id
        if not gid: return
        guild = self.bot.get_guild(gid)
        if not guild: return
        es = self._es(guild, Event.MESSAGE_DELETE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.MESSAGE_DELETE)
        if not ch: return

        src = getattr(guild, "get_channel_or_thread", guild.get_channel)(payload.channel_id)
        if self._ignored(guild, src): return

        embed_links = ch.permissions_for(guild.me).embed_links and es.embed
        msg = payload.cached_message

        if msg is None:
            if es.cached_only: return
            mention = src.mention if src else f"<#{payload.channel_id}>"
            if embed_links:
                emb = self.emb.base(colour=await self._event_colour(guild, Event.MESSAGE_DELETE))
                self.emb.author(emb, title=_("Deleted Message"))
                self.emb.add_fields(emb, [(_("Channel"), mention, True), (_("Content"), _("*Unknown*"), False)])
                return await ch.send(embed=emb)
            return await ch.send(f"{es.emoji or '🗑️'} `{datetime.utcnow():%H:%M:%S}` " + _("A message was deleted in {c}").format(c=mention))

        if msg.author.bot and not (es.bots or False):
            return

        perp = None
        try:
            if ch.permissions_for(guild.me).view_audit_log:
                async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.message_delete):
                    same_chan = getattr(entry.extra, "channel", None) and entry.extra.channel.id == msg.channel.id
                    if entry.target and entry.target.id == msg.author.id and same_chan:
                        perp = entry.user
                        break
        except Exception:
            pass

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.MESSAGE_DELETE), ts=msg.created_at)
            self.emb.author(emb, title=_("Message Deleted"), icon_url=msg.author.display_avatar.url)
            self.emb.add_fields(emb, [
                (_("Author"), self.emb.userline(msg.author), True),
                (_("Channel"), self.emb.ch_mention(msg.channel), True),
                (_("Deleted by"), self.emb.userline(perp) if perp else "—", True),
                (_("Content"), f"```\n{(msg.content or '')[:1000]}\n```" if msg.content else "—", False),
                (_("Attachments"), ", ".join(a.filename for a in msg.attachments) or "—", False),
            ])
            self.emb.footer_ids(emb, user=msg.author, extra=f"Message ID: {msg.id}")
            return await ch.send(embed=emb)

        header = f"{es.emoji or '🗑️'} `{msg.created_at:%H:%M:%S}` " + (_("{perp} deleted a message from **{a}**").format(perp=perp, a=msg.author) if perp else _("A message from **{a}** was deleted").format(a=msg.author))
        clean = escape(msg.clean_content or "", mass_mentions=True)
        await ch.send(f"{header} in {self.emb.ch_mention(msg.channel)}\n>>> {clean}"[:2000])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        gid = payload.guild_id
        if not gid: return
        guild = self.bot.get_guild(gid)
        if not guild: return
        es = self._es(guild, Event.MESSAGE_DELETE)
        if not es.enabled or not (es.bulk_enabled or False): return
        ch = await self._modlog_channel(guild, Event.MESSAGE_DELETE)
        if not ch: return

        get_ch = getattr(guild, "get_channel_or_thread", guild.get_channel)
        src = get_ch(payload.channel_id)
        if self._ignored(guild, src): return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.MESSAGE_DELETE))
            self.emb.author(emb, title=_("Bulk Message Delete"))
            self.emb.add_fields(emb, [
                (_("Channel"), self.emb.ch_mention(src) if src else f"<#{payload.channel_id}>", True),
                (_("Messages deleted"), str(len(payload.message_ids)), True),
            ])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Bulk delete in {c}, {n} messages.").format(
                e=es.emoji or "🗑️",
                t=datetime.utcnow().strftime("%H:%M:%S"),
                c=self.emb.ch_mention(src) if src else f"<#{payload.channel_id}>",
                n=len(payload.message_ids),
            ))
        if es.bulk_individual:
            for m in payload.cached_messages:
                fake = discord.RawMessageDeleteEvent({"id": m.id, "channel_id": payload.channel_id, "guild_id": gid})
                fake.cached_message = m
                try:
                    await self._raw_delete(fake)
                except Exception:
                    pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        guild = before.guild
        if not guild:
            return
        es = self._es(guild, Event.MESSAGE_EDIT)
        if not es.enabled:
            return
        if before.author.bot and not (es.bots or False):
            return
        if before.content == after.content:
            return
        ch = await self._modlog_channel(guild, Event.MESSAGE_EDIT)
        if not ch:
            return
        if self._ignored(guild, after.channel):
            return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.MESSAGE_EDIT), ts=before.created_at)
            self.emb.author(emb, title=_("Message Edited"), icon_url=before.author.display_avatar.url)
            jump = f"[Jump]({after.jump_url})" if after.jump_url else "—"
            self.emb.add_fields(emb, [
                (_("Author"), self.emb.userline(before.author), True),
                (_("Channel"), self.emb.ch_mention(before.channel), True),
                (_("Jump"), jump, True),
                (_("Before"), f"```\n{(before.content or '')[:1000]}\n```", False),
                (_("After"),  f"```\n{(after.content or '')[:1000]}\n```", False),
            ])
            self.emb.footer_ids(emb, user=before.author, extra=f"Message ID: {before.id}")
            return await ch.send(embed=emb)

        msg = _("{e} `{t}` {u} edited a message in {c}\nBefore:\n> {b}\nAfter:\n> {a}").format(
            e=es.emoji or "📝",
            t=datetime.utcnow().strftime("%H:%M:%S"),
            u=str(before.author),
            c=before.channel.mention,
            b=escape(before.content or "", mass_mentions=True),
            a=escape(after.content or "", mass_mentions=True),
        )
        await ch.send(msg[:2000])

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        es = self._es(guild, Event.USER_JOIN)
        if not es.enabled:
            return
        ch = await self._modlog_channel(guild, Event.USER_JOIN)
        if not ch:
            return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        possible = ""
        try:
            if guild.me.guild_permissions.manage_guild:
                current = await guild.invites()
                snapshot = self._gs(guild).invite_links
                for inv in current:
                    old = snapshot.get(inv.code)
                    if old and inv.uses > old.get("uses", 0):
                        inviter = inv.inviter.mention if inv.inviter else _("Unknown")
                        possible = f"https://discord.gg/{inv.code}\nInvited by: {inviter}"
                        break
        except Exception:
            pass

        created_days = (datetime.utcnow() - member.created_at).days
        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.USER_JOIN), ts=member.joined_at or datetime.utcnow())
            self.emb.author(emb, title=_("Member Joined"), icon_url=member.display_avatar.url)
            self.emb.add_fields(emb, [
                (_("Member"), self.emb.userline(member), True),
                (_("Total Users:"), str(len(guild.members)), True),
                (_("Account created on:"), f"{member.created_at:%d %b %Y %H:%M}\n({created_days} days ago)", False),
                (_("Invite Link"), possible or "—", False),
            ])
            self.emb.footer_ids(emb, user=member)
            emb.set_thumbnail(url=member.display_avatar.url)
            return await ch.send(embed=emb)

        await ch.send(_("{e} `{t}` **{m}** (`{i}`) joined. Total members: {n}").format(
            e=es.emoji or "📥",
            t=datetime.utcnow().strftime("%H:%M:%S"),
            m=member,
            i=member.id,
            n=len(guild.members),
        ))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        es = self._es(guild, Event.USER_LEFT)
        if not es.enabled:
            return
        ch = await self._modlog_channel(guild, Event.USER_LEFT)
        if not ch:
            return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        perp, reason = None, None
        try:
            if ch.permissions_for(guild.me).view_audit_log:
                after = datetime.utcnow() - timedelta(minutes=30)
                async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick, after=after):
                    if log.target.id == member.id:
                        perp, reason = log.user, log.reason
                        break
        except Exception:
            pass

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.USER_LEFT), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Member Left"), icon_url=member.display_avatar.url)
            self.emb.add_fields(emb, [
                (_("Member"), self.emb.userline(member), True),
                (_("Total Users:"), str(len(guild.members)), True),
                (_("Kicked"), self.emb.userline(perp) if perp else "—", True),
                (_("Reason"), str(reason) if reason else "—", False),
            ])
            self.emb.footer_ids(emb, user=member)
            emb.set_thumbnail(url=member.display_avatar.url)
            return await ch.send(embed=emb)

        msg = _("{e} `{t}` **{m}** (`{i}`) {verb}. Total members: {n}").format(
            e=es.emoji or "📤",
            t=datetime.utcnow().strftime("%H:%M:%S"),
            m=member, i=member.id,
            verb=_("was kicked by {p}").format(p=perp) if perp else _("left the server"),
            n=len(guild.members),
        )
        await ch.send(msg)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, new_channel: discord.abc.GuildChannel):
        guild = new_channel.guild
        es = self._es(guild, Event.CHANNEL_CREATE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.CHANNEL_CREATE)
        if not ch: return
        if self._ignored(guild, new_channel): return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        perp, reason = None, None
        try:
            if ch.permissions_for(guild.me).view_audit_log:
                async for log in guild.audit_logs(limit=2, action=discord.AuditLogAction.channel_create):
                    if log.target.id == new_channel.id:
                        perp, reason = log.user, log.reason
                        break
        except Exception:
            pass

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.CHANNEL_CREATE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Channel Created"))
            self.emb.add_fields(emb, [
                (_("Channel"), new_channel.mention, True),
                (_("Type"), str(new_channel.type).title(), True),
                (_("Created by"), self.emb.userline(perp) if perp else "—", True),
                (_("Reason"), str(reason) if reason else "—", False),
            ])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` {typ} channel created {perp} {channel}").format(
                e=es.emoji or "🧩",
                t=datetime.utcnow().strftime("%H:%M:%S"),
                typ=str(new_channel.type).title(),
                perp=_("by {p}").format(p=perp) if perp else "",
                channel=new_channel.mention,
            ))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, old_channel: discord.abc.GuildChannel):
        guild = old_channel.guild
        es = self._es(guild, Event.CHANNEL_DELETE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.CHANNEL_DELETE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        perp, reason = None, None
        try:
            if ch.permissions_for(guild.me).view_audit_log:
                async for log in guild.audit_logs(limit=2, action=discord.AuditLogAction.channel_delete):
                    if log.target.id == old_channel.id:
                        perp, reason = log.user, log.reason
                        break
        except Exception:
            pass

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.CHANNEL_DELETE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Channel Deleted"))
            self.emb.add_fields(emb, [
                (_("Name"), f"{old_channel.name} ({old_channel.id})", True),
                (_("Type"), str(old_channel.type).title(), True),
                (_("Deleted by"), self.emb.userline(perp) if perp else "—", True),
                (_("Reason"), str(reason) if reason else "—", False),
            ])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` {typ} channel deleted {perp} #{name} ({cid})").format(
                e=es.emoji or "🧩",
                t=datetime.utcnow().strftime("%H:%M:%S"),
                typ=str(old_channel.type).title(),
                perp=_("by {p}").format(p=perp) if perp else "",
                name=old_channel.name, cid=old_channel.id,
            ))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        guild = before.guild
        es = self._es(guild, Event.CHANNEL_CHANGE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.CHANNEL_CHANGE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        fields = []
        def add_change(name, b, a):
            if b != a:
                fields.append((_("Before ") + name, str(b) if b != "" else "None", True))
                fields.append((_("After ") + name, str(a) if a != "" else "None", True))

        typ = str(after.type).title()
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            add_change(_("Name:"), before.name, after.name)
            add_change(_("Topic:"), before.topic, after.topic)
            add_change(_("Category:"), before.category, after.category)
            add_change(_("Slowmode:"), before.slowmode_delay, after.slowmode_delay)
            if before.is_nsfw() != after.is_nsfw():
                add_change("NSFW", before.is_nsfw(), after.is_nsfw())
        if isinstance(before, discord.VoiceChannel) and isinstance(after, discord.VoiceChannel):
            add_change(_("Name:"), before.name, after.name)
            add_change(_("Position:"), before.position, after.position)
            add_change(_("Category:"), before.category, after.category)
            add_change(_("Bitrate:"), before.bitrate, after.bitrate)
            add_change(_("User limit:"), before.user_limit, after.user_limit)

        if not fields:
            return
        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.CHANNEL_CHANGE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Channel Updated"))
            self.emb.add_fields(emb, [(_("Channel"), after.mention, True), (_("Type"), typ, True), ("—", "—", True)])
            for f in fields:
                self.emb.add_fields(emb, [f])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Updated channel {c}").format(
                e=es.emoji or "🧩", t=datetime.utcnow().strftime("%H:%M:%S"), c=before.name,
            ))

    def _role_perm_diff(self, before: discord.Role, after: discord.Role) -> str:
        names = [p for p in dir(before.permissions) if not p.startswith("_") and isinstance(getattr(before.permissions, p), bool)]
        lines = []
        for n in names:
            if getattr(before.permissions, n) != getattr(after.permissions, n):
                lines.append(f"{n} → {getattr(after.permissions, n)}")
        return "\n".join(lines)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        guild = before.guild
        es = self._es(guild, Event.ROLE_CHANGE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.ROLE_CHANGE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        colour = await self._event_colour(guild, Event.ROLE_CHANGE, role=after)
        fields = []
        def add(name, b, a):
            if b != a:
                fields.append((_("Before ") + name, str(b), True))
                fields.append((_("After ") + name, str(a), True))
        add(_("Name:"), before.name, after.name)
        add(_("Colour:"), before.colour, after.colour)
        add(_("Mentionable:"), before.mentionable, after.mentionable)
        add(_("Is Hoisted:"), before.hoist, after.hoist)
        perm_diff = self._role_perm_diff(before, after)
        if perm_diff:
            fields.append((_('Permissions'), perm_diff[:1024], False))
        if not fields:
            return
        if embed_links:
            emb = self.emb.base(colour=colour, ts=datetime.utcnow())
            self.emb.author(emb, title=_("Role Updated"))
            self.emb.add_fields(emb, [(_("Role"), after.mention if after != guild.default_role else "@everyone", True)])
            for f in fields:
                self.emb.add_fields(emb, [f])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Updated role **{r}**").format(e=es.emoji or "🏳️", t=datetime.utcnow().strftime("%H:%M:%S"), r=before.name))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        guild = role.guild
        es = self._es(guild, Event.ROLE_CREATE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.ROLE_CREATE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.ROLE_CREATE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Role Created"))
            self.emb.add_fields(emb, [(_("Role"), role.mention, True)])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Role created {r}").format(e=es.emoji or "🏳️", t=datetime.utcnow().strftime("%H:%M:%S"), r=role.name))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild = role.guild
        es = self._es(guild, Event.ROLE_DELETE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.ROLE_DELETE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.ROLE_DELETE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Role Deleted"))
            self.emb.add_fields(emb, [(_("Role"), f"{role.name} ({role.id})", True)])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Role deleted **{r}**").format(e=es.emoji or "🏳️", t=datetime.utcnow().strftime("%H:%M:%S"), r=role.name))

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        guild = after
        es = self._es(guild, Event.GUILD_CHANGE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.GUILD_CHANGE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        changes = []
        def add(name, b, a):
            if b != a:
                changes.append((_("Before ") + name, str(b), True))
                changes.append((_("After ") + name, str(a), True))
        add(_("Name:"), before.name, after.name)
        add(_("Region:"), getattr(before, "region", None), getattr(after, "region", None))
        add(_("AFK Timeout:"), before.afk_timeout, after.afk_timeout)
        add(_("AFK Channel:"), before.afk_channel, after.afk_channel)
        add(_("Server Icon:"), bool(before.icon), bool(after.icon))
        add(_("Server Owner:"), before.owner, after.owner)
        add(_("Splash Image:"), bool(before.splash), bool(after.splash))
        add(_("Welcome message channel:"), before.system_channel, after.system_channel)
        add(_("Verification Level:"), before.verification_level, after.verification_level)
        if not changes:
            return

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.GUILD_CHANGE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Guild Updated"))
            for f in changes:
                self.emb.add_fields(emb, [f])
            icon = after.icon
            if icon:
                emb.set_thumbnail(url=icon.url)
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Guild updated").format(e=es.emoji or "🛠️", t=datetime.utcnow().strftime("%H:%M:%S")))

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        es = self._es(guild, Event.EMOJI_CHANGE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.EMOJI_CHANGE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        b, a = set(before), set(after)
        added = list(a - b)
        removed = list(b - a)
        changed = []
        for em in after:
            for old in before:
                if em.id == old.id and (em.name != old.name or set(em.roles) != set(old.roles)):
                    changed.append((old, em))
                    break
        if not (added or removed or changed):
            return

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.EMOJI_CHANGE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Emojis Updated"))
            if added:
                emb.add_field(name=_("Added"), value=" ".join(str(e) for e in added)[:1024], inline=False)
            if removed:
                emb.add_field(name=_("Removed"), value=" ".join(f"`{e}`" for e in removed)[:1024], inline=False)
            for (old, em) in changed:
                txt = ""
                if old.name != em.name:
                    txt += _("Renamed from **{a}** to **{b}**\n").format(a=old.name, b=em.name)
                if set(old.roles) != set(em.roles):
                    if em.roles:
                        txt += _("Restricted to: ") + ", ".join(r.mention for r in em.roles)
                    else:
                        txt += _("Changed to unrestricted.")
                emb.add_field(name=str(em), value=txt or "—", inline=False)
            await ch.send(embed=emb)
        else:
            lines = [f"{es.emoji or '😶‍🌫️'} `{datetime.utcnow():%H:%M:%S}` " + _("Updated Server Emojis")]
            if added:
                lines.append(_("Added: ") + ", ".join(f"{e} `{e}`" for e in added))
            if removed:
                lines.append(_("Removed: ") + ", ".join(f"`{e}`" for e in removed))
            for (old, em) in changed:
                bit = f"{em} `{em}`"
                if old.name != em.name:
                    bit += _(" renamed from ") + old.name + _(" to ") + em.name
                lines.append(bit)
            await ch.send("\n".join(lines)[:2000])

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        es = self._es(guild, Event.VOICE_CHANGE)
        if not es.enabled: return
        if member.bot: return
        ch = await self._modlog_channel(guild, Event.VOICE_CHANGE)
        if not ch: return
        if after.channel and self._ignored(guild, after.channel): return
        if before.channel and self._ignored(guild, before.channel): return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        change = None; desc = ""
        if before.deaf != after.deaf:
            change = "deaf"; desc = (member.mention + _(" was deafened.")) if after.deaf else (member.mention + _(" was undeafened."))
        elif before.mute != after.mute:
            change = "mute"; desc = (member.mention + _(" was muted.")) if after.mute else (member.mention + _(" was unmuted."))
        elif before.channel != after.channel:
            change = "channel"
            if before.channel is None:
                desc = member.mention + _(" has joined ") + inline(after.channel.name)
            elif after.channel is None:
                desc = member.mention + _(" has left ") + inline(before.channel.name)
            else:
                desc = member.mention + _(" moved from ") + inline(before.channel.name) + _(" to ") + inline(after.channel.name)
        if not change:
            return

        perp, reason = None, None
        try:
            if ch.permissions_for(guild.me).view_audit_log and change in {"deaf","mute","channel"}:
                async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                    is_change = getattr(log.after, change, None)
                    if log.target.id == member.id and is_change:
                        perp, reason = log.user, log.reason
                        break
        except Exception:
            pass

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.VOICE_CHANGE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Voice State Update"))
            self.emb.add_fields(emb, [
                (_("Member"), self.emb.userline(member), True),
                (_("Change"), desc, False),
                (_("Updated by"), self.emb.userline(perp) if perp else "—", True),
                (_("Reason"), str(reason) if reason else "—", True),
            ])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Updated Voice State for **{m}** (`{i}`)\n{d}").format(
                e=es.emoji or "🎤",
                t=datetime.utcnow().strftime("%H:%M:%S"),
                m=member, i=member.id, d=desc,
            ))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = before.guild
        es = self._es(guild, Event.USER_CHANGE)
        if not es.enabled: return
        if after.bot and not (es.bots or False): return
        ch = await self._modlog_channel(guild, Event.USER_CHANGE)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        fields = []; worth = False
        if (es.nicknames or True) and before.nick != after.nick:
            worth = True
            fields.append((_("Before Nickname"), str(before.nick), True))
            fields.append((_("After Nickname"), str(after.nick), True))
        if before.roles != after.roles:
            worth = True
            b = set(before.roles); a = set(after.roles)
            removed = [r for r in (b - a) if r.name != "@everyone"]
            added   = [r for r in (a - b) if r.name != "@everyone"]
            if added:
                fields.append((_("Roles Added"), ", ".join(r.mention for r in added), False))
            if removed:
                fields.append((_("Roles Removed"), ", ".join(r.mention for r in removed), False))
        if not worth: return

        perp, reason = None, None
        try:
            if ch.permissions_for(guild.me).view_audit_log:
                async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
                    if log.target.id == before.id:
                        perp, reason = log.user, log.reason
                        break
                if not perp:
                    async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                        if log.target.id == before.id:
                            perp, reason = log.user, log.reason
                            break
        except Exception:
            pass

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.USER_CHANGE), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Member Updated"), icon_url=before.display_avatar.url)
            self.emb.add_fields(emb, [(_("Member"), self.emb.userline(before), True)])
            for f in fields:
                self.emb.add_fields(emb, [f])
            if perp or reason:
                self.emb.add_fields(emb, [
                    (_("Updated by"), self.emb.userline(perp) if perp else "—", True),
                    (_("Reason"), str(reason) if reason else "—", True),
                ])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Member updated **{m}** (`{i}`)").format(
                e=es.emoji or "👨‍🔧", t=datetime.utcnow().strftime("%H:%M:%S"), m=before, i=before.id
            ))

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild
        # keep snapshot updated
        gs = self._gs(guild)
        created_at = getattr(invite, "created_at", datetime.utcnow())
        inviter = getattr(invite, "inviter", discord.Object(id=0))
        channel = getattr(invite, "channel", discord.Object(id=0))
        gs.invite_links[invite.code] = {
            "uses": getattr(invite, "uses", 0),
            "max_age": getattr(invite, "max_age", None),
            "created_at": created_at.timestamp(),
            "max_uses": getattr(invite, "max_uses", None),
            "temporary": getattr(invite, "temporary", False),
            "inviter": inviter.id,
            "channel": channel.id,
        }
        await self.config.guild(guild).invite_links.set(gs.invite_links)

        es = self._es(guild, Event.INVITE_CREATED)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.INVITE_CREATED)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.INVITE_CREATED), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Invite Created"))
            self.emb.add_fields(emb, [
                (_("Code:"), invite.code, True),
                (_("Inviter:"), getattr(invite.inviter, "mention", "—"), True),
                (_("Channel:"), getattr(invite.channel, "mention", "—"), True),
                (_("Max Uses:"), str(getattr(invite, "max_uses", "—")), True),
            ])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Invite created code: {c}").format(e=es.emoji or "🔗", t=datetime.utcnow().strftime("%H:%M:%S"), c=invite.code))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild = invite.guild
        es = self._es(guild, Event.INVITE_DELETED)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.INVITE_DELETED)
        if not ch: return
        embed_links = ch.permissions_for(guild.me).embed_links and es.embed

        if embed_links:
            emb = self.emb.base(colour=await self._event_colour(guild, Event.INVITE_DELETED), ts=datetime.utcnow())
            self.emb.author(emb, title=_("Invite Deleted"))
            self.emb.add_fields(emb, [
                (_("Code:"), invite.code, True),
                (_("Inviter:"), getattr(invite.inviter, "mention", "—"), True),
                (_("Channel:"), getattr(invite.channel, "mention", "—"), True),
                (_("Used:"), str(getattr(invite, "uses", "—")), True),
            ])
            await ch.send(embed=emb)
        else:
            await ch.send(_("{e} `{t}` Invite deleted code: {c}").format(e=es.emoji or "⛓️", t=datetime.utcnow().strftime("%H:%M:%S"), c=invite.code))

    # AutoMod rule lifecycle + execution
    @commands.Cog.listener()
    async def on_automod_rule_create(self, rule: discord.AutoModRule):
        guild = rule.guild
        es = self._es(guild, Event.AUTOMOD_RULE_CREATE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.AUTOMOD_RULE_CREATE)
        if not ch: return
        emb = self.emb.base(colour=await self._event_colour(guild, Event.AUTOMOD_RULE_CREATE), ts=datetime.utcnow())
        self.emb.author(emb, title=_("AutoMod: Rule Created"))
        self.emb.add_fields(emb, [
            (_("Name"), rule.name, True),
            (_("Enabled"), str(rule.enabled), True),
            (_("Event Type"), str(rule.event_type), True),
            (_("Trigger Type"), str(getattr(rule.trigger, 'type', 'unknown')), True),
            (_("Exempt Roles"), ", ".join(r.mention for r in rule.exempt_roles) or "—", False),
            (_("Exempt Channels"), ", ".join(c.mention for c in rule.exempt_channels) or "—", False),
        ])
        self.emb.footer_ids(emb, extra=f"Rule ID: {rule.id}")
        await ch.send(embed=emb)

    @commands.Cog.listener()
    async def on_automod_rule_update(self, rule: discord.AutoModRule):
        guild = rule.guild
        es = self._es(guild, Event.AUTOMOD_RULE_UPDATE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.AUTOMOD_RULE_UPDATE)
        if not ch: return
        emb = self.emb.base(colour=await self._event_colour(guild, Event.AUTOMOD_RULE_UPDATE), ts=datetime.utcnow())
        self.emb.author(emb, title=_("AutoMod: Rule Updated"))
        self.emb.add_fields(emb, [
            (_("Name"), rule.name, True),
            (_("Enabled"), str(rule.enabled), True),
            (_("Trigger Type"), str(getattr(rule.trigger, 'type', 'unknown')), True),
        ])
        self.emb.footer_ids(emb, extra=f"Rule ID: {rule.id}")
        await ch.send(embed=emb)

    @commands.Cog.listener()
    async def on_automod_rule_delete(self, rule: discord.AutoModRule):
        guild = rule.guild
        es = self._es(guild, Event.AUTOMOD_RULE_DELETE)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.AUTOMOD_RULE_DELETE)
        if not ch: return
        emb = self.emb.base(colour=await self._event_colour(guild, Event.AUTOMOD_RULE_DELETE), ts=datetime.utcnow())
        self.emb.author(emb, title=_("AutoMod: Rule Deleted"))
        self.emb.add_fields(emb, [
            (_("Name"), rule.name, True),
            (_("Trigger Type"), str(getattr(rule.trigger, 'type', 'unknown')), True),
        ])
        self.emb.footer_ids(emb, extra=f"Rule ID: {rule.id}")
        await ch.send(embed=emb)

    @commands.Cog.listener()
    async def on_automod_action_execution(self, payload: discord.AutoModAction):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild: return
        es = self._es(guild, Event.AUTOMOD_ACTION)
        if not es.enabled: return
        ch = await self._modlog_channel(guild, Event.AUTOMOD_ACTION)
        if not ch: return

        user = guild.get_member(payload.user_id) or (await self.bot.fetch_user(payload.user_id))
        channel = getattr(guild, "get_channel_or_thread", guild.get_channel)(payload.channel_id) if payload.channel_id else None
        colour = await self._event_colour(guild, Event.AUTOMOD_ACTION)

        emb = self.emb.base(colour=colour, ts=datetime.utcnow())
        self.emb.author(emb, title=_("AutoMod: Action Executed"), icon_url=getattr(user, "display_avatar", None).url if user else None)

        action_name = getattr(payload.action, "type", "Unknown")
        jump = ""
        if payload.message_id and channel:
            try:
                msg = await channel.fetch_message(payload.message_id)
                jump = f"[Jump]({msg.jump_url})"
            except Exception:
                pass

        fields = [
            (_("User"), self.emb.userline(user) if user else f"`{payload.user_id}`", True),
            (_("Channel"), self.emb.ch_mention(channel) if channel else "—", True),
            (_("Action"), str(action_name), True),
            (_("Matched Keyword"), payload.matched_keyword or "—", True),
            (_("Matched Content"), f"```\n{(payload.matched_content or '')[:900]}\n```" if payload.matched_content else "—", False),
            (_("Original Content"), f"```\n{(payload.content or '')[:900]}\n```" if payload.content else "—", False),
            (_("Jump"), jump or "—", True),
        ]
        self.emb.add_fields(emb, fields)
        self.emb.footer_ids(emb, user=user, extra=f"Rule ID: {payload.rule_id}")
        await ch.send(embed=emb)
