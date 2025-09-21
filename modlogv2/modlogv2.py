# modlogv2.py
import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Union, List, Sequence, Callable, Tuple, Deque
from collections import deque

import discord
from discord.ext import commands

from redbot.core import checks, commands as redcommands, Config, modlog, VersionInfo, version_info
from redbot.core.bot import Red
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import humanize_list, inline, escape

_ = Translator("ModLogV2", __file__)
logger = logging.getLogger("red.modlogv2")

GuildChannel = Union[discord.abc.GuildChannel, discord.Thread]

# ============================================================
# Events & Settings
# ============================================================

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
    # extras
    bots: Optional[bool] = None  # message_edit/delete + user_change
    bulk_enabled: Optional[bool] = None
    bulk_individual: Optional[bool] = None
    cached_only: Optional[bool] = None
    nicknames: Optional[bool] = None
    privs: Optional[List[str]] = None

def _defaults() -> Dict[Event, EventSettings]:
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

@dataclass
class GuildState:
    events: Dict[Event, EventSettings] = field(default_factory=_defaults)
    ignored_channels: List[int] = field(default_factory=list)
    invite_links: Dict[str, dict] = field(default_factory=dict)

# ============================================================
# Rolling sink (one embed per (guild, event, channel))
# ============================================================

@dataclass
class SinkEntry:
    ts: datetime.datetime
    text: str        # a compact single-line (or short) line
    colour: int      # colour to use when this is the newest entry
    icon_url: Optional[str] = None  # small context icon (author/guild)
    footer: Optional[str] = None    # e.g., "User ID: ..."

