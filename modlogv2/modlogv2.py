import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Union, List, Sequence, Callable

import discord
from discord.ext import commands

from redbot.core import checks, commands as redcommands, Config, modlog, VersionInfo, version_info
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list, inline, escape

_ = Translator("ModLogV2", __file__)
logger = logging.getLogger("red.modlogv2")

GuildChannel = Union[discord.abc.GuildChannel, discord.Thread]

# -------------------------
# Event declarations
# -------------------------

class Event(Enum):
    MESSAGE_EDIT = "message_edit"
    MESSAGE_DELETE = "message_delete"
    USER_CHANGE = "user_change"
    ROLE_CHANGE = "role_change"
    ROLE_CREATE = "role_create"
    ROLE_DELETE = "role_delete"
    VOICE_CHANGE = "voice_change"
    USER_JOIN = "user_join"
    USER_LEFT = "user_left"
    CHANNEL_CHANGE = "channel_change"
    CHANNEL_CREATE = "channel_create"
    CHANNEL_DELETE = "channel_delete"
    GUILD_CHANGE = "guild_change"
    EMOJI_CHANGE = "emoji_change"
    COMMANDS_USED = "commands_used"
    INVITE_CREATED = "invite_created"
    INVITE_DELETED = "invite_deleted"

    @classmethod
    def from_str(cls, s: str) -> "Event":
        s = s.lower()
        for e in cls:
            if e.value == s:
                return e
        raise ValueError(f"Unknown event: {s}")


@dataclass
class EventSettings:
    enabled: bool = False
    channel: Optional[int] = None
    colour: Optional[int] = None
    embed: bool = True
    emoji: str = ""
    # extras per-event
    # message_edit
    bots: Optional[bool] = None  # for edit/delete and user_change
    # message_delete
    bulk_enabled: Optional[bool] = None
    bulk_individual: Optional[bool] = None
    cached_only: Optional[bool] = None
    # user_change
    nicknames: Optional[bool] = None
    # commands_used
    privs: Optional[List[str]] = None


def _defaults() -> Dict[Event, EventSettings]:
    # mirror original defaults from settings.py
    d: Dict[Event, EventSettings] = {
        Event.MESSAGE_EDIT: EventSettings(enabled=False, emoji="\N{MEMO}", bots=False),
        Event.MESSAGE_DELETE: EventSettings(
            enabled=False,
            emoji="\N{WASTEBASKET}\N{VARIATION SELECTOR-16}",
            bots=False,
            bulk_enabled=False,
            bulk_individual=False,
            cached_only=True,
        ),
        Event.USER_CHANGE: EventSettings(
            enabled=False,
            emoji="\N{MAN}\N{ZERO WIDTH JOINER}\N{WRENCH}",
            bots=True,
            nicknames=True,
        ),
        Event.ROLE_CHANGE: EventSettings(enabled=False, emoji="\N{WAVING WHITE FLAG}\N{VARIATION SELECTOR-16}"),
        Event.ROLE_CREATE: EventSettings(enabled=False, emoji="\N{WAVING WHITE FLAG}\N{VARIATION SELECTOR-16}"),
        Event.ROLE_DELETE: EventSettings(enabled=False, emoji="\N{WAVING WHITE FLAG}\N{VARIATION SELECTOR-16}"),
        Event.VOICE_CHANGE: EventSettings(enabled=False, emoji="\N{MICROPHONE}"),
        Event.USER_JOIN: EventSettings(enabled=False, emoji="\N{INBOX TRAY}\N{VARIATION SELECTOR-16}"),
        Event.USER_LEFT: EventSettings(enabled=False, emoji="\N{OUTBOX TRAY}\N{VARIATION SELECTOR-16}"),
        Event.CHANNEL_CHANGE: EventSettings(enabled=False, emoji=""),
        Event.CHANNEL_CREATE: EventSettings(enabled=False, emoji=""),
        Event.CHANNEL_DELETE: EventSettings(enabled=False, emoji=""),
        Event.GUILD_CHANGE: EventSettings(enabled=False, emoji="\N{HAMMER AND WRENCH}\N{VARIATION SELECTOR-16}"),
        Event.EMOJI_CHANGE: EventSettings(enabled=False, emoji=""),
        Event.COMMANDS_USED: EventSettings(enabled=False, emoji="\N{ROBOT FACE}", privs=["MOD","ADMIN","BOT_OWNER","GUILD_OWNER"]),
        Event.INVITE_CREATED: EventSettings(enabled=False, emoji=""),
        Event.INVITE_DELETED: EventSettings(enabled=False, emoji=""),
    }
    return d

# -------------------------
# Guild state
# -------------------------

@dataclass
class GuildState:
    events: Dict[Event, EventSettings] = field(default_factory=_defaults)
    ignored_channels: List[int] = field(default_factory=list)
    invite_links: Dict[str, dict] = field(default_factory=dict)

# -------------------------
# UI View
# -------------------------

