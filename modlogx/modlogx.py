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
    if mention:
        pretty = mention
    else:
        # fall back to #name
        name = getattr(o, "name", "?")
        pretty = f"#{name}"
    return f"{pretty} (`{o.id}`)"


@dataclass
class Case:
    id: int
    action: str
    target_id: int
    target_name: str
    mod_id: Optional[int]
    mod_name: Optional[str]
    reason: Optional[str]
    duration: Optional[int]  # seconds
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "mod_id": self.mod_id,
            "mod_name": self.mod_name,
            "reason": self.reason,
            "duration": self.duration,
            "created_at": self.created_at,
        }

# ------------------ Cog ------------------
class ModLogX(commands.Cog):
    """Next-gen modlog with webhook output, detailed embeds, cases, and audit correlation."""

    __author__ = "Pat"
    __version__ = "3.1.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA51D0ECAFE2025, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._audit_fetch_lock: dict[int, asyncio.Lock] = {}

    # ---------- Config util ----------
    async def _gdata(self, guild: discord.Guild) -> Dict[str, Any]:
        return await self.config.guild(guild).all()

    async def _enabled(self, guild: discord.Guild) -> bool:
        d = await self._gdata(guild)
        return bool(d["enabled"] and d["webhook_url"])

    async def _cat(self, guild: discord.Guild, name: str) -> Any:
        return (await self._gdata(guild))["categories"].get(name)

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
    ):
        data = await self._gdata(guild)
        if not (data["enabled"] and data["webhook_url"]):
            return

        embed = discord.Embed(title=title, description=limit(description, 4000), color=color, url=url)
        embed.timestamp = now_utc()
        for name, value, inline in fields:
            embed.add_field(name=name, value=limit(value, 1024), inline=inline)
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
        case = Case(
            id=cid,
            action=action,
            target_id=target.id,
            target_name=str(target),
            mod_id=getattr(moderator, "id", None),
            mod_name=str(moderator) if moderator else None,
            reason=reason,
            duration=duration,
            created_at=now_utc().isoformat(),
        )
        await self.config.guild(guild).cases.set_raw(str(cid), value=case.to_dict())

        if (await self._cat(guild, "moderation_cases")):
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
        return case

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
            f"Use `[p]modlogx setup #channel` to set the webhook.\n"
            f"Toggles: `[p]modlogx enable on/off`, `[p]modlogx embeds on/off`, `[p]modlogx identity bot|event`,\n"
            f"Sub-events: `[p]modlogx sub <category> <event> <on/off>`"
        )

    @group.command()
    async def setup(self, ctx: commands.Context, channel: discord.TextChannel):
        """Create or refresh the log webhook in a channel."""
        try:
            wh = await channel.create_webhook(name="ModLogX")
        except discord.Forbidden:
            return await ctx.send("❌ I need **Manage Webhooks** in that channel.")
        await self.config.guild(ctx.guild).webhook_url.set(wh.url)
        await ctx.send(f"✅ Webhook set for {channel.mention}. You're ready!")

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
        desc = (
            f"**Action**: {c['action']}\n"
            f"**Target**: <@{c['target_id']}> (`{c['target_id']}`)\n"
            f"**Moderator**: {(f"<@{c['mod_id']}>" if c.get('mod_id') else 'Unknown')}\n"
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
    # ----- Messages (no create) -----
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
                ("After", limit(after.content, 1000), False),
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
        await self._send_embed(g, event_key="member_join", title="Member Joined", description=member.mention, fields=[("User", u(member), True)])

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        g = member.guild
        if not (await self._enabled(g)):
            return
        cats = await self._cat(g, "members")
        if not cats or not cats.get("leave", True):
            return
        await self._send_embed(g, event_key="member_leave", title="Member Left", description=member.mention, fields=[("User", u(member), True)])

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if not (await self._enabled(g)):
            return
        cats = await self._cat(g, "members")
        if not cats or not cats.get("update", True):
            return

        def _role_names(roles: List[discord.Role]) -> set[str]:
            return {r.name for r in roles if r.name != "@everyone"}

        changes = []
        if before.nick != after.nick:
            changes.append(f"**Nickname**: `{before.nick}` → `{after.nick}`")

        if before.roles != after.roles:
            b = _role_names(before.roles)
            a = _role_names(after.roles)
            added = a - b
            removed = b - a
            if added:
                changes.append(f"**Roles Added**: {', '.join(sorted(added))}")
            if removed:
                changes.append(f"**Roles Removed**: {', '.join(sorted(removed))}")

        if getattr(before, "timed_out_until", None) != getattr(after, "timed_out_until", None):
            changes.append(f"**Timeout**: `{before.timed_out_until}` → `{after.timed_out_until}`")

        if not changes:
            return

        await self._send_embed(
            g,
            event_key="member_update",
            title="Member Updated",
            description=after.mention,
            fields=[("Changes", "\n".join(changes), False), ("User", f"{after.mention} • `{after.id}`", True)],
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
            async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.ban):
                if entry.target.id == user.id:
                    actor = entry.user
                    reason = entry.reason
                    break
        await self._new_case(guild, action="ban", target=user, moderator=actor, reason=reason)
        await self._send_embed(
            guild,
            event_key="ban",
            title="Member Banned",
            description=u(user),
            fields=[("By", u(actor) if actor else "Unknown", True), ("Reason", reason or "*none*", False)],
            color=discord.Color.red(),
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

    # ----- Scheduled / Stage / Guild -----
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
        await self._send_embed(g, event_key="voice", title="Voice State Changed", description=desc)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if not (await self._enabled(g)) or not await self._cat(g, "presence"):
            return
        if str(before.status) == str(after.status):
            return
        await self._send_embed(g, event_key="presence", title="Presence Updated", description=f"{u(after)}: {before.status} → {after.status}")

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