class RollingSink:
    """
    Maintains a single embed per (guild_id, event, channel_id).
    New entries are queued and flushed periodically by editing the same message.
    """
    def __init__(self, bot: Red, *, max_items: int = 12, flush_interval: float = 1.5):
        self.bot = bot
        self.max_items = max_items
        self.flush_interval = flush_interval
        # key: (guild_id, event.value, channel_id) -> state
        self._state: Dict[Tuple[int, str, int], Dict[str, Union[int, Deque[SinkEntry], datetime.datetime]]] = {}
        # message registry to edit
        self._message_ids: Dict[Tuple[int, str, int], int] = {}
        self._task: Optional[asyncio.Task] = None
        self._pending: asyncio.Event = asyncio.Event()
        self._running = False

    async def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._worker())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def _key(self, guild_id: int, event: Event, channel_id: int) -> Tuple[int, str, int]:
        return (guild_id, event.value, channel_id)

    def enqueue(self, guild_id: int, event: Event, channel_id: int, entry: SinkEntry):
        key = self._key(guild_id, event, channel_id)
        state = self._state.get(key)
        if not state:
            state = {"queue": deque(maxlen=self.max_items), "last": datetime.datetime.utcfromtimestamp(0)}
            self._state[key] = state
        q: Deque[SinkEntry] = state["queue"]  # type: ignore
        q.append(entry)
        self._pending.set()

    async def _ensure_message(self, guild: discord.Guild, channel: discord.TextChannel, key: Tuple[int,str,int]) -> Optional[discord.Message]:
        """
        Ensure there is a sink message; create one if needed.
        """
        mid = self._message_ids.get(key)
        try:
            if mid:
                msg = await channel.fetch_message(mid)
                return msg
        except Exception:
            # message missing (deleted), recreate
            pass
        # create a fresh skeleton embed
        try:
            e = discord.Embed(title=f"ModLog • {key[1].replace('_',' ').title()}", description="*…*", colour=discord.Colour.blurple())
            e.set_footer(text="Rolling log")
            m = await channel.send(embed=e)
            self._message_ids[key] = m.id
            return m
        except Exception as e:
            logger.debug("Failed to create rolling sink message: %r", e)
            return None

    def _build_embed(self, guild: discord.Guild, ev: Event, entries: Deque[SinkEntry]) -> discord.Embed:
        if not entries:
            e = discord.Embed(title=f"ModLog • {ev.value.replace('_',' ').title()}", description="*", colour=discord.Colour.blurple())
            return e
        newest = entries[-1]
        e = discord.Embed(
            title=f"ModLog • {ev.value.replace('_',' ').title()}",
            description="\n".join(
                f"• `{x.ts.strftime('%H:%M:%S')}` {x.text}" for x in list(entries)
            )[:4000],  # be safe under 4096 desc limit
            colour=discord.Colour(newest.colour)
        )
        if newest.icon_url:
            e.set_author(name=guild.name, icon_url=newest.icon_url)
        if newest.footer:
            e.set_footer(text=newest.footer)
        return e

    async def _worker(self):
        while self._running:
            try:
                # wait for signal or timeout
                try:
                    await asyncio.wait_for(self._pending.wait(), timeout=self.flush_interval)
                except asyncio.TimeoutError:
                    pass
                self._pending.clear()

                # flush all queues
                for key, state in list(self._state.items()):
                    gid, ev_s, cid = key
                    guild = self.bot.get_guild(gid)
                    if not guild:
                        continue
                    ch = getattr(guild, "get_channel_or_thread", guild.get_channel)(cid)
                    if not ch or not isinstance(ch, (discord.TextChannel, discord.Thread)):
                        continue

                    q: Deque[SinkEntry] = state["queue"]  # type: ignore
                    if not q:
                        continue

                    # (Thread also supports edit on the message inside it)
                    base_channel = ch if isinstance(ch, discord.TextChannel) else ch.parent
                    if not base_channel:
                        continue
                    if not base_channel.permissions_for(guild.me).send_messages:
                        continue
                    if base_channel.permissions_for(guild.me).embed_links is False:
                        # If no embed perms, send a compact text burst (at most one per flush)
                        # Keep it very short to avoid spam.
                        burst = " | ".join(f"`{x.ts.strftime('%H:%M:%S')}` {x.text}" for x in list(q)[-3:])
                        try:
                            await ch.send(burst[:2000])
                        except Exception:
                            pass
                        q.clear()
                        continue

                    msg = await self._ensure_message(guild, ch if isinstance(ch, discord.TextChannel) else ch, key)  # type: ignore
                    if not msg:
                        # fallback to plain send to avoid losing info
                        burst = "\n".join(f"`{x.ts.strftime('%H:%M:%S')}` {x.text}" for x in list(q)[-5:])
                        try:
                            await ch.send(burst[:2000])
                        except Exception:
                            pass
                        q.clear()
                        continue

                    ev = Event.from_str(ev_s)
                    emb = self._build_embed(guild, ev, q)
                    try:
                        await msg.edit(embed=emb)
                        # keep no more than max_items; we already enforce by deque maxlen
                        # we do not clear queue so the message shows history; new entries overwrite via append.
                    except Exception as e:
                        logger.debug("Sink edit failed (%s): %r", ev_s, e)
                        # attempt recreate next time by dropping id
                        self._message_ids.pop(key, None)
                        # light fallback
                        try:
                            await ch.send("\n".join(f"`{x.ts.strftime('%H:%M:%S')}` {x.text}" for x in list(q)[-3:])[:2000])
                        except Exception:
                            pass
                        q.clear()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Rolling sink loop error: %r", e)
                # small backoff to avoid tight crash loop
                await asyncio.sleep(2)

# ============================================================
# Cog
# ============================================================

