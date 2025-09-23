from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import discord
from redbot.core import Config, checks, commands

__red_end_user_data_statement__ = (
    "This cog stores moderation case metadata and minimal log context (IDs and excerpts) per guild."
)

# ------------------ Visuals ------------------
EVENT_ICONS = {
    # messages
    "message_delete": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f5d1.png",
    "message_edit": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/270f.png",
    "purge": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f9f9.png",
    # reactions
    "reaction": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f44d.png",
    # members / moderation
    "member_join": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f389.png",
    "member_leave": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f6aa.png",
    "member_update": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26a1.png",
    "ban": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f528.png",
    "unban": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f513.png",
    "kick": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26d4.png",       # ⛔
    "timeout": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/23f0.png",    # ⏰
    "untimeout": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/2705.png",  # ✅
    # roles/channels/server
    "role": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f3f7.png",
    "channel": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f4e3.png",
    "thread": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f9f5.png",
    "guild": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f3db.png",
    "emoji": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f600.png",
    "sticker": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f4f0.png",
    "invite": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f4e9.png",
    "webhook": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f4ac.png",
    "integration": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f517.png",
    "scheduled": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/23f3.png",
    "stage": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f3a4.png",
    "voice": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f399.png",
    "presence": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f7e2.png",
    # automod
    "automod_rules": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f6e1.png",
    "automod_action": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26d4.png",
    # default
    "default": "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/1f4cb.png",
}

# ------------------ Defaults ------------------
DEFAULTS_GUILD = {
    "enabled": True,
    "use_embeds": True,
    "webhook_url": None,
    "webhook_identity": "bot",  # "bot" -> use bot name/avatar, "event" -> per-event identity
    "categories": {
        "messages": {"edit": True, "delete": True, "purge": True, "snipe": True},
        "reactions": True,
        "members": {"join": True, "leave": True, "update": True, "ban": True, "unban": True},
        "roles": True,
        "channels": True,
        "threads": True,
        "voice": False,
        "presence": False,
        "server": True,
        "emojis": True,
        "stickers": True,
        "invites": True,
        "webhooks": True,
        "integrations": True,
        "scheduled_events": True,
        "stage": True,
        "automod": {"rules": True, "execution": True},
        "moderation_cases": True,
    },
    # cases / snipe / counters
    "case_counter": 0,
    "cases": {},
    "snipes": {},
    "prune_count": 0,
}

# ------------------ Small helpers ------------------
def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)

def limit(text: Optional[str], n: int = 1024) -> str:
    if text is None:
        return "*none*"
    return text if len(text) <= n else text[: n - 1] + "…"

def u(user: Optional[Union[discord.Member, discord.User]]) -> str:
    if not user:
        return "Unknown"
    return f"{user} (`{user.id}`)"

def chn(o: Optional[Union[discord.abc.GuildChannel, discord.Thread]]) -> str:
    """Format a channel/thread mention with its ID."""
    if not o:
        return "Unknown"
    mention = getattr(o, "mention", None)
    pretty = mention if mention else f"#{getattr(o, 'name', '?')}"
    return f"{pretty} (`{o.id}`)"

def _role_diff(before_roles: List[discord.Role], after_roles: List[discord.Role]) -> Tuple[List[discord.Role], List[discord.Role]]:
    """Return (added, removed) roles, ignoring @everyone."""
    b = {r.id: r for r in before_roles if r.name != "@everyone"}
    a = {r.id: r for r in after_roles if r.name != "@everyone"}
    added_ids = set(a.keys()) - set(b.keys())
    removed_ids = set(b.keys()) - set(a.keys())
    added = [a[i] for i in added_ids]
    removed = [b[i] for i in removed_ids]
    added.sort(key=lambda r: (-r.position, r.name.lower()))
    removed.sort(key=lambda r: (-r.position, r.name.lower()))
    return added, removed

def _role_mentions(roles: List[discord.Role]) -> str:
    return ", ".join(r.mention for r in roles) if roles else "*none*"

def _bool_emoji(v: bool) -> str:
    return "🟢" if v else "⚪"

def _identity_label(mode: str) -> str:
    return "Bot identity" if mode == "bot" else "Per-event identity"