class SetupView(discord.ui.View):
    def __init__(self, cog: "ModLogV2", guild: discord.Guild, author_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.author_id = author_id
        self.selected_events: List[str] = []
        self._last_channel_id: Optional[int] = None

        # Events (1..25)
        ev_opts = [discord.SelectOption(label=e.value, value=e.value) for e in Event]
        self.event_select.options = ev_opts[:25]
        self.event_select.min_values = 1
        self.event_select.max_values = min(len(self.event_select.options), 25)

        # Channels (0..1), never empty
        text_like = [
            c for c in guild.channels
            if isinstance(c, discord.TextChannel) or getattr(c, "type", None) == discord.ChannelType.news
        ]
        if text_like:
            opts = [discord.SelectOption(label=f"#{c.name}", value=str(c.id)) for c in text_like[:25]]
            self.channel_picker.options = opts
            self.channel_picker.placeholder = "Pick a modlog channel (optional)"
        else:
            self.channel_picker.options = [
                discord.SelectOption(label="Use core modlog channel (default)", value="__core__")
            ]
            self.channel_picker.placeholder = "No channels visible — will use core modlog"
        self.channel_picker.min_values = 0
        self.channel_picker.max_values = 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(_("Only the person who opened this can use it."), ephemeral=True)
            return False
        return True

    def _mutate_selected(self, mutator: Callable[[EventSettings], None]) -> int:
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

    @discord.ui.select(placeholder="Pick one or more events…", min_values=1, max_values=25, row=0)
    async def event_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_events = list(select.values)
        await interaction.response.defer()

    @discord.ui.select(placeholder="Pick a modlog channel (optional)", min_values=0, max_values=1, row=1)
    async def channel_picker(self, interaction: discord.Interaction, select: discord.ui.Select):
        if select.values and select.values[0] != "__core__":
            self._last_channel_id = int(select.values[0])
        else:
            self._last_channel_id = None
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
        def flip(es: EventSettings): es.embed = not bool(es.embed)
        n = self._mutate_selected(flip)
        await self._save_and_ack(interaction, _("Toggled embeds for {n} event(s).").format(n=n))

    @discord.ui.button(label="Set Channel", style=discord.ButtonStyle.secondary, row=3)
    async def btn_set_channel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self.selected_events:
            return await interaction.response.send_message(_("Select events first."), ephemeral=True)
        n = self._mutate_selected(lambda es: setattr(es, "channel", self._last_channel_id))
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

# -------------------------
# Cog
# -------------------------

@cog_i18n(_)
class ModLogV2(commands.Cog):
    """Modern, nicer modlogs with a setup UI and quickstart."""

    __author__ = ["Pat+ChatGPT"]
    __version__ = "2.0.0"

    def __init__(self, bot: Red):
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, 0xA11CE55, force_registration=True)
        self.config.register_global(version=self.__version__)
        # per-guild blob
        self.config.register_guild(
            events={}, ignored_channels=[], invite_links={}
        )
        self._cache: Dict[int, GuildState] = {}
        self._invite_task: Optional[asyncio.Task] = None

    # -------- lifecycle --------

    async def cog_load(self) -> None:
        await self._load_all()
        if self._invite_task is None or self._invite_task.done():
            try:
                self._invite_task = asyncio.create_task(self._invite_loop())
            except RuntimeError:
                logger.exception("Failed to start invite loop")
                self._invite_task = None

    def cog_unload(self) -> None:
        if self._invite_task:
            self._invite_task.cancel()

    async def red_delete_data_for_user(self, **kwargs):
        return

    # -------- config helpers --------

    async def _load_all(self):
        all_guild = await self.config.all_guilds()
        for gid, blob in all_guild.items():
            gs = GuildState()
            # load events dict
            defaults = _defaults()
            stored = blob.get("events") or {}
            for ev in Event:
                raw = stored.get(ev.value)
                if raw:
                    es = EventSettings(**{**defaults[ev].__dict__, **raw})
                else:
                    es = defaults[ev]
                gs.events[ev] = es
            gs.ignored_channels = blob.get("ignored_channels", [])
            gs.invite_links = blob.get("invite_links", {})
            self._cache[gid] = gs

        # bump version if needed
        stored_version = await self.config.version()
        if stored_version != self.__version__:
            await self.config.version.set(self.__version__)
            # also rewrite guild structures to be sure
            for gid, gs in self._cache.items():
                await self._save(discord.Object(id=gid))  # type: ignore

    async def _save(self, guild: Union[discord.Guild, discord.Object]):
        gid = guild.id
        gs = self._cache.get(gid) or GuildState()
        payload = {
            "events": {e.value: gs.events[e].__dict__ for e in Event},
            "ignored_channels": gs.ignored_channels,
            "invite_links": gs.invite_links,
        }
        await self.config.guild_from_id(gid).set(payload)

    def _gs(self, guild: discord.Guild) -> GuildState:
        if guild.id not in self._cache:
            self._cache[guild.id] = GuildState()
        return self._cache[guild.id]

    # -------- utility --------

    async def _get_embed_colour(self, channel: discord.abc.Messageable, guild: discord.Guild) -> discord.Colour:
        try:
            if await self.bot.db.guild(guild).use_bot_color():
                return guild.me.colour
            else:
                return await self.bot.db.color()
        except AttributeError:
            # Red fallback
            return await self.bot.get_embed_colour(channel)

    async def _event_colour(self, guild: discord.Guild, ev: Event, changed_role: Optional[discord.Role] = None) -> discord.Colour:
        defaults = {
            Event.MESSAGE_EDIT: discord.Colour.orange(),
            Event.MESSAGE_DELETE: discord.Colour.dark_red(),
            Event.USER_CHANGE: discord.Colour.greyple(),
            Event.ROLE_CHANGE: changed_role.colour if changed_role else discord.Colour.blue(),
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
            Event.COMMANDS_USED: await self._get_embed_colour(guild.text_channels[0] if guild.text_channels else guild, guild),
            Event.INVITE_CREATED: discord.Colour.blurple(),
            Event.INVITE_DELETED: discord.Colour.blurple(),
        }
        es = self._gs(guild).events[ev]
        return discord.Colour(es.colour) if es.colour else defaults[ev]

    async def _modlog_channel(self, guild: discord.Guild, ev: Event) -> GuildChannel:
        gs = self._gs(guild)
        target: Optional[GuildChannel] = None
        es = gs.events[ev]
        if es.channel:
            get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
            target = get_channel(es.channel)  # type: ignore
        if target is None:
            try:
                target = await modlog.get_modlog_channel(guild)
            except RuntimeError:
                raise RuntimeError("No Modlog set")
        if not target.permissions_for(guild.me).send_messages:
            raise RuntimeError("No permission to send messages in modlog channel")
        return target

    async def _is_ignored(self, guild: discord.Guild, channel: Optional[GuildChannel]) -> bool:
        if channel is None:
            return False
        gs = self._gs(guild)
        ignored = gs.ignored_channels
        cid = getattr(channel, "id", None)
        if cid in ignored:
            return True
        cat = getattr(channel, "category", None)
        if cat and getattr(cat, "id", None) in ignored:
            return True
        parent = getattr(channel, "parent", None)
        if parent and getattr(parent, "id", None) in ignored:
            return True
        if parent:
            pcat = getattr(parent, "category", None)
            if pcat and getattr(pcat, "id", None) in ignored:
                return True
        return False

    @staticmethod
    def _chan_mention(channel_id: int, channel: Optional[GuildChannel]) -> str:
        return channel.mention if channel else f"<#{channel_id}>"

    @staticmethod
    def _guild_icon_url(guild: discord.Guild) -> Optional[str]:
        return guild.icon.url if guild.icon else None

    async def _settings_text(self, guild: discord.Guild) -> str:
        gs = self._gs(guild)
        try:
            core = self.bot.get_cog("ModLog")
            # just check existence; channel is fetched below
        except Exception:
            core = None
        try:
            modch = (await modlog.get_modlog_channel(guild)).mention
        except Exception:
            modch = "Not Set"

        lines = [f"**Settings for {guild.name}**\nCore Modlog Channel: {modch}\n"]
        for ev in Event:
            es = gs.events[ev]
            ch = guild.get_channel(es.channel) if es.channel else None
            lines.append(f"• `{ev.value}`: **{es.enabled}**"
                         + (f" → {ch.mention}" if ch else ""))
        if gs.ignored_channels:
            chs = ", ".join(self._chan_mention(cid, guild.get_channel(cid)) for cid in gs.ignored_channels)
            lines.append(f"\nIgnored Channels: {chs}")
        return "\n".join(lines)

    # -------------------------
    # Commands
    # -------------------------

    @redcommands.group(name="modlogv2", invoke_without_command=True, aliases=["mlv2"])
    @commands.guild_only()
    async def grp(self, ctx: redcommands.Context):
        """Show ModLogV2 settings"""
        await ctx.maybe_send_embed(self._settings_text(ctx.guild))

    @grp.command(name="setup")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def setup(self, ctx: redcommands.Context):
        """Interactive setup UI (with safe fallback)."""
        try:
            view = SetupView(self, ctx.guild, ctx.author.id)
            msg = (
                "**ModLogV2 Setup**\n"
                "1) Use the dropdown to select one or more events.\n"
                "2) (Optional) pick a channel.\n"
                "3) Click a button (Enable/Disable/Embeds/Set/Reset).\n"
                "Tip: Run this again anytime."
            )
            await ctx.send(msg, view=view)
        except Exception as e:
            await ctx.send(
                "⚠️ I couldn't open the interactive setup here.\n"
                f"```{type(e).__name__}: {e}```\n"
                "You can still set things up quickly:\n"
                f"• Set core modlog channel: `{ctx.clean_prefix}modlogset modlog #{ctx.channel.name}`\n"
                f"• Enable key events: `{ctx.clean_prefix}modlogv2 toggle true message_edit message_delete user_join user_left`\n"
                f"• (Optional) pin here: `{ctx.clean_prefix}modlogv2 channel {ctx.channel.mention} message_edit message_delete user_join user_left`\n"
                f"• Review: `{ctx.clean_prefix}modlogv2`\n\n"
                f"Or run `{ctx.clean_prefix}modlogv2 quickstart` for a one-command setup."
            )

    @grp.command(name="quickstart")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def quickstart(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        """One-shot setup enabling common events in this channel (or a specified channel)."""
        ch = channel or ctx.channel
        gs = self._gs(ctx.guild)

        defaults = [
            Event.MESSAGE_EDIT,
            Event.MESSAGE_DELETE,
            Event.USER_JOIN,
            Event.USER_LEFT,
            Event.COMMANDS_USED,
        ]
        for ev in defaults:
            es = gs.events[ev]
            es.enabled = True
            es.embed = True
            es.channel = ch.id

        gs.events[Event.MESSAGE_DELETE].bulk_enabled = True
        gs.events[Event.MESSAGE_DELETE].cached_only = False

        await self._save(ctx.guild)
        await ctx.send(
            "✅ Quickstart complete.\n"
            f"Enabled: {', '.join(f'`{e.value}`' for e in defaults)} → {ch.mention}\n"
            f"Use `{ctx.clean_prefix}modlogv2` to review, or `{ctx.clean_prefix}modlogv2 setup` to tweak."
        )

    # ---- parity commands (channel/embeds/toggle etc.) ----

    def _parse_events(self, ctx: redcommands.Context, names: List[str]) -> List[Event]:
        evs = []
        for n in names:
            try:
                evs.append(Event.from_str(n))
            except Exception:
                raise commands.BadArgument(f"`{n}` is not a valid event.")
        if not evs:
            raise commands.BadArgument(_("You must provide which events should be included."))
        return evs

    @grp.command(name="colour", aliases=["color"])
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_colour(self, ctx: redcommands.Context, colour: Optional[discord.Colour], *events: str):
        """Set custom colour for events (hex or named colour)."""
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        val = colour.value if colour else None
        for ev in evs:
            gs.events[ev].colour = val
        await self._save(ctx.guild)
        await ctx.send(_("Set {ev} to {c}").format(ev=humanize_list([e.value for e in evs]), c=str(colour)))

    @grp.command(name="embeds")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_embeds(self, ctx: redcommands.Context, set_to: bool, *events: str):
        """Enable/disable embeds for events."""
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].embed = set_to
        await self._save(ctx.guild)
        await ctx.send(_("{ev} embed logs set to {v}").format(ev=humanize_list([e.value for e in evs]), v=str(set_to)))

    @grp.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_toggle(self, ctx: redcommands.Context, set_to: bool, *events: str):
        """Turn on/off specific events."""
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].enabled = set_to
        await self._save(ctx.guild)
        await ctx.send(_("{ev} logs set to {v}").format(ev=humanize_list([e.value for e in evs]), v=str(set_to)))

    @grp.command(name="channel")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_channel(self, ctx: redcommands.Context, channel: discord.TextChannel, *events: str):
        """Set per-event channel."""
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].channel = channel.id
        await self._save(ctx.guild)
        await ctx.send(_("{ev} logs will go to {ch}").format(ev=humanize_list([e.value for e in evs]), ch=channel.mention))

    @grp.command(name="resetchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_resetchannel(self, ctx: redcommands.Context, *events: str):
        """Reset events to the core modlog channel."""
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].channel = None
        await self._save(ctx.guild)
        await ctx.send(_("Reset channel for {ev}.").format(ev=humanize_list([e.value for e in evs])))

    @grp.command(name="all")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_all(self, ctx: redcommands.Context, set_to: bool):
        """Turn all logging options on/off."""
        gs = self._gs(ctx.guild)
        for ev in Event:
            gs.events[ev].enabled = set_to
        await self._save(ctx.guild)
        await ctx.maybe_send_embed(self._settings_text(ctx.guild))

    @grp.command(name="botedits")
    async def cmd_botedits(self, ctx: redcommands.Context):
        """Toggle message edit notifications for bots."""
        gs = self._gs(ctx.guild)
        es = gs.events[Event.MESSAGE_EDIT]
        es.bots = not bool(es.bots)
        await self._save(ctx.guild)
        await ctx.send(_("Bots edited messages ") + ("enabled" if es.bots else "disabled"))

    @grp.command(name="botdeletes")
    async def cmd_botdeletes(self, ctx: redcommands.Context):
        """Toggle message delete notifications for bots (cached)."""
        gs = self._gs(ctx.guild)
        es = gs.events[Event.MESSAGE_DELETE]
        es.bots = not bool(es.bots)
        await self._save(ctx.guild)
        await ctx.send(_("Bot delete logs ") + ("enabled" if es.bots else "disabled"))

    @grp.group(name="delete")
    async def grp_delete(self, ctx: redcommands.Context):
        """Delete logging settings."""
        pass

    @grp_delete.command(name="bulkdelete")
    async def cmd_bulk(self, ctx: redcommands.Context):
        gs = self._gs(ctx.guild)
        es = gs.events[Event.MESSAGE_DELETE]
        es.bulk_enabled = not bool(es.bulk_enabled)
        await self._save(ctx.guild)
        await ctx.send(_("Bulk message delete logs ") + ("enabled" if es.bulk_enabled else "disabled"))

    @grp_delete.command(name="individual")
    async def cmd_bulk_indiv(self, ctx: redcommands.Context):
        gs = self._gs(ctx.guild)
        es = gs.events[Event.MESSAGE_DELETE]
        es.bulk_individual = not bool(es.bulk_individual)
        await self._save(ctx.guild)
        await ctx.send(_("Individual message delete logs for bulk message delete ") + ("enabled" if es.bulk_individual else "disabled"))

    @grp_delete.command(name="cachedonly")
    async def cmd_cached_only(self, ctx: redcommands.Context):
        gs = self._gs(ctx.guild)
        es = gs.events[Event.MESSAGE_DELETE]
        es.cached_only = not bool(es.cached_only)
        await self._save(ctx.guild)
        await ctx.send(_("Delete logs for non-cached messages ") + ("disabled" if es.cached_only else "enabled"))

    @grp.command(name="botchange")
    async def cmd_user_botchange(self, ctx: redcommands.Context):
        gs = self._gs(ctx.guild)
        es = gs.events[Event.USER_CHANGE]
        es.bots = not bool(es.bots)
        await self._save(ctx.guild)
        await ctx.send(_("Bots will be tracked in user change logs.") if es.bots else _("Bots will no longer be tracked in user change logs."))

    @grp.command(name="nickname", aliases=["nicknames"])
    async def cmd_user_nicks(self, ctx: redcommands.Context):
        gs = self._gs(ctx.guild)
        es = gs.events[Event.USER_CHANGE]
        es.nicknames = not bool(es.nicknames)
        await self._save(ctx.guild)
        await ctx.send(_("Nicknames will be tracked in user change logs.") if es.nicknames else _("Nicknames will no longer be tracked in user change logs."))

    @grp.command(name="commandlevel", aliases=["commandslevel"])
    async def cmd_command_level(self, ctx: redcommands.Context, *levels: str):
        """Set which privilege levels to log for commands (MOD ADMIN BOT_OWNER GUILD_OWNER NONE)."""
        if not levels:
            return await ctx.send_help()
        allowed = {"MOD","ADMIN","BOT_OWNER","GUILD_OWNER","NONE"}
        bad = [l for l in levels if l.upper() not in allowed]
        if bad:
            raise commands.BadArgument(_("Invalid level(s): ") + ", ".join(bad))
        gs = self._gs(ctx.guild)
        gs.events[Event.COMMANDS_USED].privs = [l.upper() for l in levels]
        await self._save(ctx.guild)
        await ctx.send(_("Command logs set to: ") + humanize_list(gs.events[Event.COMMANDS_USED].privs))

    @grp.command(name="ignore")
    async def cmd_ignore(self, ctx: redcommands.Context, channel: Union[
        discord.TextChannel, discord.CategoryChannel, discord.VoiceChannel,
        discord.StageChannel, discord.ForumChannel, discord.Thread]):
        gs = self._gs(ctx.guild)
        if channel.id not in gs.ignored_channels:
            gs.ignored_channels.append(channel.id)
            await self._save(ctx.guild)
            await ctx.send(_(" Now ignoring events in ") + channel.mention)
        else:
            await ctx.send(channel.mention + _(" is already being ignored."))

    @grp.command(name="unignore")
    async def cmd_unignore(self, ctx: redcommands.Context, channel: Union[
        discord.TextChannel, discord.CategoryChannel, discord.VoiceChannel,
        discord.StageChannel, discord.ForumChannel, discord.Thread]):
        gs = self._gs(ctx.guild)
        if channel.id in gs.ignored_channels:
            gs.ignored_channels.remove(channel.id)
            await self._save(ctx.guild)
            await ctx.send(_(" Now tracking events in ") + channel.mention)
        else:
            await ctx.send(channel.mention + _(" is not being ignored."))

    # -------------------------
    # Listeners (core parity set)
    # -------------------------

    async def _can_send_embed(self, channel: discord.TextChannel, ev: Event, guild: discord.Guild) -> bool:
        gs = self._gs(guild)
        return channel.permissions_for(guild.me).embed_links and gs.events[ev].embed

    async def _member_can_run(self, ctx: redcommands.Context) -> bool:
        command = ctx.message.content.replace(ctx.prefix, "")
        com = ctx.bot.get_command(command)
        if com is None:
            return False
        try:
            testcontext = await ctx.bot.get_context(ctx.message, cls=redcommands.Context)
            to_check = [*reversed(com.parents)] + [com]
            can = False
            for cmd in to_check:
                can = await cmd.can_run(testcontext)
                if can is False:
                    break
        except commands.CheckFailure:
            can = False
        return can

    @commands.Cog.listener()
    async def on_command(self, ctx: redcommands.Context):
        guild = ctx.guild
        if guild is None:
            return
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        es = gs.events[Event.COMMANDS_USED]
        if not es.enabled:
            return
        if await self._is_ignored(guild, ctx.channel):
            return
        try:
            ch = await self._modlog_channel(guild, Event.COMMANDS_USED)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.COMMANDS_USED, guild)

        command = ctx.message.content.replace(ctx.prefix, "")
        try:
            privs = self.bot.get_command(command).requires.privilege_level.name
        except Exception:
            return
        wanted = set(es.privs or [])
        if privs not in wanted:
            return

        time = ctx.message.created_at
        if embed_ok:
            colour = await self._event_colour(guild, Event.COMMANDS_USED)
            e = discord.Embed(description=ctx.message.content, colour=colour, timestamp=time)
            e.add_field(name=_("Channel"), value=ctx.channel.mention)
            e.add_field(name=_("Can Run"), value=str(await self._member_can_run(ctx)))
            e.add_field(name=_("Privilege"), value=privs)
            e.set_footer(text=_("User ID: ") + str(ctx.author.id))
            e.set_author(name=f"{ctx.author} ({ctx.author.id}) - Used a Command", icon_url=ctx.author.display_avatar.url)
            await ch.send(embed=e)
        else:
            inf = (f"{es.emoji} `{time.strftime('%H:%M:%S')}` "
                   f"{ctx.author}(`{ctx.author.id}`) used `{ctx.message.content}` in {ctx.channel.mention}")
            await ch.send(inf[:2000])

    @commands.Cog.listener(name="on_raw_message_delete")
    async def on_raw_message_delete_listener(self, payload: discord.RawMessageDeleteEvent, *, check_audit_log: bool = True):
        gid = payload.guild_id
        if gid is None:
            return
        guild = self.bot.get_guild(gid)
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        es = gs.events[Event.MESSAGE_DELETE]
        if not es.enabled:
            return
        get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
        source_channel = get_channel(payload.channel_id)
        if await self._is_ignored(guild, source_channel):
            return
        try:
            ch = await self._modlog_channel(guild, Event.MESSAGE_DELETE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.MESSAGE_DELETE, guild)
        message = payload.cached_message
        if message is None:
            if es.cached_only:
                return
            mention = self._chan_mention(payload.channel_id, source_channel)
            if embed_ok:
                e = discord.Embed(description=_("*Message's content unknown.*"),
                                  colour=await self._event_colour(guild, Event.MESSAGE_DELETE))
                e.add_field(name=_("Channel"), value=mention)
                e.set_author(name=_("Deleted Message"))
                await ch.send(embed=e)
            else:
                msg = _("{emoji} `{time}` A message was deleted in {channel}").format(
                    emoji=es.emoji, time=datetime.datetime.utcnow().strftime("%H:%M:%S"), channel=mention
                )
                await ch.send(f"{msg}\n> *Message's content unknown.*")
            return

        # cached
        if message.author.bot and not es.bots:
            return
        if message.content == "" and message.attachments == []:
            return

        perp = None
        if ch.permissions_for(guild.me).view_audit_log and check_audit_log:
            async for log in guild.audit_logs(limit=2, action=discord.AuditLogAction.message_delete):
                if getattr(log.extra, "channel", None) and log.extra.channel.id == message.channel.id and log.target.id == message.author.id:
                    perp = f"{log.user}({log.user.id})"
                    break

        mention = self._chan_mention(message.channel.id, message.channel)
        time = message.created_at
        if embed_ok:
            e = discord.Embed(description=message.content, colour=await self._event_colour(guild, Event.MESSAGE_DELETE), timestamp=time)
            e.add_field(name=_("Channel"), value=mention)
            if perp:
                e.add_field(name=_("Deleted by"), value=perp)
            if message.attachments:
                files = ", ".join(a.filename for a in message.attachments)
                e.add_field(name=_("Attachments"), value=files[:1024])
            e.set_footer(text=_("User ID: ") + str(message.author.id))
            e.set_author(name=f"{message.author} ({message.author.id}) - Deleted Message", icon_url=message.author.display_avatar.url)
            await ch.send(embed=e)
        else:
            head = (_("{emoji} `{time}` A message from **{author}** (`{a_id}`) was deleted in {channel}")
                    .format(emoji=es.emoji, time=time.strftime("%H:%M:%S"), author=message.author, a_id=message.author.id, channel=mention))
            clean = escape(message.clean_content, mass_mentions=True)[: (1990 - len(head))]
            await ch.send(f"{head}\n>>> {clean}")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        gid = payload.guild_id
        if gid is None:
            return
        guild = self.bot.get_guild(gid)
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        es = gs.events[Event.MESSAGE_DELETE]
        if not es.enabled or not es.bulk_enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.MESSAGE_DELETE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.MESSAGE_DELETE, guild)
        get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
        message_channel = get_channel(payload.channel_id)
        if await self._is_ignored(guild, message_channel):
            return
        amount = len(payload.message_ids)
        mention = self._chan_mention(payload.channel_id, message_channel)
        if embed_ok:
            e = discord.Embed(description=mention, colour=await self._event_colour(guild, Event.MESSAGE_DELETE))
            e.set_author(name=_("Bulk message delete"), icon_url=self._guild_icon_url(guild) or discord.Embed.Empty)
            e.add_field(name=_("Channel"), value=mention)
            e.add_field(name=_("Messages deleted"), value=str(amount))
            await ch.send(embed=e)
        else:
            msg = _("{emoji} `{time}` Bulk message delete in {channel}, {amount} messages deleted.").format(
                emoji=es.emoji, time=datetime.datetime.utcnow().strftime("%H:%M:%S"), channel=mention, amount=amount
            )
            await ch.send(msg)
        if es.bulk_individual:
            for m in payload.cached_messages:
                fake = discord.RawMessageDeleteEvent({"id": m.id, "channel_id": payload.channel_id, "guild_id": gid})
                fake.cached_message = m
                try:
                    await self.on_raw_message_delete_listener(fake, check_audit_log=False)
                except Exception:
                    pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        guild = before.guild
        if guild is None:
            return
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        es = gs.events[Event.MESSAGE_EDIT]
        if not es.enabled:
            return
        if before.author.bot and not es.bots:
            return
        if before.content == after.content:
            return
        if await self._is_ignored(guild, after.channel):
            return
        try:
            ch = await self._modlog_channel(guild, Event.MESSAGE_EDIT)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.MESSAGE_EDIT, guild)
        if embed_ok:
            e = discord.Embed(description=before.content, colour=await self._event_colour(guild, Event.MESSAGE_EDIT), timestamp=before.created_at)
            e.add_field(name=_("After Message:"), value=f"[Click to see new message]({after.jump_url})")
            e.add_field(name=_("Channel:"), value=before.channel.mention)
            e.set_footer(text=_("User ID: ") + str(before.author.id))
            e.set_author(name=f"{before.author} ({before.author.id}) - Edited Message", icon_url=before.author.display_avatar.url)
            await ch.send(embed=e)
        else:
            msg = (_("{emoji} `{time}` **{author}** (`{a_id}`) edited a message in {channel}.\n"
                     "Before:\n> {before}\nAfter:\n> {after}")
                   .format(emoji=es.emoji, time=datetime.datetime.utcnow().strftime("%H:%M:%S"),
                           author=before.author, a_id=before.author.id, channel=before.channel.mention,
                           before=escape(before.content, mass_mentions=True),
                           after=escape(after.content, mass_mentions=True)))
            await ch.send(msg[:2000])

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        es = gs.events[Event.USER_JOIN]
        if not es.enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.USER_JOIN)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.USER_JOIN, guild)
        users = len(guild.members)
        since_created = (datetime.datetime.utcnow() - member.created_at).days
        user_created = member.created_at.strftime("%d %b %Y %H:%M")
        created_on = f"{user_created}\n({since_created} days ago)"
        if embed_ok:
            e = discord.Embed(description=member.mention, colour=await self._event_colour(guild, Event.USER_JOIN),
                              timestamp=member.joined_at or datetime.datetime.utcnow())
            e.add_field(name=_("Total Users:"), value=str(users))
            e.add_field(name=_("Account created on:"), value=created_on)
            e.set_footer(text=_("User ID: ") + str(member.id))
            e.set_author(name=f"{member} ({member.id}) has joined the server", icon_url=member.display_avatar.url)
            e.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=e)
        else:
            await ch.send(_("{emoji} `{time}` **{m}**(`{mid}`) joined. Total members: {u}")
                          .format(emoji=es.emoji, time=datetime.datetime.utcnow().strftime("%H:%M:%S"),
                                  m=member, mid=member.id, u=users))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        es = gs.events[Event.USER_LEFT]
        if not es.enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.USER_LEFT)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.USER_LEFT, guild)
        perp = None
        reason = None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, after=datetime.datetime.utcnow() + datetime.timedelta(minutes=-30), action=discord.AuditLogAction.kick):
                if log.target.id == member.id:
                    perp = log.user
                    reason = log.reason
                    break
        if embed_ok:
            e = discord.Embed(description=member.mention, colour=await self._event_colour(guild, Event.USER_LEFT), timestamp=datetime.datetime.utcnow())
            e.add_field(name=_("Total Users:"), value=str(len(guild.members)))
            if perp: e.add_field(name=_("Kicked"), value=perp.mention)
            if reason: e.add_field(name=_("Reason"), value=str(reason))
            e.set_footer(text=_("User ID: ") + str(member.id))
            e.set_author(name=f"{member} ({member.id}) has left the server", icon_url=member.display_avatar.url)
            e.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=e)
        else:
            if perp:
                await ch.send(_("{emoji} `{time}` **{m}**(`{mid}`) was kicked by {p}. Total members: {u}")
                              .format(emoji=es.emoji, time=datetime.datetime.utcnow().strftime("%H:%M:%S"),
                                      m=member, mid=member.id, p=perp, u=len(guild.members)))
            else:
                await ch.send(_("{emoji} `{time}` **{m}**(`{mid}`) left. Total members: {u}")
                              .format(emoji=es.emoji, time=datetime.datetime.utcnow().strftime("%H:%M:%S"),
                                      m=member, mid=member.id, u=len(guild.members)))

    # (Channel/Role/Guild/Emoji/Voice/Member update listeners implemented in a compact but parity way)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, new_channel: discord.abc.GuildChannel):
        guild = new_channel.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.CHANNEL_CREATE].enabled:
            return
        if await self._is_ignored(guild, new_channel):
            return
        try:
            ch = await self._modlog_channel(guild, Event.CHANNEL_CREATE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.CHANNEL_CREATE, guild)
        t = str(new_channel.type).title()
        time = datetime.datetime.utcnow()
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=2, action=discord.AuditLogAction.channel_create):
                if log.target.id == new_channel.id:
                    perp = log.user
                    reason = log.reason
                    break
        if embed_ok:
            e = discord.Embed(description=f"{new_channel.mention} {new_channel.name}", timestamp=time,
                              colour=await self._event_colour(guild, Event.CHANNEL_CREATE))
            e.set_author(name=_("{t} Channel Created {n} ({i})").format(t=t, n=new_channel.name, i=new_channel.id))
            e.add_field(name=_("Type"), value=t)
            if perp: e.add_field(name=_("Created by"), value=perp.mention)
            if reason: e.add_field(name=_("Reason"), value=reason)
            await ch.send(embed=e)
        else:
            await ch.send(_("{emoji} `{time}` {t} channel created {c}")
                          .format(emoji=gs.events[Event.CHANNEL_CREATE].emoji, time=time.strftime("%H:%M:%S"),
                                  t=t, c=new_channel.mention))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, old_channel: discord.abc.GuildChannel):
        guild = old_channel.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.CHANNEL_DELETE].enabled:
            return
        if await self._is_ignored(guild, old_channel):
            return
        try:
            ch = await self._modlog_channel(guild, Event.CHANNEL_DELETE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.CHANNEL_DELETE, guild)
        t = str(old_channel.type).title()
        time = datetime.datetime.utcnow()
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=2, action=discord.AuditLogAction.channel_delete):
                if log.target.id == old_channel.id:
                    perp = log.user
                    reason = log.reason
                    break
        if embed_ok:
            e = discord.Embed(description=old_channel.name, timestamp=time, colour=await self._event_colour(guild, Event.CHANNEL_DELETE))
            e.set_author(name=_("{t} Channel Deleted {n} ({i})").format(t=t, n=old_channel.name, i=old_channel.id))
            e.add_field(name=_("Type"), value=t)
            if perp: e.add_field(name=_("Deleted by"), value=perp.mention)
            if reason: e.add_field(name=_("Reason"), value=reason)
            await ch.send(embed=e)
        else:
            await ch.send(_("{emoji} `{time}` {t} channel deleted #{n} ({i})")
                          .format(emoji=gs.events[Event.CHANNEL_DELETE].emoji, time=time.strftime("%H:%M:%S"),
                                  t=t, n=old_channel.name, i=old_channel.id))

    async def _perm_changes_text(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel, embed_links: bool) -> str:
        p_msg = ""
        bp, ap = {}, {}
        for o, p in before.overwrites.items():
            bp[str(o.id)] = [i for i in p]
        for o, p in after.overwrites.items():
            ap[str(o.id)] = [i for i in p]
        for ent in bp:
            ent_obj = before.guild.get_role(int(ent)) or before.guild.get_member(int(ent))
            if ent not in ap:
                p_msg += (f"{ent_obj.mention if embed_links else ent_obj.name} Overwrites removed.\n")
                continue
            if ap[ent] != bp[ent]:
                a = set(ap[ent]); b = set(bp[ent])
                for diff in list(a - b):
                    p_msg += (f"{ent_obj.mention if embed_links else ent_obj.name} {diff[0]} Set to {diff[1]}\n")
        for ent in ap:
            ent_obj = after.guild.get_role(int(ent)) or after.guild.get_member(int(ent))
            if ent not in bp:
                p_msg += (f"{ent_obj.mention if embed_links else ent_obj.name} Overwrites added.\n")
        return p_msg

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        guild = before.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.CHANNEL_CHANGE].enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.CHANNEL_CHANGE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.CHANNEL_CHANGE, guild)
        t = str(after.type).title()
        time = datetime.datetime.utcnow()
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_update):
                if log.target.id == before.id:
                    perp, reason = log.user, log.reason
                    break
        e = discord.Embed(description=after.mention, timestamp=time, colour=await self._event_colour(guild, Event.CHANNEL_CREATE))
        e.set_author(name=_("{t} Channel Updated {n} ({i})").format(t=t, n=before.name, i=before.id))

        changed = False
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            for attr, name in {"name":_("Name:"), "topic":_("Topic:"), "category":_("Category:"), "slowmode_delay":_("Slowmode delay:")}.items():
                b = getattr(before, attr); a = getattr(after, attr)
                if b != a:
                    changed = True
                    e.add_field(name=_("Before ") + name, value=str(b)[:1024] or "None")
                    e.add_field(name=_("After ") + name, value=str(a)[:1024] or "None")
            if before.is_nsfw() != after.is_nsfw():
                changed = True
                e.add_field(name=_("Before NSFW"), value=str(before.is_nsfw()))
                e.add_field(name=_("After NSFW"), value=str(after.is_nsfw()))
            pmsg = await self._perm_changes_text(before, after, embed_ok)
            if pmsg:
                changed = True
                e.add_field(name=_("Permissions"), value=pmsg[:1024])
        elif isinstance(before, discord.VoiceChannel) and isinstance(after, discord.VoiceChannel):
            for attr, name in {"name":_("Name:"), "position":_("Position:"), "category":_("Category:"), "bitrate":_("Bitrate:"), "user_limit":_("User limit:")}.items():
                b = getattr(before, attr); a = getattr(after, attr)
                if b != a:
                    changed = True
                    e.add_field(name=_("Before ") + name, value=str(b))
                    e.add_field(name=_("After ") + name, value=str(a))
            pmsg = await self._perm_changes_text(before, after, embed_ok)
            if pmsg:
                changed = True
                e.add_field(name=_("Permissions"), value=pmsg[:1024])
        if perp: e.add_field(name=_("Updated by"), value=perp.mention)
        if reason: e.add_field(name=_("Reason"), value=reason)
        if changed:
            if embed_ok:
                await ch.send(embed=e)
            else:
                await ch.send(escape(_("Updated channel ") + before.name, mass_mentions=True))

    async def _role_perm_changes(self, before: discord.Role, after: discord.Role) -> str:
        perms = [
            "create_instant_invite","kick_members","ban_members","administrator","manage_channels","manage_guild",
            "add_reactions","view_audit_log","priority_speaker","read_messages","send_messages","send_tts_messages",
            "manage_messages","embed_links","attach_files","read_message_history","mention_everyone","external_emojis",
            "connect","speak","mute_members","deafen_members","move_members","use_voice_activation","change_nickname",
            "manage_nicknames","manage_roles","manage_webhooks","manage_emojis"
        ]
        pmsg = ""
        for p in perms:
            if getattr(before.permissions, p) != getattr(after.permissions, p):
                pmsg += f"{p} Set to {getattr(after.permissions, p)}\n"
        return pmsg

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        guild = before.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.ROLE_CHANGE].enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.ROLE_CHANGE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.ROLE_CHANGE, guild)
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.role_update):
                if log.target.id == before.id:
                    perp, reason = log.user, log.reason
                    break
        e = discord.Embed(description=after.mention, colour=after.colour, timestamp=datetime.datetime.utcnow())
        e.set_author(name=_("Updated {role} ({r_id}) role ").format(role=before.name, r_id=before.id))
        changed = False
        for attr, name in {"name":_("Name:"), "color":_("Colour:"), "mentionable":_("Mentionable:"), "hoist":_("Is Hoisted:")}.items():
            b = getattr(before, attr); a = getattr(after, attr)
            if b != a:
                changed = True
                e.add_field(name=_("Before ") + name, value=str(b))
                e.add_field(name=_("After ") + name, value=str(a))
        pmsg = await self._role_perm_changes(before, after)
        if pmsg:
            changed = True
            e.add_field(name=_("Permissions"), value=pmsg[:1024])
        if perp: e.add_field(name=_("Updated by"), value=perp.mention)
        if reason: e.add_field(name=_("Reason"), value=reason)
        if changed:
            if embed_ok:
                await ch.send(embed=e)
            else:
                await ch.send(_("{emoji} Updated role **{role}**").format(emoji=gs.events[Event.ROLE_CHANGE].emoji, role=before.name))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        guild = role.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.ROLE_CREATE].enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.ROLE_CREATE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.ROLE_CREATE, guild)
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.role_create):
                if log.target.id == role.id:
                    perp, reason = log.user, log.reason
                    break
        if embed_ok:
            e = discord.Embed(description=role.mention, colour=await self._event_colour(guild, Event.ROLE_CREATE), timestamp=datetime.datetime.utcnow())
            e.set_author(name=_("Role created {role} ({r_id})").format(role=role.name, r_id=role.id))
            if perp: e.add_field(name=_("Created by"), value=perp.mention)
            if reason: e.add_field(name=_("Reason"), value=reason)
            await ch.send(embed=e)
        else:
            await ch.send(_("{emoji} Role created {name}").format(emoji=gs.events[Event.ROLE_CREATE].emoji, name=role.name))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild = role.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.ROLE_DELETE].enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.ROLE_DELETE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.ROLE_DELETE, guild)
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.role_delete):
                if log.target.id == role.id:
                    perp, reason = log.user, log.reason
                    break
        if embed_ok:
            e = discord.Embed(description=role.name, timestamp=datetime.datetime.utcnow(), colour=await self._event_colour(guild, Event.ROLE_DELETE))
            e.set_author(name=_("Role deleted {role} ({r_id})").format(role=role.name, r_id=role.id))
            if perp: e.add_field(name=_("Deleted by"), value=perp.mention)
            if reason: e.add_field(name=_("Reason"), value=reason)
            await ch.send(embed=e)
        else:
            await ch.send(_("{emoji} Role deleted **{name}**").format(emoji=gs.events[Event.ROLE_DELETE].emoji, name=role.name))

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        guild = after
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.GUILD_CHANGE].enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.GUILD_CHANGE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.GUILD_CHANGE, guild)
        time = datetime.datetime.utcnow()
        e = discord.Embed(timestamp=time, colour=await self._event_colour(guild, Event.GUILD_CHANGE))
        icon = self._guild_icon_url(guild)
        e.set_author(name=_("Updated Guild"), icon_url=icon or discord.Embed.Empty)
        if icon: e.set_thumbnail(url=icon)
        changed = False
        fields = {"name":_("Name:"), "afk_timeout":_("AFK Timeout:"), "afk_channel":_("AFK Channel:"),
                  "icon":_("Server Icon:"), "owner":_("Server Owner:"), "system_channel":_("Welcome message channel:"), "verification_level":_("Verification Level:")}
        for attr, name in fields.items():
            b = getattr(before, attr, None); a = getattr(after, attr, None)
            if b != a:
                changed = True
                e.add_field(name=_("Before ") + name, value=str(b)[:1024])
                e.add_field(name=_("After ") + name, value=str(a)[:1024])
        if changed:
            if embed_ok:
                await ch.send(embed=e)
            else:
                await ch.send(_("{emoji} Guild updated").format(emoji=gs.events[Event.GUILD_CHANGE].emoji))

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: Sequence[discord.Emoji], after: Sequence[discord.Emoji]):
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.EMOJI_CHANGE].enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.EMOJI_CHANGE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.EMOJI_CHANGE, guild)
        time = datetime.datetime.utcnow()
        e = discord.Embed(description="", timestamp=time, colour=await self._event_colour(guild, Event.EMOJI_CHANGE))
        e.set_author(name=_("Updated Server Emojis"))
        msg = ""
        b = set(before); a = set(after)
        try:
            added = (a - b).pop()
        except KeyError:
            added = None
        try:
            removed = (b - a).pop()
        except KeyError:
            removed = None

        changed_pair = set((e, e.name, tuple(e.roles)) for e in after)
        changed_pair.difference_update((e, e.name, tuple(e.roles)) for e in (before + ((added,) if added else tuple())))
        try:
            changed = next(iter(changed_pair))[0]
        except Exception:
            changed = None

        action = None
        if removed:
            msg += f"`{removed}` (ID: {removed.id}) Removed from the server\n"
            e.description += msg
            action = discord.AuditLogAction.emoji_delete
        elif added:
            msg += f"{added} `{added}` Added to the server\n"
            e.description += msg
            action = discord.AuditLogAction.emoji_create
        elif changed:
            old = next((em for em in before if em.id == changed.id), None)
            if old:
                if old.name != changed.name:
                    msg += f"{changed} `{changed}` Renamed from {old.name} to {changed.name}\n"
                    e.description += msg
                    action = discord.AuditLogAction.emoji_update
                if old.roles != changed.roles:
                    if not changed.roles:
                        e.description += _(" Changed to unrestricted.\n")
                    elif not old.roles:
                        e.description += _(" Restricted to roles: ") + humanize_list([r.mention for r in changed.roles])
                    else:
                        e.description += _(" Role restriction changed.\n")
        if not e.description:
            return
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log and action:
            async for log in guild.audit_logs(limit=1, action=action):
                perp, reason = log.user, log.reason
                break
        if perp: e.add_field(name=_("Updated by "), value=perp.mention)
        if reason: e.add_field(name=_("Reason "), value=reason)
        if embed_ok:
            await ch.send(embed=e)
        else:
            await ch.send(gs.events[Event.EMOJI_CHANGE].emoji + " " + msg)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        if not gs.events[Event.VOICE_CHANGE].enabled or member.bot:
            return
        try:
            ch = await self._modlog_channel(guild, Event.VOICE_CHANGE)
        except RuntimeError:
            return
        if after.channel and await self._is_ignored(guild, after.channel): return
        if before.channel and await self._is_ignored(guild, before.channel): return
        embed_ok = await self._can_send_embed(ch, Event.VOICE_CHANGE, guild)
        e = discord.Embed(timestamp=datetime.datetime.utcnow(), colour=await self._event_colour(guild, Event.VOICE_CHANGE))
        e.set_author(name=f"{member} ({member.id}) Voice State Update")
        changed = False
        if before.deaf != after.deaf:
            changed = True
            e.description = member.mention + (" was deafened." if after.deaf else " was undeafened.")
        if before.mute != after.mute:
            changed = True
            e.description = member.mention + (" was muted." if after.mute else " was unmuted.")
        if before.channel != after.channel:
            changed = True
            if before.channel is None:
                e.description = member.mention + _(" has joined ") + inline(after.channel.name)
            elif after.channel is None:
                e.description = member.mention + _(" has left ") + inline(before.channel.name)
            else:
                e.description = (member.mention + _(" has moved from ") + inline(before.channel.name) + _(" to ") + inline(after.channel.name))
        if not changed:
            return
        perp, reason = None, None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                if log.target.id == member.id:
                    perp, reason = log.user, log.reason
                    break
        if perp: e.add_field(name=_("Updated by"), value=perp.mention)
        if reason: e.add_field(name=_("Reason"), value=reason)
        if embed_ok:
            await ch.send(embed=e)
        else:
            await ch.send(escape(e.description or "", mass_mentions=True))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = before.guild
        if version_info >= VersionInfo.from_str("3.4.0"):
            if await self.bot.cog_disabled_in_guild(self, guild):
                return
        gs = self._gs(guild)
        es = gs.events[Event.USER_CHANGE]
        if not es.enabled:
            return
        if not es.bots and after.bot:
            return
        try:
            ch = await self._modlog_channel(guild, Event.USER_CHANGE)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.USER_CHANGE, guild)
        e = discord.Embed(timestamp=datetime.datetime.utcnow(), colour=await self._event_colour(guild, Event.USER_CHANGE))
        e.set_author(name=f"{before} ({before.id}) updated", icon_url=before.display_avatar.url)
        changed = False
        perp, reason = None, None
        if es.nicknames and before.nick != after.nick:
            changed = True
            e.add_field(name=_("Before Nickname"), value=str(before.nick)[:1024])
            e.add_field(name=_("After Nickname"), value=str(after.nick)[:1024])
        if before.roles != after.roles:
            b = set(before.roles); a = set(after.roles)
            removed = list(b - a); added = list(a - b)
            if removed:
                changed = True
                e.add_field(name=_("Roles removed"), value=humanize_list([r.mention for r in removed])[:1024] or "-")
            if added:
                changed = True
                e.add_field(name=_("Roles added"), value=humanize_list([r.mention for r in added])[:1024] or "-")
        if not changed:
            return
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                if log.target.id == before.id:
                    perp, reason = log.user, log.reason
                    break
        if perp: e.add_field(name=_("Updated by"), value=perp.mention)
        if reason: e.add_field(name=_("Reason"), value=reason)
        if embed_ok:
            await ch.send(embed=e)
        else:
            await ch.send(_("{emoji} `{time}` Member updated **{m}** (`{mid}`)").format(
                emoji=es.emoji, time=datetime.datetime.utcnow().strftime("%H:%M:%S"), m=before, mid=before.id
            ))

    # ---- Invite tracking (basic parity) ----

    async def _invite_loop(self):
        if version_info >= VersionInfo.from_str("3.2.0"):
            await self.bot.wait_until_red_ready()
        else:
            await self.bot.wait_until_ready()
        while self is self.bot.get_cog("ModLogV2"):
            for gid, gs in list(self._cache.items()):
                guild = self.bot.get_guild(gid)
                if not guild:
                    continue
                if gs.events[Event.USER_JOIN].enabled:
                    await self._save_invites(guild)
            await asyncio.sleep(300)

    async def _save_invites(self, guild: discord.Guild) -> bool:
        invites = {}
        if not guild.me.guild_permissions.manage_guild:
            return False
        for inv in await guild.invites():
            try:
                created_at = getattr(inv, "created_at", datetime.datetime.utcnow())
                channel = getattr(inv, "channel", discord.Object(id=0))
                inviter = getattr(inv, "inviter", discord.Object(id=0))
                invites[inv.code] = {
                    "uses": getattr(inv, "uses", 0),
                    "max_age": getattr(inv, "max_age", None),
                    "created_at": created_at.timestamp(),
                    "max_uses": getattr(inv, "max_uses", None),
                    "temporary": getattr(inv, "temporary", False),
                    "inviter": inviter.id,
                    "channel": channel.id,
                }
            except Exception:
                logger.exception("Error saving invites.")
                pass
        self._gs(guild).invite_links = invites
        await self._save(guild)
        return True

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild
        gs = self._gs(guild)
        es = gs.events[Event.INVITE_CREATED]
        # cache new code
        if invite.code not in gs.invite_links:
            created_at = getattr(invite, "created_at", datetime.datetime.utcnow())
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
            await self._save(guild)
        if not es.enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.INVITE_CREATED)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.INVITE_CREATED, guild)
        attrs = {"code":_("Code:"), "inviter":_("Inviter:"), "channel":_("Channel:"), "max_uses":_("Max Uses:")}
        msgbits = []
        emb = discord.Embed(title=_("Invite Created"), colour=await self._event_colour(guild, Event.INVITE_CREATED))
        for attr, name in attrs.items():
            v = getattr(invite, attr, None)
            if v:
                msgbits.append(f"{name} {v}")
                emb.add_field(name=name, value=str(v))
        if not msgbits:
            return
        if embed_ok:
            await ch.send(embed=emb)
        else:
            await ch.send(gs.events[Event.INVITE_CREATED].emoji + " " + " | ".join(msgbits))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild = invite.guild
        gs = self._gs(guild)
        es = gs.events[Event.INVITE_DELETED]
        if not es.enabled:
            return
        try:
            ch = await self._modlog_channel(guild, Event.INVITE_DELETED)
        except RuntimeError:
            return
        embed_ok = await self._can_send_embed(ch, Event.INVITE_DELETED, guild)
        attrs = {"code":_("Code: "), "inviter":_("Inviter: "), "channel":_("Channel: "), "max_uses":_("Max Uses: "), "uses":_("Used: ")}
        emb = discord.Embed(title=_("Invite Deleted"), colour=await self._event_colour(guild, Event.INVITE_DELETED))
        msgbits = []
        for attr, name in attrs.items():
            v = getattr(invite, attr, None)
            if v:
                msgbits.append(f"{name}{v}")
                emb.add_field(name=name, value=str(v))
        if not msgbits:
            return
        if embed_ok:
            await ch.send(embed=emb)
        else:
            await ch.send(gs.events[Event.INVITE_DELETED].emoji + " " + " | ".join(msgbits))