@cog_i18n(_)
class ModLogV2(redcommands.Cog):
    """Modern modlogs using rolling (editable) embeds to reduce spam and rate limits."""

    __author__ = ["Pat+ChatGPT"] # tbh I got lazy
    __version__ = "2.1.0"

    def __init__(self, bot: Red):
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, 0xA11CE55, force_registration=True)
        self.config.register_global(version=self.__version__)
        self.config.register_guild(events={}, ignored_channels=[], invite_links={}, rolling=True, max_lines=12, flush_interval=1.5)
        self._cache: Dict[int, GuildState] = {}
        self._invite_task: Optional[asyncio.Task] = None
        self._sink = RollingSink(bot)

    # ------------- lifecycle -------------

    async def cog_load(self) -> None:
        await self._load_all()
        await self._sink.start()
        if self._invite_task is None or self._invite_task.done():
            try:
                self._invite_task = asyncio.create_task(self._invite_loop())
            except RuntimeError:
                logger.exception("Failed to start invite loop")
                self._invite_task = None

    def cog_unload(self) -> None:
        if self._invite_task:
            self._invite_task.cancel()
        self._sink.stop()

    async def red_delete_data_for_user(self, **kwargs):
        return

    # ------------- config helpers -------------

    async def _load_all(self):
        all_guild = await self.config.all_guilds()
        for gid, blob in all_guild.items():
            gs = GuildState()
            # events
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

            # per-guild sink tuning
            max_lines = blob.get("max_lines", 12)
            flush = blob.get("flush_interval", 1.5)
            self._sink.max_items = max_lines
            self._sink.flush_interval = flush

        # bump version & persist
        stored_version = await self.config.version()
        if stored_version != self.__version__:
            await self.config.version.set(self.__version__)
            for gid, gs in self._cache.items():
                await self._save(discord.Object(id=gid))  # type: ignore

    async def _save(self, guild: Union[discord.Guild, discord.Object]):
        gid = guild.id
        gs = self._cache.get(gid) or GuildState()
        payload = {
            "events": {e.value: gs.events[e].__dict__ for e in Event},
            "ignored_channels": gs.ignored_channels,
            "invite_links": gs.invite_links,
            "rolling": True,
            "max_lines": self._sink.max_items,
            "flush_interval": self._sink.flush_interval,
        }
        await self.config.guild_from_id(gid).set(payload)

    def _gs(self, guild: discord.Guild) -> GuildState:
        if guild.id not in self._cache:
            self._cache[guild.id] = GuildState()
        return self._cache[guild.id]

    # ------------- utility -------------

    async def _get_embed_colour(self, channel: discord.abc.Messageable, guild: discord.Guild) -> discord.Colour:
        try:
            if await self.bot.db.guild(guild).use_bot_color():
                return guild.me.colour
            else:
                return await self.bot.db.color()
        except AttributeError:
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

    async def _emit(self, guild: discord.Guild, ev: Event, ch: Union[discord.TextChannel, discord.Thread], *, text_line: str, colour: discord.Colour, footer: Optional[str] = None, icon_url: Optional[str] = None, embed_enabled: bool = True):
        """
        Central logger: send to sink (preferred) or text fallback.
        """
        base_ch = ch if isinstance(ch, discord.TextChannel) else ch.parent
        if not base_ch:
            return
        if embed_enabled and base_ch.permissions_for(guild.me).embed_links:
            entry = SinkEntry(
                ts=datetime.datetime.utcnow(), text=text_line[:350], colour=int(colour), icon_url=icon_url, footer=footer
            )
            self._sink.enqueue(guild.id, ev, ch.id, entry)  # type: ignore
        else:
            try:
                await ch.send(f"`{datetime.datetime.utcnow().strftime('%H:%M:%S')}` {text_line}"[:2000])
            except Exception:
                pass

    async def _settings_text(self, guild: discord.Guild) -> str:
        gs = self._gs(guild)
        try:
            modch = (await modlog.get_modlog_channel(guild)).mention
        except Exception:
            modch = "Not Set"
        lines = [f"**Settings for {guild.name}**\nCore Modlog Channel: {modch}\n"]
        get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
        for ev in Event:
            es = gs.events[ev]
            ch = get_channel(es.channel) if es.channel else None  # type: ignore[arg-type]
            lines.append(f"• `{ev.value}`: **{es.enabled}**" + (f" → {ch.mention}" if ch else ""))
        if gs.ignored_channels:
            chs = ", ".join(self._chan_mention(cid, get_channel(cid)) for cid in gs.ignored_channels)  # type: ignore[arg-type]
            lines.append(f"\nIgnored Channels: {chs}")
        lines.append(f"\nRolling embeds: **on** • items: **{self._sink.max_items}** • flush: **{self._sink.flush_interval:.1f}s**")
        return "\n".join(lines)

    # ============================================================
    # Commands
    # ============================================================

    @redcommands.group(name="modlogv2", invoke_without_command=True, aliases=["mlv2"])
    @commands.guild_only()
    async def grp(self, ctx: redcommands.Context):
        """Show ModLogV2 settings"""
        await ctx.maybe_send_embed(await self._settings_text(ctx.guild))

    @grp.command(name="setup")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def setup(self, ctx: redcommands.Context):
        """Quick setup tips (interactive view removed for maximum compatibility)."""
        await ctx.send(
            "**ModLogV2 Quick Setup**\n"
            f"• Set core modlog channel: `{ctx.clean_prefix}modlogset modlog #{ctx.channel.name}`\n"
            f"• Enable common events: `{ctx.clean_prefix}modlogv2 toggle true message_edit message_delete user_join user_left commands_used`\n"
            f"• Route to this channel: `{ctx.clean_prefix}modlogv2 channel {ctx.channel.mention} message_edit message_delete user_join user_left commands_used`\n"
            f"• Review: `{ctx.clean_prefix}modlogv2`\n\n"
            f"Rolling embeds are enabled by default and will edit a single message per event."
        )

    @grp.command(name="quickstart")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def quickstart(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        """One-shot setup enabling common events in this channel (or a specified channel)."""
        ch = channel or ctx.channel
        gs = self._gs(ctx.guild)
        defaults = [Event.MESSAGE_EDIT, Event.MESSAGE_DELETE, Event.USER_JOIN, Event.USER_LEFT, Event.COMMANDS_USED]
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
            f"Use `{ctx.clean_prefix}modlogv2` to review."
        )

    # parity commands (colour/embeds/toggle/channel/resetchannel/all & toggles)
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
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].embed = set_to
        await self._save(ctx.guild)
        await ctx.send(_("{ev} embed logs set to {v}").format(ev=humanize_list([e.value for e in evs]), v=str(set_to)))

    @grp.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_toggle(self, ctx: redcommands.Context, set_to: bool, *events: str):
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].enabled = set_to
        await self._save(ctx.guild)
        await ctx.send(_("{ev} logs set to {v}").format(ev=humanize_list([e.value for e in evs]), v=str(set_to)))

    @grp.command(name="channel")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_channel(self, ctx: redcommands.Context, channel: discord.TextChannel, *events: str):
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].channel = channel.id
        await self._save(ctx.guild)
        await ctx.send(_("{ev} logs will go to {ch}").format(ev=humanize_list([e.value for e in evs]), ch=channel.mention))

    @grp.command(name="resetchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_resetchannel(self, ctx: redcommands.Context, *events: str):
        evs = self._parse_events(ctx, list(events))
        gs = self._gs(ctx.guild)
        for ev in evs:
            gs.events[ev].channel = None
        await self._save(ctx.guild)
        await ctx.send(_("Reset channel for {ev}.").format(ev=humanize_list([e.value for e in evs])))

    @grp.command(name="all")
    @checks.admin_or_permissions(manage_guild=True)
    async def cmd_all(self, ctx: redcommands.Context, set_to: bool):
        gs = self._gs(ctx.guild)
        for ev in Event:
            gs.events[ev].enabled = set_to
        await self._save(ctx.guild)
        await ctx.maybe_send_embed(await self._settings_text(ctx.guild))

    @grp.command(name="botedits")
    async def cmd_botedits(self, ctx: redcommands.Context):
        gs = self._gs(ctx.guild)
        es = gs.events[Event.MESSAGE_EDIT]
        es.bots = not bool(es.bots)
        await self._save(ctx.guild)
        await ctx.send(_("Bots edited messages ") + ("enabled" if es.bots else "disabled"))

    @grp.command(name="botdeletes")
    async def cmd_botdeletes(self, ctx: redcommands.Context):
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

    # ============================================================
    # Helpers
    # ============================================================

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

    # ============================================================
    # Listeners (now emit into rolling sink)
    # ============================================================

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

        command = ctx.message.content.replace(ctx.prefix, "")
        try:
            privs = self.bot.get_command(command).requires.privilege_level.name
        except Exception:
            return
        wanted = set(es.privs or [])
        if privs not in wanted:
            return

        colour = await self._event_colour(guild, Event.COMMANDS_USED)
        text = f"{ctx.author} (`{ctx.author.id}`) → {ctx.channel.mention} • {escape(ctx.message.content, mass_mentions=True)[:180]}"
        await self._emit(guild, Event.COMMANDS_USED, ch, text_line=text, colour=colour, footer=f"User ID: {ctx.author.id}", icon_url=ctx.author.display_avatar.url, embed_enabled=es.embed)

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
        message = payload.cached_message
        colour = await self._event_colour(guild, Event.MESSAGE_DELETE)

        if message is None:
            if es.cached_only:
                return
            mention = self._chan_mention(payload.channel_id, source_channel)
            text = f"Message deleted in {mention} • *(content unknown)*"
            await self._emit(guild, Event.MESSAGE_DELETE, ch, text_line=text, colour=colour, embed_enabled=es.embed)
            return

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
        base = f"{message.author} (`{message.author.id}`) in {mention}"
        if perp:
            base = f"{perp} deleted a message from {base}"
        text = f"{base}: {escape(message.clean_content, mass_mentions=True)[:160]}"
        await self._emit(guild, Event.MESSAGE_DELETE, ch, text_line=text, colour=colour, footer=f"User ID: {message.author.id}", icon_url=message.author.display_avatar.url, embed_enabled=es.embed)

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
        get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
        message_channel = get_channel(payload.channel_id)
        if await self._is_ignored(guild, message_channel):
            return
        mention = self._chan_mention(payload.channel_id, message_channel)
        amount = len(payload.message_ids)
        colour = await self._event_colour(guild, Event.MESSAGE_DELETE)
        text = f"Bulk delete in {mention} • {amount} messages"
        await self._emit(guild, Event.MESSAGE_DELETE, ch, text_line=text, colour=colour, embed_enabled=es.embed)

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
        colour = await self._event_colour(guild, Event.MESSAGE_EDIT)
        text = (f"{before.author} (`{before.author.id}`) edited in {before.channel.mention} • "
                f"Before: {escape(before.content, mass_mentions=True)[:80]} → After: {escape(after.content, mass_mentions=True)[:80]}")
        await self._emit(guild, Event.MESSAGE_EDIT, ch, text_line=text, colour=colour, footer=f"User ID: {before.author.id}", icon_url=before.author.display_avatar.url, embed_enabled=es.embed)

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
        users = len(guild.members)
        since_created = (datetime.datetime.utcnow() - member.created_at).days
        colour = await self._event_colour(guild, Event.USER_JOIN)
        text = f"{member} (`{member.id}`) joined • Total: {users} • Created {since_created}d ago"
        await self._emit(guild, Event.USER_JOIN, ch, text_line=text, colour=colour, footer=f"User ID: {member.id}", icon_url=member.display_avatar.url, embed_enabled=es.embed)

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
        perp = None
        reason = None
        if ch.permissions_for(guild.me).view_audit_log:
            async for log in guild.audit_logs(limit=5, after=datetime.datetime.utcnow() + datetime.timedelta(minutes=-30), action=discord.AuditLogAction.kick):
                if log.target.id == member.id:
                    perp = log.user
                    reason = log.reason
                    break
        colour = await self._event_colour(guild, Event.USER_LEFT)
        text = f"{member} (`{member.id}`) left • Total: {len(guild.members)}"
        if perp:
            text = f"{member} (`{member.id}`) was kicked by {perp} • Total: {len(guild.members)}"
        if reason:
            text += f" • Reason: {reason}"
        await self._emit(guild, Event.USER_LEFT, ch, text_line=text, colour=colour, footer=f"User ID: {member.id}", icon_url=member.display_avatar.url, embed_enabled=es.embed)

    # Channel create/delete/update condensed similarly:

    async def _perm_changes_text(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel, embed_links: bool) -> str:
        p_msg = ""
        bp, ap = {}, {}
        for o, p in before.overwrites.items():
            bp[str(o.id)] = [i for i in p]
        for o, p in after.overwrites.items():
            ap[str(o.id)] = [i for i in p]
        def fmt_entity(guild: discord.Guild, ent_id: int) -> str:
            role = guild.get_role(ent_id)
            if role is not None:
                return role.mention if embed_links else role.name
            member = guild.get_member(ent_id)
            if member is not None:
                return member.mention if embed_links else member.display_name
            return f"`{ent_id}`"
        for ent in bp:
            name = fmt_entity(before.guild, int(ent))
            if ent not in ap:
                p_msg += (f"{name} overwrites removed; ")
                continue
            if ap[ent] != bp[ent]:
                a = set(ap[ent]); b = set(bp[ent])
                for diff in list(a - b):
                    p_msg += (f"{name} {diff[0]}→{diff[1]}; ")
        for ent in ap:
            name = fmt_entity(after.guild, int(ent))
            if ent not in bp:
                p_msg += (f"{name} overwrites added; ")
        return p_msg[:300]

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
        t = str(new_channel.type).title()
        colour = await self._event_colour(guild, Event.CHANNEL_CREATE)
        text = f"{t} channel created: {new_channel.mention}"
        await self._emit(guild, Event.CHANNEL_CREATE, ch, text_line=text, colour=colour, embed_enabled=gs.events[Event.CHANNEL_CREATE].embed)

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
        t = str(old_channel.type).title()
        colour = await self._event_colour(guild, Event.CHANNEL_DELETE)
        text = f"{t} channel deleted: #{old_channel.name} (`{old_channel.id}`)"
        await self._emit(guild, Event.CHANNEL_DELETE, ch, text_line=text, colour=colour, embed_enabled=gs.events[Event.CHANNEL_DELETE].embed)

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
        t = str(after.type).title()
        colour = await self._event_colour(guild, Event.CHANNEL_CHANGE)
        changed_bits: List[str] = []
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            for attr, name in {"name":"Name", "topic":"Topic", "category":"Category", "slowmode_delay":"Slowmode"}.items():
                b = getattr(before, attr); a = getattr(after, attr)
                if b != a:
                    changed_bits.append(f"{name}: {b!s}→{a!s}")
            if before.is_nsfw() != after.is_nsfw():
                changed_bits.append(f"NSFW: {before.is_nsfw()}→{after.is_nsfw()}")
            pmsg = await self._perm_changes_text(before, after, True)
            if pmsg:
                changed_bits.append(f"Perms: {pmsg}")
        elif isinstance(before, discord.VoiceChannel) and isinstance(after, discord.VoiceChannel):
            for attr, name in {"name":"Name", "position":"Position", "category":"Category", "bitrate":"Bitrate", "user_limit":"UserLimit"}.items():
                b = getattr(before, attr); a = getattr(after, attr)
                if b != a:
                    changed_bits.append(f"{name}: {b!s}→{a!s}")
            pmsg = await self._perm_changes_text(before, after, True)
            if pmsg:
                changed_bits.append(f"Perms: {pmsg}")
        if not changed_bits:
            return
        text = f"{t} channel updated: {after.mention} • " + " | ".join(changed_bits)[:300]
        await self._emit(guild, Event.CHANNEL_CHANGE, ch, text_line=text, colour=colour, embed_enabled=gs.events[Event.CHANNEL_CHANGE].embed)

    # Roles / Guild / Emojis / Voice / Member updates condensed in same style:

    async def _role_perm_changes(self, before: discord.Role, after: discord.Role) -> str:
        perms = [
            "create_instant_invite","kick_members","ban_members","administrator","manage_channels","manage_guild",
            "add_reactions","view_audit_log","priority_speaker","read_messages","send_messages","send_tts_messages",
            "manage_messages","embed_links","attach_files","read_message_history","mention_everyone","external_emojis",
            "connect","speak","mute_members","deafen_members","move_members","use_voice_activation","change_nickname",
            "manage_nicknames","manage_roles","manage_webhooks","manage_emojis"
        ]
        bits = []
        for p in perms:
            if getattr(before.permissions, p) != getattr(after.permissions, p):
                bits.append(f"{p}: {getattr(after.permissions, p)}")
        return "; ".join(bits)[:300]

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
        colour = await self._event_colour(guild, Event.ROLE_CHANGE, changed_role=after)
        bits = []
        for attr, name in {"name":"Name", "color":"Colour", "mentionable":"Mentionable", "hoist":"Hoisted"}.items():
            b = getattr(before, attr); a = getattr(after, attr)
            if b != a:
                bits.append(f"{name}: {b!s}→{a!s}")
        pbits = await self._role_perm_changes(before, after)
        if pbits:
            bits.append(f"Perms: {pbits}")
        if not bits:
            return
        text = f"Role updated: {after.mention} • " + " | ".join(bits)
        await self._emit(guild, Event.ROLE_CHANGE, ch, text_line=text, colour=colour, embed_enabled=gs.events[Event.ROLE_CHANGE].embed)

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
        colour = await self._event_colour(guild, Event.ROLE_CREATE)
        text = f"Role created: {role.mention}"
        await self._emit(guild, Event.ROLE_CREATE, ch, text_line=text, colour=colour, embed_enabled=gs.events[Event.ROLE_CREATE].embed)

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
        colour = await self._event_colour(guild, Event.ROLE_DELETE)
        text = f"Role deleted: **{role.name}** (`{role.id}`)"
        await self._emit(guild, Event.ROLE_DELETE, ch, text_line=text, colour=colour, embed_enabled=gs.events[Event.ROLE_DELETE].embed)

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
        colour = await self._event_colour(guild, Event.GUILD_CHANGE)
        fields = {"name":"Name", "afk_timeout":"AFK", "afk_channel":"AFKChan", "icon":"Icon", "owner":"Owner", "system_channel":"WelcomeChan", "verification_level":"VerifyLevel"}
        bits = []
        for attr, name in fields.items():
            b = getattr(before, attr, None); a = getattr(after, attr, None)
            if b != a:
                bits.append(f"{name}: {b!s}→{a!s}")
        if not bits:
            return
        text = "Guild updated • " + " | ".join(bits)[:300]
        await self._emit(guild, Event.GUILD_CHANGE, ch, text_line=text, colour=colour, icon_url=self._guild_icon_url(guild), embed_enabled=gs.events[Event.GUILD_CHANGE].embed)

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
        colour = await self._event_colour(guild, Event.EMOJI_CHANGE)
        b = set(before); a = set(after)
        try:
            added = (a - b).pop()
        except KeyError:
            added = None
        try:
            removed = (b - a).pop()
        except KeyError:
            removed = None
        changed_text = ""
        if removed is not None:
            changed_text = f"Emoji removed: `{removed}` (ID: {removed.id})"
        elif added is not None:
            changed_text = f"Emoji added: {added} `{added}`"
        else:
            # rename/restrict change detection
            changed_pair = set((e, e.name, tuple(e.roles)) for e in after)
            baseline = list(before)
            if added:
                baseline.append(added)
            changed_pair.difference_update((e, e.name, tuple(e.roles)) for e in baseline)
            try:
                changed = next(iter(changed_pair))[0]
            except Exception:
                changed = None
            if changed:
                old = next((em for em in before if em.id == changed.id), None)
                if old:
                    if old.name != changed.name:
                        changed_text = f"Emoji renamed: {old.name}→{changed.name}"
                    elif old.roles != changed.roles:
                        changed_text = "Emoji role restriction changed"
        if not changed_text:
            return
        await self._emit(guild, Event.EMOJI_CHANGE, ch, text_line=changed_text, colour=colour, embed_enabled=gs.events[Event.EMOJI_CHANGE].embed)

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
        colour = await self._event_colour(guild, Event.VOICE_CHANGE)
        text = None
        if before.deaf != after.deaf:
            text = f"{member} (`{member.id}`) was " + ("deafened" if after.deaf else "undeafened")
        if before.mute != after.mute:
            text = f"{member} (`{member.id}`) was " + ("muted" if after.mute else "unmuted")
        if before.channel != after.channel:
            if before.channel is None:
                text = f"{member} (`{member.id}`) joined {inline(after.channel.name)}"
            elif after.channel is None:
                text = f"{member} (`{member.id}`) left {inline(before.channel.name)}"
            else:
                text = f"{member} (`{member.id}`) moved {inline(before.channel.name)} → {inline(after.channel.name)}"
        if text:
            await self._emit(guild, Event.VOICE_CHANGE, ch, text_line=text, colour=colour, footer=f"User ID: {member.id}", embed_enabled=gs.events[Event.VOICE_CHANGE].embed)

    # ------------- Invite tracking (unchanged storage; condensed emits) -------------

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
        # cache
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
        colour = await self._event_colour(guild, Event.INVITE_CREATED)
        who = getattr(invite, "inviter", None)
        where = getattr(invite, "channel", None)
        text = f"Invite created • Code: {invite.code} • By: {who} • In: {getattr(where,'mention', 'Unknown')}"
        await self._emit(guild, Event.INVITE_CREATED, ch, text_line=text, colour=colour, embed_enabled=es.embed)

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
        colour = await self._event_colour(guild, Event.INVITE_DELETED)
        who = getattr(invite, "inviter", None)
        where = getattr(invite, "channel", None)
        used = getattr(invite, "uses", None)
        max_uses = getattr(invite, "max_uses", None)
        text = f"Invite deleted • Code: {invite.code} • By: {who} • In: {getattr(where,'mention','Unknown')} • Uses: {used}/{max_uses}"
        await self._emit(guild, Event.INVITE_DELETED, ch, text_line=text, colour=colour, embed_enabled=es.embed)