# ------------------ Setup View (UI) ------------------
class SetupView(discord.ui.View):
    def __init__(self, cog: "ModLogX", guild: discord.Guild, *, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.state: Dict[str, Any] = {}
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            with contextlib.suppress(Exception):
                await self.message.edit(view=self)

    async def on_start(self, ctx: commands.Context):
        d = await self.cog._gdata(self.guild)
        self.state = {
            "enabled": d["enabled"],
            "use_embeds": d["use_embeds"],
            "webhook_identity": d.get("webhook_identity", "bot"),
            "webhook_url": d.get("webhook_url"),
            "channel_id": None,
        }

        self.clear_items()
        self.add_item(_ChannelSelect(self))
        self.add_item(_ToggleEnabled(self))
        self.add_item(_ToggleEmbeds(self))
        self.add_item(_ToggleIdentity(self))
        self.add_item(_CreateWebhook(self))
        self.add_item(_TestLog(self))
        self.add_item(_Save(self))
        self.add_item(_Close(self))

        embed = self.cog._build_setup_embed(self.guild, self.state)
        self.message = await ctx.send(embed=embed, view=self)

    async def refresh(self):
        if self.message:
            embed = self.cog._build_setup_embed(self.guild, self.state)
            with contextlib.suppress(Exception):
                await self.message.edit(embed=embed, view=self)

class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(channel_types=[discord.ChannelType.text], min_values=0, max_values=1, placeholder="Select a log channel…")

    async def callback(self, interaction: discord.Interaction):
        if self.values:
            ch: discord.TextChannel = self.values[0]  # type: ignore
            self._view.state["channel_id"] = ch.id
        else:
            self._view.state["channel_id"] = None
        await interaction.response.edit_message(embed=self._view.cog._build_setup_embed(self._view.guild, self._view.state), view=self._view)

class _ToggleEnabled(discord.ui.Button):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(style=discord.ButtonStyle.secondary, label="Enable/Disable")

    async def callback(self, interaction: discord.Interaction):
        self._view.state["enabled"] = not self._view.state["enabled"]
        await interaction.response.edit_message(embed=self._view.cog._build_setup_embed(self._view.guild, self._view.state), view=self._view)

class _ToggleEmbeds(discord.ui.Button):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(style=discord.ButtonStyle.secondary, label="Toggle Embeds")

    async def callback(self, interaction: discord.Interaction):
        self._view.state["use_embeds"] = not self._view.state["use_embeds"]
        await interaction.response.edit_message(embed=self._view.cog._build_setup_embed(self._view.guild, self._view.state), view=self._view)

class _ToggleIdentity(discord.ui.Button):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(style=discord.ButtonStyle.secondary, label="Identity: bot/event")

    async def callback(self, interaction: discord.Interaction):
        cur = self._view.state.get("webhook_identity", "bot")
        self._view.state["webhook_identity"] = "event" if cur == "bot" else "bot"
        await interaction.response.edit_message(embed=self._view.cog._build_setup_embed(self._view.guild, self._view.state), view=self._view)

class _CreateWebhook(discord.ui.Button):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(style=discord.ButtonStyle.primary, label="Create/Refresh Webhook")

    async def callback(self, interaction: discord.Interaction):
        ch_id = self._view.state.get("channel_id")
        if not ch_id:
            return await interaction.response.send_message("Pick a channel first.", ephemeral=True)
        ch = interaction.client.get_channel(ch_id)  # type: ignore
        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("Channel must be a text channel.", ephemeral=True)

        try:
            wh = await ch.create_webhook(name="ModLogX")
        except discord.Forbidden:
            return await interaction.response.send_message("I need **Manage Webhooks** in that channel.", ephemeral=True)
        except Exception as e:
            return await interaction.response.send_message(f"Couldn’t create webhook: `{e}`", ephemeral=True)

        self._view.state["webhook_url"] = wh.url
        await interaction.response.edit_message(embed=self._view.cog._build_setup_embed(self._view.guild, self._view.state), view=self._view)

class _TestLog(discord.ui.Button):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(style=discord.ButtonStyle.success, label="Send Test")

    async def callback(self, interaction: discord.Interaction):
        if not self._view.state.get("webhook_url"):
            return await interaction.response.send_message("Webhook isn’t set yet.", ephemeral=True)
        await self._view.cog._send_embed(
            self._view.guild,
            event_key="default",
            title="Test Log",
            description="If you see this, your webhook works.",
        )
        await interaction.response.send_message("Sent a test log to the configured webhook.", ephemeral=True)

class _Save(discord.ui.Button):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(style=discord.ButtonStyle.primary, label="Save")

    async def callback(self, interaction: discord.Interaction):
        await self._view.cog.config.guild(self._view.guild).enabled.set(bool(self._view.state["enabled"]))
        await self._view.cog.config.guild(self._view.guild).use_embeds.set(bool(self._view.state["use_embeds"]))
        await self._view.cog.config.guild(self._view.guild).webhook_identity.set(self._view.state.get("webhook_identity", "bot"))
        await self._view.cog.config.guild(self._view.guild).webhook_url.set(self._view.state.get("webhook_url"))
        await interaction.response.send_message("Saved.", ephemeral=True)
        await self._view.refresh()

class _Close(discord.ui.Button):
    def __init__(self, view: SetupView):
        self._view = view
        super().__init__(style=discord.ButtonStyle.danger, label="Close")

    async def callback(self, interaction: discord.Interaction):
        for item in self._view.children:
            item.disabled = True
        await interaction.response.edit_message(view=self._view)

# ------------------ Cog ------------------
class ModLogX(commands.Cog):
    """Modlog with webhook output, detailed embeds, cases, and audit correlation."""

    __author__ = "you"
    __version__ = "3.4.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA51D0ECAFE2025, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._audit_fetch_lock: Dict[int, asyncio.Lock] = {}

    # ---------- Config util ----------
    async def _gdata(self, guild: discord.Guild) -> Dict[str, Any]:
        return await self.config.guild(guild).all()

    async def _enabled(self, guild: discord.Guild) -> bool:
        d = await self._gdata(guild)
        return bool(d["enabled"] and d["webhook_url"])

    async def _cat(self, guild: discord.Guild, name: str) -> Any:
        return (await self._gdata(guild))["categories"].get(name)

    # ---------- Setup UI embed ----------
    def _build_setup_embed(self, guild: discord.Guild, state: Dict[str, Any]) -> discord.Embed:
        ch_mention = f"<#{state.get('channel_id')}>" if state.get("channel_id") else "*not set*"
        wh_state = "configured" if state.get("webhook_url") else "not set"
        e = discord.Embed(
            title="ModLogX • Setup",
            description="Configure where logs go and how they look.",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Enabled", value=f"{_bool_emoji(state['enabled'])} `{state['enabled']}`", inline=True)
        e.add_field(name="Embeds", value=f"{_bool_emoji(state['use_embeds'])} `{state['use_embeds']}`", inline=True)
        e.add_field(name="Identity", value=f"`{state['webhook_identity']}`", inline=True)
        e.add_field(name="Log channel", value=ch_mention, inline=True)
        e.add_field(name="Webhook", value=wh_state, inline=True)
        e.set_footer(text=f"{guild.name} • v{self.__version__}")
        return e

    # ---------- Webhook send ----------
    async def _send_embed(
        self,
        guild: discord.Guild,
        *,
        event_key: str,
        title: str,
        description: str,
        fields: Iterable[Tuple[str, str, bool]] = (),
        color: Union[int, discord.Color] = discord.Color.blurple(),
        url: Optional[str] = None,
        footer: Optional[str] = None,
        force_plain: bool = False,
        thumbnail_url: Optional[str] = None,
    ):
        data = await self._gdata(guild)
        if not (data["enabled"] and data["webhook_url"]):
            return

        embed = discord.Embed(title=title, description=limit(description, 4000), color=color, url=url)
        embed.timestamp = now_utc()
        for name, value, inline in fields:
            embed.add_field(name=name, value=limit(value, 1024), inline=inline)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        embed.set_footer(text=footer or f"{guild.name} • v{self.__version__}")

        identity_mode = data.get("webhook_identity", "bot")
        if identity_mode == "bot" and self.bot.user:
            username = self.bot.user.name
            avatar_url = self.bot.user.display_avatar.url
        else:
            username = f"ModLog • {title}"
            avatar_url = EVENT_ICONS.get(event_key, EVENT_ICONS["default"])

        try:
            wh = discord.Webhook.from_url(data["webhook_url"], client=self.bot)
        except Exception:
            return

        try:
            if data["use_embeds"] and not force_plain:
                await wh.send(embed=embed, username=username, avatar_url=avatar_url)
            else:
                content = f"**{title}**\n\n{limit(description, 1800)}"
                for n, v, _ in fields:
                    content += f"\n\n**{n}**\n{limit(v, 1000)}"
                await wh.send(content=content, username=username, avatar_url=avatar_url)
        except discord.NotFound:
            await self.config.guild(guild).webhook_url.set(None)
        except Exception:
            pass

    # ---------- Cases ----------
    @dataclass
    class Case:
        id: int
        action: str
        target_id: int
        target_name: str
        mod_id: Optional[int]
        mod_name: Optional[str]
        reason: Optional[str]
        duration: Optional[int]
        created_at: str

    async def _new_case(
        self,
        guild: discord.Guild,
        *,
        action: str,
        target: Union[discord.Member, discord.User],
        moderator: Optional[Union[discord.Member, discord.User]],
        reason: Optional[str],
        duration: Optional[int] = None,
    ) -> Case:
        async with self.config.guild(guild).case_counter.get_lock():
            cid = await self.config.guild(guild).case_counter()
            await self.config.guild(guild).case_counter.set(cid + 1)
        case = {
            "id": cid,
            "action": action,
            "target_id": target.id,
            "target_name": str(target),
            "mod_id": getattr(moderator, "id", None),
            "mod_name": str(moderator) if moderator else None,
            "reason": reason,
            "duration": duration,
            "created_at": now_utc().isoformat(),
        }
        await self.config.guild(guild).cases.set_raw(str(cid), value=case)

        if await self._cat(guild, "moderation_cases"):
            desc = (
                f"**{action.title()}**\n"
                f"Target: {u(target)}\n"
                f"Moderator: {u(moderator) if moderator else 'Unknown'}\n"
                f"Reason: {reason or '*none*'}"
            )
            extra: List[Tuple[str, str, bool]] = [("Case ID", f"`{cid}`", True)]
            if duration:
                extra.append(("Duration", f"{duration}s", True))
            await self._send_embed(guild, event_key="default", title=f"Case {cid} • {action.title()}", description=desc, fields=extra)
        return self.Case(**case)  # type: ignore[arg-type]

    # ---------- Audit helpers ----------
    async def _who_deleted_message(self, guild: discord.Guild, message: discord.Message) -> Optional[discord.User]:
        lock = self._audit_fetch_lock.setdefault(guild.id, asyncio.Lock())
        async with lock:
            with contextlib.suppress(Exception):
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.message_delete):
                    if getattr(entry.extra, "channel", None) and entry.extra.channel.id != message.channel.id:
                        continue
                    if (now_utc() - entry.created_at.replace(tzinfo=dt.timezone.utc)).total_seconds() > 20:
                        continue
                    return entry.user
        return None

    async def _recent_kick_for(self, guild: discord.Guild, user_id: int):
        """Return (moderator, reason) if the user was kicked very recently; else (None, None)."""
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.kick):
                if not entry.target or entry.target.id != user_id:
                    continue
                age = (now_utc() - entry.created_at.replace(tzinfo=dt.timezone.utc)).total_seconds()
                if age <= 30:
                    return entry.user, entry.reason
        except Exception:
            pass
        return None, None

    async def _recent_timeout_for(self, guild: discord.Guild, user_id: int):
        """Return (moderator, reason, expires_dt) for a very recent timeout change; else (None, None, None)."""
        try:
            async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.member_update):
                tgt = getattr(entry, "target", None)
                if not tgt or tgt.id != user_id:
                    continue
                changes = getattr(entry, "changes", None)
                expires = None
                if changes:
                    with contextlib.suppress(Exception):
                        after = getattr(changes, "after", None) or {}
                        expires = (
                            after.get("communication_disabled_until")
                            or after.get("timed_out_until")
                            or after.get("timeout_until")
                        )
                age = (now_utc() - entry.created_at.replace(tzinfo=dt.timezone.utc)).total_seconds()
                if age <= 30:
                    return entry.user, entry.reason, expires
        except Exception:
            pass
        return None, None, None

    # ================= Commands =================
    @commands.group(name="modlogx", invoke_without_command=True)
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def group(self, ctx: commands.Context):
        g = ctx.guild
        d = await self._gdata(g)
        dest = "Configured" if d["webhook_url"] else "Not set"
        await ctx.send(
            f"**ModLogX** v{self.__version__}\n"
            f"Enabled: `{d['enabled']}` • Embeds: `{d['use_embeds']}` • Identity: `{d.get('webhook_identity','bot')}` • Destination: `{dest}`\n"
            f"Run `[p]modlogx setup` to open the interactive setup.\n"
            f"Toggles: `[p]modlogx enable on/off`, `[p]modlogx embeds on/off`, `[p]modlogx identity bot|event`, "
            f"Sub-events: `[p]modlogx sub <category> <event> <on/off>`"
        )

    @group.command()
    async def setup(self, ctx: commands.Context):
        """Open the interactive setup."""
        view = SetupView(self, ctx.guild)
        await view.on_start(ctx)

    @group.command()
    async def enable(self, ctx: commands.Context, state: Optional[bool] = None):
        await self.config.guild(ctx.guild).enabled.set(True if state is None else bool(state))
        await ctx.tick()

    @group.command()
    async def embeds(self, ctx: commands.Context, state: Optional[bool] = None):
        await self.config.guild(ctx.guild).use_embeds.set(True if state is None else bool(state))
        await ctx.tick()

    @group.command()
    async def identity(self, ctx: commands.Context, mode: str):
        """Set webhook identity: bot | event"""
        mode = mode.lower().strip()
        if mode not in {"bot", "event"}:
            return await ctx.send("Use `bot` or `event`.")
        await self.config.guild(ctx.guild).webhook_identity.set(mode)
        await ctx.tick()

    @group.command()
    async def sub(self, ctx: commands.Context, category: str, event: str, state: Optional[bool] = None):
        """Toggle a sub-event, e.g. `[p]modlogx sub messages delete off`"""
        cats = await self.config.guild(ctx.guild).categories()
        sub = cats.get(category)
        if not isinstance(sub, dict):
            return await ctx.send("That category has no sub-events.")
        if event not in sub:
            return await ctx.send(f"Unknown sub-event. Available: {', '.join(sub.keys())}")
        sub[event] = (not sub[event]) if state is None else bool(state)
        cats[category] = sub
        await self.config.guild(ctx.guild).categories.set(cats)
        await ctx.tick()

    @group.command()
    async def case(self, ctx: commands.Context, case_id: int):
        """Show a case."""
        c = await self.config.guild(ctx.guild).cases.get_raw(str(case_id), default=None)
        if not c:
            return await ctx.send("Case not found.")
        mod_str = f"<@{c['mod_id']}>" if c.get("mod_id") else "Unknown"
        target_mention = f"<@{c['target_id']}>"
        desc = (
            f"**Action**: {c['action']}\n"
            f"**Target**: {target_mention} (`{c['target_id']}`)\n"
            f"**Moderator**: {mod_str}\n"
            f"**Reason**: {c.get('reason') or '*none*'}\n"
            f"**When**: {c.get('created_at')}"
        )
        await ctx.send(embed=discord.Embed(title=f"Case {case_id}", description=desc, color=discord.Color.orange()))

    @group.command()
    async def snipes(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Show last deleted message in a channel (if enabled)."""
        channel = channel or ctx.channel
        sn = await self.config.guild(ctx.guild).snipes.get_raw(str(channel.id), default=None)
        if not sn:
            return await ctx.send("No snipe recorded.")
        desc = f"**Author**: <@{sn['author_id']}>\n**When**: {sn['ts']}\n\n{limit(sn['content'], 1800)}"
        await ctx.send(embed=discord.Embed(title="Snipe", description=desc, color=discord.Color.red()))

    # ================= Listeners =================
    # ----- Messages -----
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not (message.guild and await self._enabled(message.guild)):
            return
        cats = await self._cat(message.guild, "messages")
        if not cats or not cats.get("delete", True):
            return

        deleter = await self._who_deleted_message(message.guild, message)

        attachments = ", ".join(f"[{a.filename}]({a.url})" for a in getattr(message, "attachments", [])) or "*none*"
        embc = len(getattr(message, "embeds", []) or [])
        ref = getattr(message, "reference", None)
        ref_id = getattr(ref, "message_id", None)
        ref_jump = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{ref_id}" if ref_id else None

        fields: List[Tuple[str, str, bool]] = [
            ("Author", f"{message.author.mention} • `{message.author.id}`", True),
            ("Channel", f"{message.channel.mention} • `{message.channel.id}`", True),
            ("Message ID", f"`{message.id}`", True),
            ("Created", discord.utils.format_dt(message.created_at, style="f"), True),
            ("Pinned", str(getattr(message, "pinned", False)), True),
            ("Embeds", str(embc), True),
            ("Attachments", attachments, False),
        ]
        if deleter:
            fields.insert(0, ("Deleted By", f"{deleter.mention} • `{deleter.id}`", True))
        if ref_id:
            fields.append(("Reply To", f"[{ref_id}]({ref_jump})", True))

        await self._send_embed(
            message.guild,
            event_key="message_delete",
            title="Message Deleted",
            description=message.content or "*no content*",
            fields=fields,
            color=discord.Color.red(),
            url=getattr(message, "jump_url", None),
        )

        if cats.get("snipe", True) and not message.author.bot:
            await self.config.guild(message.guild).snipes.set_raw(
                str(message.channel.id),
                value={
                    "author_id": getattr(message.author, "id", None),
                    "content": message.content,
                    "attachments": [a.url for a in getattr(message, "attachments", [])],
                    "ts": now_utc().isoformat(),
                },
            )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not (guild and await self._enabled(guild)):
            return
        cats = await self._cat(guild, "messages")
        if not cats or not cats.get("delete", True):
            return
        fields = [
            ("Channel", f"<#{payload.channel_id}> • `{payload.channel_id}`", True),
            ("Message ID", f"`{payload.message_id}`", True),
            ("Note", "Message not cached — content unavailable.", False),
        ]
        await self._send_embed(
            guild,
            event_key="message_delete",
            title="Message Deleted (Uncached)",
            description="The message was not cached; content unknown.",
            fields=fields,
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: List[discord.Message]):
        if not messages:
            return
        guild = messages[0].guild
        if not (guild and await self._enabled(guild)):
            return
        cats = await self._cat(guild, "messages")
        if not cats or not cats.get("purge", True):
            return
        channel = messages[0].channel
        first = min(m.created_at for m in messages)
        last = max(m.created_at for m in messages)

        actor = None
        with contextlib.suppress(Exception):
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.message_bulk_delete):
                if getattr(e.extra, "channel", None) and e.extra.channel.id == channel.id:
                    actor = e.user
                    break

        fields = [
            ("Channel", f"{channel.mention} • `{channel.id}`", True),
            ("Count", str(len(messages)), True),
            ("Window", f"{discord.utils.format_dt(first,'t')} → {discord.utils.format_dt(last,'t')}", True),
        ]
        if actor:
            fields.insert(0, ("Purged By", f"{actor.mention} • `{actor.id}`", True))

        await self._send_embed(
            guild,
            event_key="purge",
            title="Bulk Delete",
            description=f"{len(messages)} messages purged.",
            fields=fields,
            color=discord.Color.red(),
        )

        async with self.config.guild(guild).prune_count.get_lock():
            n = await self.config.guild(guild).prune_count()
            await self.config.guild(guild).prune_count.set(n + len(messages))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id or 0)
        if not (guild and await self._enabled(guild)):
            return
        cats = await self._cat(guild, "messages")
        if not cats or not cats.get("purge", True):
            return
        await self._send_embed(
            guild,
            event_key="purge",
            title="Bulk Delete",
            description=f"{len(payload.message_ids)} messages purged in <#{payload.channel_id}>",
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not (after.guild and await self._enabled(after.guild)):
            return
        if after.author and after.author.bot:
            return
        cats = await self._cat(after.guild, "messages")
        if not cats or not cats.get("edit", True):
            return
        if before.content == after.content:
            return
        await self._send_embed(
            after.guild,
            event_key="message_edit",
            title="Message Edited",
            description=f"In {chn(after.channel)} by {u(after.author)}",
            fields=[
                ("Before", limit(before.content, 1000), False),
                ("After",  limit(after.content, 1000), False),
                ("Message ID", f"`{after.id}`", True),
            ],
            color=discord.Color.orange(),
            url=after.jump_url,
        )

    # ----- Reactions -----
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: Union[discord.User, discord.Member]):
        g = reaction.message.guild
        if not (g and await self._enabled(g)):
            return
        if not await self._cat(g, "reactions"):
            return
        await self._send_embed(
            g,
            event_key="reaction",
            title="Reaction Added",
            description=f"{u(user)} reacted in {chn(reaction.message.channel)}",
            fields=[("Emoji", str(reaction.emoji), True), ("Message ID", f"`{reaction.message.id}`", True)],
        )

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction: discord.Reaction, user: Union[discord.User, discord.Member]):
        g = reaction.message.guild
        if not (g and await self._enabled(g)):
            return
        if not await self._cat(g, "reactions"):
            return
        await self._send_embed(
            g,
            event_key="reaction",
            title="Reaction Removed",
            description=f"{u(user)} removed a reaction in {chn(reaction.message.channel)}",
            fields=[("Emoji", str(reaction.emoji), True), ("Message ID", f"`{reaction.message.id}`", True)],
        )

    # ----- Members -----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = member.guild
        if not (await self._enabled(g)):
            return
        cats = await self._cat(g, "members")
        if not cats or not cats.get("join", True):
            return
        await self._send_embed(
            g,
            event_key="member_join",
            title="Member Joined",
            description=member.mention,
            fields=[("User", u(member), True)],
            thumbnail_url=member.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        g = member.guild
        if not (await self._enabled(g)):
            return
        cats = await self._cat(g, "members")
        if not cats or not cats.get("leave", True):
            return

        # Detect kick
        mod, reason = await self._recent_kick_for(g, member.id)
        kicked = mod is not None

        # Times
        joined = getattr(member, "joined_at", None)
        created = getattr(member, "created_at", None)
        fmt_joined_abs = discord.utils.format_dt(joined, style="f") if joined else "*unknown*"
        fmt_joined_rel = discord.utils.format_dt(joined, style="R") if joined else ""
        fmt_created_abs = discord.utils.format_dt(created, style="f") if created else "*unknown*"
        fmt_created_rel = discord.utils.format_dt(created, style="R") if created else ""

        tenure = ""
        if joined:
            delta = (now_utc() - (joined if joined.tzinfo else joined.replace(tzinfo=dt.timezone.utc)))
            tenure = f"{max(0, delta.days)} days"

        # Role snapshot
        roles = [r for r in getattr(member, "roles", []) if r.name != "@everyone"]
        roles.sort(key=lambda r: (-r.position, r.name.lower()))
        role_count = len(roles)
        if role_count == 0:
            roles_value = "*none*"
        elif role_count <= 15:
            roles_value = ", ".join(r.mention for r in roles)
        else:
            roles_value = ", ".join(r.mention for r in roles[:15]) + f" … (+{role_count - 15} more)"

        title = "Member Kicked" if kicked else "Member Left"
        color = discord.Color.red() if kicked else discord.Color.blurple()
        icon_key = "kick" if kicked else "member_leave"

        fields = [
            ("User", f"{member.mention} ({member})", False),
            ("Joined", f"{fmt_joined_abs} {f'({fmt_joined_rel})' if fmt_joined_rel else ''}", True),
            ("Account Created", f"{fmt_created_abs} {f'({fmt_created_rel})' if fmt_created_rel else ''}", True),
            ("Server Tenure", tenure or "*n/a*", True),
            (f"Roles ({role_count})", roles_value, False),
        ]
        if kicked:
            fields.insert(1, ("Kicked By", f"{mod.mention} • `{mod.id}`" if isinstance(mod, discord.User) else "Unknown", True))
            fields.insert(2, ("Reason", reason or "*none provided*", False))
            await self._new_case(g, action="kick", target=member, moderator=mod, reason=reason)

        await self._send_embed(
            g,
            event_key=icon_key,
            title=title,
            description=member.mention,
            fields=fields,
            color=color,
            thumbnail_url=member.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if not (await self._enabled(g)):
            return
        cats = await self._cat(g, "members")
        if not cats or not cats.get("update", True):
            return

        # Role/nick
        added, removed = _role_diff(before.roles, after.roles)
        nick_changed = before.nick != after.nick

        # Timeout change
        b_to = getattr(before, "timed_out_until", None)
        a_to = getattr(after, "timed_out_until", None)
        timeout_changed = b_to != a_to

        if timeout_changed:
            mod, reason, audit_expires = await self._recent_timeout_for(g, after.id)
            expires_at = a_to or audit_expires
            expires_str_abs = discord.utils.format_dt(expires_at, style="f") if expires_at else "*unknown*"
            expires_str_rel = f" ({discord.utils.format_dt(expires_at, style='R')})" if expires_at else ""
            dur_str = "*n/a*"
            if expires_at:
                try:
                    rem = (expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=dt.timezone.utc)) - now_utc()
                    secs = max(0, int(rem.total_seconds()))
                    days, rem2 = divmod(secs, 86400)
                    hours, rem2 = divmod(rem2, 3600)
                    minutes, _ = divmod(rem2, 60)
                    parts = []
                    if days: parts.append(f"{days}d")
                    if hours: parts.append(f"{hours}h")
                    if minutes or not parts: parts.append(f"{minutes}m")
                    dur_str = " ".join(parts)
                except Exception:
                    pass

            if a_to and not b_to:
                # set
                fields = [
                    ("User", f"{after.mention} ({after})", False),
                    ("Moderator", f"{mod.mention} • `{mod.id}`" if mod else "Unknown", True),
                    ("Reason", reason or "*none*", False),
                    ("Expires", f"{expires_str_abs}{expires_str_rel}", True),
                    ("Duration (remaining)", dur_str, True),
                ]
                await self._send_embed(
                    g,
                    event_key="timeout",
                    title="Member Timed Out",
                    description=after.mention,
                    fields=fields,
                    color=discord.Color.red(),
                    thumbnail_url=after.display_avatar.url,
                )
                remaining_secs = None
                if expires_at:
                    try:
                        remaining_secs = max(0, int(((expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=dt.timezone.utc)) - now_utc()).total_seconds()))
                    except Exception:
                        pass
                await self._new_case(g, action="timeout", target=after, moderator=mod, reason=reason, duration=remaining_secs)

            elif (not a_to) and b_to:
                # cleared
                fields = [
                    ("User", f"{after.mention} ({after})", False),
                    ("Moderator", f"{mod.mention} • `{mod.id}`" if mod else "Unknown", True),
                    ("Reason", reason or "*none*", False),
                ]
                await self._send_embed(
                    g,
                    event_key="untimeout",
                    title="Timeout Cleared",
                    description=after.mention,
                    fields=fields,
                    color=discord.Color.green(),
                    thumbnail_url=after.display_avatar.url,
                )
                await self._new_case(g, action="untimeout", target=after, moderator=mod, reason=reason)

            else:
                # updated
                fields = [
                    ("User", f"{after.mention} ({after})", False),
                    ("Moderator", f"{mod.mention} • `{mod.id}`" if mod else "Unknown", True),
                    ("Reason", reason or "*none*", False),
                    ("New Expiry", f"{expires_str_abs}{expires_str_rel}", True),
                    ("Duration (remaining)", dur_str, True),
                ]
                await self._send_embed(
                    g,
                    event_key="timeout",
                    title="Timeout Updated",
                    description=after.mention,
                    fields=fields,
                    color=discord.Color.orange(),
                    thumbnail_url=after.display_avatar.url,
                )
                await self._new_case(g, action="timeout_update", target=after, moderator=mod, reason=reason)

        # role/nick embed (skip if we just sent a timeout embed to avoid spam)
        if (added or removed or nick_changed) and not timeout_changed:
            if added and not removed:
                title = "User roles added"; color = discord.Color.green()
            elif removed and not added:
                title = "User roles removed"; color = discord.Color.red()
            elif added and removed:
                title = "User roles updated"; color = discord.Color.yellow()
            else:
                title = "Member updated"; color = discord.Color.blurple()

            actor = None
            if added or removed:
                with contextlib.suppress(Exception):
                    async for e in g.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
                        if e.target and e.target.id == after.id:
                            actor = e.user
                            break

            fields: List[Tuple[str, str, bool]] = [("User", f"{after.mention} ({after})", False)]
            if added:   fields.append(("Added", _role_mentions(added), False))
            if removed: fields.append(("Removed", _role_mentions(removed), False))
            if nick_changed: fields.append(("Nickname", f"`{before.nick}` → `{after.nick}`", False))
            footer = f"by {actor}" if actor else None

            await self._send_embed(
                g,
                event_key="member_update",
                title=title,
                description="",
                fields=fields,
                color=color,
                footer=footer,
                thumbnail_url=after.display_avatar.url,
            )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: Union[discord.User, discord.Member]):
        if not (await self._enabled(guild)):
            return
        cats = await self._cat(guild, "members")
        if not cats or not cats.get("ban", True):
            return
        actor = None
        reason = None
        with contextlib.suppress(Exception):
            async for entry in guild.audit_logs(limit=4, action=discord.AuditLogAction.ban):
                if entry.target.id == user.id:
                    actor = entry.user
                    reason = entry.reason
                    break

        created = getattr(user, "created_at", None)
        created_abs = discord.utils.format_dt(created, style="f") if created else "*unknown*"
        created_rel = f" ({discord.utils.format_dt(created,'R')})" if created else ""

        await self._new_case(guild, action="ban", target=user, moderator=actor, reason=reason)
        await self._send_embed(
            guild,
            event_key="ban",
            title="Member Banned",
            description=f"{getattr(user,'mention',str(user))}",
            fields=[
                ("By", u(actor) if actor else "Unknown", True),
                ("Reason", reason or "*none*", False),
                ("Account Created", f"{created_abs}{created_rel}", True),
            ],
            color=discord.Color.red(),
            thumbnail_url=user.display_avatar.url if isinstance(user, (discord.Member, discord.User)) else None,
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: Union[discord.User, discord.Member]):
        if not (await self._enabled(guild)):
            return
        cats = await self._cat(guild, "members")
        if not cats or not cats.get("unban", True):
            return
        actor = None
        reason = None
        with contextlib.suppress(Exception):
            async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.unban):
                if entry.target.id == user.id:
                    actor = entry.user
                    reason = entry.reason
                    break
        await self._new_case(guild, action="unban", target=user, moderator=actor, reason=reason)
        await self._send_embed(
            guild,
            event_key="unban",
            title="Member Unbanned",
            description=u(user),
            fields=[("By", u(actor) if actor else "Unknown", True), ("Reason", reason or "*none*", False)],
            thumbnail_url=user.display_avatar.url if isinstance(user, (discord.Member, discord.User)) else None,
        )

    # ----- Roles -----
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        g = role.guild
        if not (await self._enabled(g)) or not await self._cat(g, "roles"):
            return
        await self._send_embed(g, event_key="role", title="Role Created", description=role.mention, fields=[("Role ID", f"`{role.id}`", True)])

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        g = role.guild
        if not (await self._enabled(g)) or not await self._cat(g, "roles"):
            return
        await self._send_embed(g, event_key="role", title="Role Deleted", description=role.name, fields=[("Role ID", f"`{role.id}`", True)])

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        g = after.guild
        if not (await self._enabled(g)) or not await self._cat(g, "roles"):
            return
        diffs = []
        if before.name != after.name:
            diffs.append(f"**Name**: {before.name} → {after.name}")
        if before.color != after.color:
            diffs.append(f"**Color**: {before.color} → {after.color}")
        if before.mentionable != after.mentionable:
            diffs.append(f"**Mentionable**: {before.mentionable} → {after.mentionable}")
        if before.permissions.value != after.permissions.value:
            added_bits = after.permissions.value & ~before.permissions.value
            removed_bits = before.permissions.value & ~after.permissions.value
            added = [p for p, v in discord.Permissions(added_bits) if v]
            removed = [p for p, v in discord.Permissions(removed_bits) if v]
            if added:
                diffs.append(f"**Perms Added**: {', '.join(added)}")
            if removed:
                diffs.append(f"**Perms Removed**: {', '.join(removed)}")
        if not diffs:
            return
        await self._send_embed(
            g,
            event_key="role",
            title="Role Updated",
            description=after.mention,
            fields=[("Changes", "\n".join(diffs), False), ("Role ID", f"`{after.id}`", True)],
        )

    # ----- Channels / Threads -----
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        g = channel.guild
        if not (await self._enabled(g)) or not await self._cat(g, "channels"):
            return
        await self._send_embed(g, event_key="channel", title="Channel Created", description=chn(channel))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        g = channel.guild
        if not (await self._enabled(g)) or not await self._cat(g, "channels"):
            return
        name = getattr(channel, "name", "?")
        await self._send_embed(g, event_key="channel", title="Channel Deleted", description=f"{name} (`{channel.id}`)")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        g = after.guild
        if not (await self._enabled(g)) or not await self._cat(g, "channels"):
            return
        diffs = []
        if getattr(before, "name", None) != getattr(after, "name", None):
            diffs.append(f"**Name**: {getattr(before,'name',None)} → {getattr(after,'name',None)}")
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            if before.slowmode_delay != after.slowmode_delay:
                diffs.append(f"**Slowmode**: {before.slowmode_delay}s → {after.slowmode_delay}s")
            if before.nsfw != after.nsfw:
                diffs.append(f"**NSFW**: {before.nsfw} → {after.nsfw}")
            if before.topic != after.topic:
                diffs.append(f"**Topic**:\n{limit(before.topic,200)}\n→\n{limit(after.topic,200)}")
        if isinstance(before, discord.VoiceChannel) and isinstance(after, discord.VoiceChannel):
            if before.bitrate != after.bitrate:
                diffs.append(f"**Bitrate**: {before.bitrate} → {after.bitrate}")
            if before.user_limit != after.user_limit:
                diffs.append(f"**User Limit**: {before.user_limit} → {after.user_limit}")
        if not diffs:
            return
        await self._send_embed(
            g,
            event_key="channel",
            title="Channel Updated",
            description=f"{after.mention} • `{after.id}`",
            fields=[("Changes", "\n".join(diffs), False)],
        )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        g = thread.guild
        if not (await self._enabled(g)) or not await self._cat(g, "threads"):
            return
        await self._send_embed(g, event_key="thread", title="Thread Created", description=thread.name, fields=[("Parent", chn(thread.parent), True)])

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        g = after.guild
        if not (await self._enabled(g)) or not await self._cat(g, "threads"):
            return
        diffs = []
        if before.name != after.name:
            diffs.append(f"**Name**: {before.name} → {after.name}")
        if before.archived != after.archived:
            diffs.append(f"**Archived**: {before.archived} → {after.archived}")
        if before.locked != after.locked:
            diffs.append(f"**Locked**: {before.locked} → {after.locked}")
        if not diffs:
            return
        await self._send_embed(g, event_key="thread", title="Thread Updated", description=after.name, fields=[("Changes", "\n".join(diffs), False)])

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        g = thread.guild
        if not (await self._enabled(g)) or not await self._cat(g, "threads"):
            return
        await self._send_embed(g, event_key="thread", title="Thread Deleted", description=thread.name)

    # ----- Emojis / Stickers -----
    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        if not (await self._enabled(guild)) or not await self._cat(guild, "emojis"):
            return
        await self._send_embed(guild, event_key="emoji", title="Emojis Updated", description=f"{len(before)} → {len(after)}")

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before, after):
        if not (await self._enabled(guild)) or not await self._cat(guild, "stickers"):
            return
        await self._send_embed(guild, event_key="sticker", title="Stickers Updated", description=f"{len(before)} → {len(after)}")

    # ----- Invites / Webhooks / Integrations -----
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        g = invite.guild
        if not (g and await self._enabled(g)) or not await self._cat(g, "invites"):
            return
        await self._send_embed(g, event_key="invite", title="Invite Created", description=f"`{invite.code}` for {chn(invite.channel)}")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        g = invite.guild
        if not (g and await self._enabled(g)) or not await self._cat(g, "invites"):
            return
        await self._send_embed(g, event_key="invite", title="Invite Deleted", description=f"`{invite.code}`")

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        g = channel.guild
        if not (await self._enabled(g)) or not await self._cat(g, "webhooks"):
            return
        await self._send_embed(g, event_key="webhook", title="Webhooks Updated", description=chn(channel))

    @commands.Cog.listener()
    async def on_integration_update(self, guild: discord.Guild):
        if not (await self._enabled(guild)) or not await self._cat(guild, "integrations"):
            return
        await self._send_embed(guild, event_key="integration", title="Integrations Updated", description=guild.name)

    # ----- Scheduled / Stage -----
    @commands.Cog.listener()
    async def on_guild_scheduled_event_create(self, event: discord.GuildScheduledEvent):
        g = event.guild
        if not (await self._enabled(g)) or not await self._cat(g, "scheduled_events"):
            return
        await self._send_embed(g, event_key="scheduled", title="Scheduled Event Created", description=event.name)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_update(self, before: discord.GuildScheduledEvent, after: discord.GuildScheduledEvent):
        g = after.guild
        if not (await self._enabled(g)) or not await self._cat(g, "scheduled_events"):
            return
        await self._send_embed(g, event_key="scheduled", title="Scheduled Event Updated", description=after.name)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_delete(self, event: discord.GuildScheduledEvent):
        g = event.guild
        if not (await self._enabled(g)) or not await self._cat(g, "scheduled_events"):
            return
        await self._send_embed(g, event_key="scheduled", title="Scheduled Event Deleted", description=event.name)

    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage: discord.StageInstance):
        g = stage.guild
        if not (await self._enabled(g)) or not await self._cat(g, "stage"):
            return
        await self._send_embed(g, event_key="stage", title="Stage Created", description=stage.topic or "No topic")

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before: discord.StageInstance, after: discord.StageInstance):
        g = after.guild
        if not (await self._enabled(g)) or not await self._cat(g, "stage"):
            return
        await self._send_embed(g, event_key="stage", title="Stage Updated", description=after.topic or "No topic")

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage: discord.StageInstance):
        g = stage.guild
        if not (await self._enabled(g)) or not await self._cat(g, "stage"):
            return
        await self._send_embed(g, event_key="stage", title="Stage Deleted", description=stage.topic or "No topic")

    # ----- Voice / Presence / Guild -----
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        g = member.guild
        if not (await self._enabled(g)) or not await self._cat(g, "voice"):
            return
        if before.channel == after.channel and before.self_stream == after.self_stream and before.self_video == after.self_video:
            return
        desc = f"{u(member)}\n{chn(before.channel)} → {chn(after.channel)}"
        await self._send_embed(g, event_key="voice", title="Voice State Changed", description=desc, thumbnail_url=member.display_avatar.url)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if not (await self._enabled(g)) or not await self._cat(g, "presence"):
            return
        if str(before.status) == str(after.status):
            return
        await self._send_embed(
            g,
            event_key="presence",
            title="Presence Updated",
            description=f"{u(after)}: {before.status} → {after.status}",
            thumbnail_url=after.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        g = after
        if not (await self._enabled(g)) or not await self._cat(g, "server"):
            return
        diffs = []
        if before.name != after.name:
            diffs.append(f"**Name**: {before.name} → {after.name}")
        if before.afk_timeout != after.afk_timeout:
            diffs.append(f"**AFK Timeout**: {before.afk_timeout} → {after.afk_timeout}")
        if not diffs:
            return
        await self._send_embed(g, event_key="guild", title="Guild Updated", description=g.name, fields=[("Changes", "\n".join(diffs), False)])

    # ----- AutoMod & Gateway -----
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        g = entry.guild
        if not (g and await self._enabled(g)):
            return
        cats = await self._cat(g, "automod")
        if cats and cats.get("rules", True) and str(entry.action).startswith("AuditLogAction.auto_moderation_rule_"):
            await self._send_embed(
                g,
                event_key="automod_rules",
                title=f"AutoMod Rule {entry.action.name.split('_')[-1].title()}",
                description=f"By: {u(entry.user)}",
                fields=[("Changes", "\n".join(str(c) for c in (entry.changes or [])) or "*n/a*", False)],
                color=discord.Color.dark_teal(),
            )

    async def _on_automod_action_execution(self, payload):  # discord.py ≥2.6, if gateway exposes it
        g = getattr(payload, "guild", None) or self.bot.get_guild(getattr(payload, "guild_id", 0))
        if not (g and await self._enabled(g)):
            return
        cats = await self._cat(g, "automod")
        if not cats or not cats.get("execution", True):
            return
        user = getattr(payload, "user", None)
        matched = getattr(payload, "matched_content", None) or getattr(payload, "content", None)
        rule_id = getattr(payload, "rule_id", None)
        await self._send_embed(
            g,
            event_key="automod_action",
            title="AutoMod Action Executed",
            description=f"User: {u(user)}\nRule ID: `{rule_id}`",
            fields=[("Content", limit(str(matched), 1000), False)],
            color=discord.Color.dark_red(),
        )

    # lifecycle
    def cog_load(self):
        with contextlib.suppress(Exception):
            self.bot.add_listener(self._on_automod_action_execution, "on_automod_action_execution")

    def cog_unload(self):
        with contextlib.suppress(Exception):
            self.bot.remove_listener(self._on_automod_action_execution, "on_automod_action_execution")
