from __future__ import annotations
import logging, traceback
from typing import Any, Dict, Optional

import discord
from redbot.core import commands, checks, Config

from .embeds import build_embed
from .ui import SetupView

log = logging.getLogger("red.modlogv2")

CONF_IDENT = 0xA5D0F2C3BEEF2025

# Broad coverage; toggleable from UI
DEFAULT_EVENTS = {
    # messages
    "message_create": False,            # off by default (noisy)
    "message_edit": True,
    "message_delete": True,
    "message_bulk_delete": True,
    # reactions
    "reaction_add": True,
    "reaction_remove": True,
    "reaction_clear": True,
    # members
    "member_join": True,
    "member_remove": True,
    "member_update": True,
    # voice/presence (noisy -> default off)
    "voice_change": False,
    "presence_update": False,
    # roles
    "role_changes": True,
    # channels/threads
    "channel_changes": True,
    "thread_changes": True,
    # emojis/stickers
    "emoji_changes": True,
    "sticker_changes": True,
    # invites/webhooks/integrations
    "invites": True,
    "webhooks": True,
    "integrations": True,
    # scheduled events / stage / guild updates
    "scheduled_events": True,
    "stage": True,
    "guild_change": True,
    # commands (Red)
    "commands_used": False,
    # AutoMod
    "automod_rules": True,              # via audit log entry create
    "automod_action_execution": True,   # gateway event (if surfaced)
}

# ----- compact serializers / diff helpers -----
def _u(user: discord.abc.User | None):
    return {"id": user.id, "name": str(user), "bot": bool(getattr(user, "bot", False))} if user else None

def _ch(ch: discord.abc.GuildChannel | None):
    if not ch: return None
    base = {"id": ch.id, "name": getattr(ch, "name", None), "type": str(ch.type)}
    if isinstance(ch, discord.VoiceChannel):
        base.update({"user_limit": ch.user_limit, "bitrate": ch.bitrate})
    if isinstance(ch, discord.TextChannel):
        base.update({"nsfw": ch.nsfw, "slowmode": ch.slowmode_delay})
    return base

def _role(r: discord.Role | None):
    return {"id": r.id, "name": r.name, "color": r.color.value, "position": r.position, "hoist": r.hoist, "mentionable": r.mentionable} if r else None

def _overwrites(ch: discord.abc.GuildChannel | None):
    if not ch: return None
    out = []
    for target, ow in ch.overwrites.items():
        tgt = {"type": "role" if isinstance(target, discord.Role) else "member", "id": target.id, "name": getattr(target, "name", str(target))}
        allow, deny = ow.pair()
        out.append({"target": tgt, "allow": allow.value, "deny": deny.value})
    return out

def _diff_dict(old: dict, new: dict, keys: list[str]):
    changes = []
    for k in keys:
        if old.get(k) != new.get(k):
            changes.append({"field": k, "before": old.get(k), "after": new.get(k)})
    return changes


def _embed_to_plaintext(embed: discord.Embed, *, fallback: str) -> str:
    """Convert an embed into a plain text representation (for non-embed channels)."""

    def _is_value(value: Any) -> bool:
        return value not in (None, discord.Embed.Empty)

    parts: list[str] = []

    author = getattr(embed, "author", None)
    author_name = getattr(author, "name", None)
    if _is_value(author_name):
        parts.append(str(author_name))

    if _is_value(embed.title):
        parts.append(str(embed.title))
    if _is_value(embed.description):
        parts.append(str(embed.description))

    for field in embed.fields:
        name = (field.name or "").strip()
        value = (field.value or "").strip()
        if name and value:
            parts.append(f"{name}:\n{value}")
        elif value:
            parts.append(value)
        elif name:
            parts.append(name)

    footer = getattr(embed, "footer", None)
    footer_text = getattr(footer, "text", None)
    if _is_value(footer_text):
        parts.append(str(footer_text))

    text = "\n\n".join(part for part in parts if part).strip()
    if not text:
        text = fallback
    if len(text) > 2000:
        text = text[:1997] + "..."
    return text


class ModLogV2(commands.Cog):
    """Modern moderation logging with a one-command UI, AutoMod support, and Red Config-backed storage."""

    __author__ = "you"
    __version__ = "2.2.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_IDENT, force_registration=True)

        self._automod_listener_registered = False

        # Defaults
        default_global = {"event_counter": 0}  # global monotonic id for custom-group rows
        default_guild = {
            "enabled": True,
            "log_channel_id": None,
            "use_embeds": True,
            "events": DEFAULT_EVENTS.copy(),
        }

        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)

        # Custom group for events: key = (guild_id, event_id)
        self.config.init_custom("event", 2)

        # AutoMod action execution (discord.py 2.6.2 exposes this)
        try:
            self.bot.add_listener(self._on_automod_action_execution, "on_automod_action_execution")
            self._automod_listener_registered = True
            log.info("Registered on_automod_action_execution.")
        except Exception:
            log.debug("AutoMod action exec not available:\n%s", traceback.format_exc())

    def cog_unload(self):
        if self._automod_listener_registered:
            try:
                self.bot.remove_listener(self._on_automod_action_execution, "on_automod_action_execution")
            except Exception:
                log.debug("Failed to remove AutoMod listener:\n%s", traceback.format_exc())
            finally:
                self._automod_listener_registered = False

    # ---------------- Public Commands ----------------
    @commands.group(name="modlog", invoke_without_command=True)
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def modlog_root(self, ctx: commands.Context):
        """Open the setup UI."""
        data = await self._get_settings(ctx.guild.id)
        ch = ctx.guild.get_channel(data["log_channel_id"]) if data["log_channel_id"] else None
        await ctx.send(
            f"**ModLogV2** v{self.__version__}\n"
            f"Enabled: `{data['enabled']}` • Embeds: `{data['use_embeds']}` • "
            f"Log channel: {ch.mention if ch else 'Not set'}```Uses Red Config for persistence. If your Red instance uses the PostgreSQL backend, data lives in Postgres automatically.```",
            view=SetupView(self, ctx.guild),
        )

    # ---------------- Internals ----------------
    async def _get_settings(self, guild_id: int) -> Dict[str, Any]:
        current = await self.config.guild_from_id(guild_id).all()
        merged = {
            "enabled": current.get("enabled", True),
            "log_channel_id": current.get("log_channel_id", None),
            "use_embeds": current.get("use_embeds", True),
            "events": {**DEFAULT_EVENTS, **current.get("events", {})},
        }
        return merged

    async def _save_settings(self, guild_id: int, data: Dict[str, Any]):
        await self.config.guild_from_id(guild_id).set(data)

    async def _resolve_log_channel(self, guild: discord.Guild, preferred_id: int | None) -> discord.TextChannel | None:
        """Return a channel we can actually send to. Prefer configured; else fall back to first viable."""
        # Preferred first
        if preferred_id:
            ch = guild.get_channel(preferred_id)
            if isinstance(ch, discord.TextChannel):
                perms = ch.permissions_for(guild.me)
                if perms.send_messages and perms.embed_links:
                    return ch
        # Fallback
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                return ch
        return None

    async def _send_and_store(
        self,
        guild: discord.Guild,
        event: str,
        *,
        embed: Optional[discord.Embed] = None,
        content: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        force_embed: bool = False,  # set True at callsite for critical events
    ):
        settings = await self._get_settings(guild.id)
        if not settings["enabled"]:
            return

        preferred_id = settings.get("log_channel_id")
        dest = await self._resolve_log_channel(guild, preferred_id)

        send_kwargs: Dict[str, Any] = {}
        use_embed = bool(embed) and (force_embed or settings["use_embeds"])

        if content:
            send_kwargs["content"] = content

        if embed is not None and use_embed:
            send_kwargs["embed"] = embed

        if not send_kwargs:
            if embed is not None:
                fallback = f"{event.replace('_', ' ').title()}"
                send_kwargs["content"] = _embed_to_plaintext(embed, fallback=fallback)
            else:
                send_kwargs["content"] = f"{event.replace('_', ' ').title()}"

        # Post to channel (embed preferred/forced)
        if dest:
            try:
                await dest.send(**send_kwargs)
            except discord.Forbidden:
                # If preferred channel failed, try a fallback and warn
                alt = await self._resolve_log_channel(guild, None)
                if alt and (dest.id != alt.id):
                    try:
                        warn = build_embed(
                            guild=guild,
                            event="guild_update",
                            title="ModLogV2: configured log channel not writable",
                            description=(
                                f"Tried to send to <#{preferred_id}> but lack permissions or channel is missing.\n"
                                f"Posting here instead."
                            ),
                        )
                        await alt.send(embed=warn)
                        if force_embed and embed is not None and "embed" not in send_kwargs:
                            await alt.send(embed=embed)
                        else:
                            await alt.send(**send_kwargs)
                    except Exception:
                        log.exception("Fallback channel also failed")
                else:
                    log.warning("No channel available to send log message", extra={"guild_id": guild.id})
            except Exception:
                log.exception("Unexpected error sending log")
        else:
            log.warning("No text channel with send/embed perms found; cannot post log", extra={"guild_id": guild.id})

        # Persist event row via Config custom group (goes to Red’s backend)
        if payload is not None:
            async with self.config.event_counter.get_lock():
                eid = await self.config.event_counter()
                await self.config.event_counter.set(eid + 1)
            row = {"event": event, "payload": payload}
            await self.config.custom("event", guild.id, eid).set(row)

    # ---------------- Messages ----------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        events = (await self._get_settings(message.guild.id))["events"]
        if not events.get("message_create", False):
            return
        payload = {
            "channel_id": message.channel.id,
            "author": _u(message.author),
            "message_id": message.id,
            "content": message.content,
            "attachments": [a.to_dict() for a in message.attachments],
            "embeds": [e.to_dict() for e in message.embeds] if message.embeds else [],
        }
        await self._send_and_store(message.guild, "message_create", payload=payload)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or (after.author and after.author.bot):
            return
        events = (await self._get_settings(after.guild.id))["events"]
        if not events.get("message_edit", True) or before.content == after.content:
            return
        emb = build_embed(
            guild=after.guild, event="message_edit", title="Message edited",
            author=after.author,
            description=f"**Before**\n{(before.content or '*none*')[:1024]}\n\n**After**\n{(after.content or '*none*')[:3000]}",
            jump_url=after.jump_url,
        )
        payload = {
            "channel_id": getattr(after.channel, "id", None),
            "author_id": getattr(after.author, "id", None),
            "before": before.content, "after": after.content,
            "message_id": after.id,
        }
        # Force embed for visibility
        await self._send_and_store(after.guild, "message_edit", embed=emb, payload=payload, force_embed=True)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        # This fires only if the message was cached; raw handler below is the safety net
        if not message.guild or message.author.bot:
            return
        events = (await self._get_settings(message.guild.id))["events"]
        if not events.get("message_delete", True):
            return
        emb = build_embed(
            guild=message.guild, event="message_delete", title="Message deleted",
            author=message.author, description=(message.content or "*no content*")[:4000]
        )
        payload = {
            "channel_id": getattr(message.channel, "id", None),
            "author_id": getattr(message.author, "id", None),
            "content": message.content,
            "attachments": [a.to_dict() for a in message.attachments],
            "message_id": message.id,
            "cached": True,
        }
        await self._send_and_store(message.guild, "message_delete", embed=emb, payload=payload, force_embed=True)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages: return
        guild = messages[0].guild
        if not guild: return
        events = (await self._get_settings(guild.id))["events"]
        if not events.get("message_bulk_delete", True):
            return
        count = len(messages)
        channel = messages[0].channel
        emb = build_embed(
            guild=guild, event="message_bulk_delete", title="Bulk delete",
            description=f"{count} messages purged in {channel.mention}"
        )
        payload = {
            "channel_id": channel.id,
            "count": count,
            "message_ids": [m.id for m in messages if m],
            "author_ids": [getattr(m.author, "id", None) for m in messages if m],
            "cached": True,
        }
        await self._send_and_store(guild, "message_bulk_delete", embed=emb, payload=payload, force_embed=True)

    # ---------------- RAW delete safety net ----------------
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        events = (await self._get_settings(guild.id))["events"]
        if not events.get("message_delete", True):
            return

        channel = guild.get_channel(payload.channel_id)
        cached = payload.cached_message  # may be None
        author = getattr(cached, "author", None)
        content = getattr(cached, "content", None)

        emb = build_embed(
            guild=guild,
            event="message_delete",
            title="Message deleted",
            description=(content or "*content unknown (not cached)*")[:4000],
            author=author,
            jump_url=getattr(cached, "jump_url", None),
            fields=[
                ("Message ID", f"`{payload.message_id}`", True),
                ("Channel", channel.mention if isinstance(channel, discord.TextChannel) else f"<#{payload.channel_id}>", True),
            ],
        )

        pl = {
            "message_id": payload.message_id,
            "channel_id": payload.channel_id,
            "author_id": getattr(author, "id", None),
            "cached": bool(cached),
            "content": content,
        }
        await self._send_and_store(guild, "message_delete", embed=emb, payload=pl, force_embed=True)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        events = (await self._get_settings(guild.id))["events"]
        if not events.get("message_bulk_delete", True):
            return

        channel = guild.get_channel(payload.channel_id)
        count = len(payload.message_ids)

        emb = build_embed(
            guild=guild,
            event="message_bulk_delete",
            title="Bulk delete",
            description=f"{count} messages purged in "
                        f"{channel.mention if isinstance(channel, discord.TextChannel) else f'<#{payload.channel_id}>'}",
            fields=[(
                "Message IDs",
                ", ".join(f"`{i}`" for i in list(payload.message_ids)[:20]) + (" …" if count > 20 else ""),
                False
            )],
        )

        pl = {
            "channel_id": payload.channel_id,
            "count": count,
            "message_ids": list(payload.message_ids),
            "cached": bool(payload.cached_messages),
        }
        await self._send_and_store(guild, "message_bulk_delete", embed=emb, payload=pl, force_embed=True)

    # ---------------- Reactions ----------------
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User | discord.Member):
        msg = reaction.message
        if not msg.guild: return
        events = (await self._get_settings(msg.guild.id))["events"]
        if not events.get("reaction_add", True): return
        payload = {"message_id": msg.id, "channel_id": msg.channel.id, "user": _u(user), "emoji": str(reaction.emoji)}
        await self._send_and_store(msg.guild, "reaction_add", payload=payload)

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction: discord.Reaction, user: discord.User | discord.Member):
        msg = reaction.message
        if not msg.guild: return
        events = (await self._get_settings(msg.guild.id))["events"]
        if not events.get("reaction_remove", True): return
        payload = {"message_id": msg.id, "channel_id": msg.channel.id, "user": _u(user), "emoji": str(reaction.emoji)}
        await self._send_and_store(msg.guild, "reaction_remove", payload=payload)

    @commands.Cog.listener()
    async def on_reaction_clear(self, message: discord.Message, reactions):
        if not message.guild: return
        events = (await self._get_settings(message.guild.id))["events"]
        if not events.get("reaction_clear", True): return
        payload = {"message_id": message.id, "channel_id": message.channel.id, "reactions": [str(r.emoji) for r in reactions]}
        await self._send_and_store(message.guild, "reaction_clear", payload=payload)

    # ---------------- Members ----------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        events = (await self._get_settings(member.guild.id))["events"]
        if not events.get("member_join", True): return
        emb = build_embed(guild=member.guild, event="member_join", title="Member joined", author=member, description=f"{member.mention}")
        await self._send_and_store(member.guild, "member_join", embed=emb, payload={"user": _u(member)})

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        events = (await self._get_settings(member.guild.id))["events"]
        if not events.get("member_remove", True): return
        emb = build_embed(guild=member.guild, event="member_remove", title="Member left", author=member, description=f"{member.mention}")
        await self._send_and_store(member.guild, "member_remove", embed=emb, payload={"user": _u(member)})

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        events = (await self._get_settings(after.guild.id))["events"]
        if not events.get("member_update", True): return
        b = {"nick": before.nick, "roles": [r.id for r in before.roles], "pending": before.pending, "timed_out_until": getattr(before, "timed_out_until", None)}
        a = {"nick": after.nick, "roles": [r.id for r in after.roles], "pending": after.pending, "timed_out_until": getattr(after, "timed_out_until", None)}
        diffs = _diff_dict(b, a, ["nick", "roles", "pending", "timed_out_until"])
        if not diffs: return
        emb = build_embed(guild=after.guild, event="member_update", title="Member updated", author=after, description=after.mention,
                          fields=[("Changes", "\n".join(f"`{d['field']}`: {d['before']} → {d['after']}" for d in diffs)[:1024], False)])
        await self._send_and_store(after.guild, "member_update", embed=emb, payload={"user": _u(after), "diffs": diffs})

    # ---------------- Roles ----------------
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        events = (await self._get_settings(role.guild.id))["events"]
        if not events.get("role_changes", True): return
        emb = build_embed(guild=role.guild, event="role_create", title="Role created", description=role.mention)
        await self._send_and_store(role.guild, "role_create", embed=emb, payload={"role": _role(role)})

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        events = (await self._get_settings(role.guild.id))["events"]
        if not events.get("role_changes", True): return
        emb = build_embed(guild=role.guild, event="role_delete", title="Role deleted", description=role.name)
        await self._send_and_store(role.guild, "role_delete", embed=emb, payload={"role": {"id": role.id, "name": role.name}})

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        events = (await self._get_settings(after.guild.id))["events"]
        if not events.get("role_changes", True): return
        b = {"name": before.name, "color": before.color.value, "hoist": before.hoist, "mentionable": before.mentionable, "position": before.position, "permissions": before.permissions.value}
        a = {"name": after.name,  "color": after.color.value, "hoist": after.hoist, "mentionable": after.mentionable, "position": after.position, "permissions": after.permissions.value}
        diffs = _diff_dict(b, a, list(b.keys()))
        if not diffs: return
        emb = build_embed(guild=after.guild, event="role_update", title="Role updated", description=after.mention,
                          fields=[("Changes", "\n".join(f"`{d['field']}`: {d['before']} → {d['after']}" for d in diffs)[:1024], False)])
        await self._send_and_store(after.guild, "role_update", embed=emb, payload={"role_id": after.id, "diffs": diffs})

    # ---------------- Channels ----------------
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        events = (await self._get_settings(channel.guild.id))["events"]
        if not events.get("channel_changes", True): return
        emb = build_embed(guild=channel.guild, event="channel_create", title="Channel created", description=f"{channel.mention} • {channel.id}")
        await self._send_and_store(channel.guild, "channel_create", embed=emb, payload={"channel": _ch(channel), "overwrites": _overwrites(channel)})

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        events = (await self._get_settings(channel.guild.id))["events"]
        if not events.get("channel_changes", True): return
        emb = build_embed(guild=channel.guild, event="channel_delete", title="Channel deleted", description=f"#{getattr(channel, 'name', '?')} • {channel.id}")
        await self._send_and_store(channel.guild, "channel_delete", embed=emb, payload={"channel_id": channel.id, "name": getattr(channel, "name", None)})

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        events = (await self._get_settings(after.guild.id))["events"]
        if not events.get("channel_changes", True): return
        b = {"name": getattr(before, "name", None), "nsfw": getattr(before, "nsfw", None), "topic": getattr(before, "topic", None), "slowmode": getattr(before, "slowmode_delay", None)}
        a = {"name": getattr(after, "name", None),  "nsfw": getattr(after, "nsfw", None),  "topic": getattr(after, "topic", None),  "slowmode": getattr(after, "slowmode_delay", None)}
        diffs = _diff_dict(b, a, ["name","nsfw","topic","slowmode"])
        ow_b, ow_a = _overwrites(before), _overwrites(after)
        if ow_b != ow_a:
            diffs.append({"field":"overwrites","before":ow_b,"after":ow_a})
        if not diffs: return
        emb = build_embed(guild=after.guild, event="channel_update", title="Channel updated", description=f"{after.mention}",
                          fields=[("Changes", "\n".join(f"`{d['field']}` changed" for d in diffs)[:1024], False)])
        await self._send_and_store(after.guild, "channel_update", embed=emb, payload={"channel_id": after.id, "diffs": diffs})

    # ---------------- Emojis / Stickers ----------------
    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        events = (await self._get_settings(guild.id))["events"]
        if not events.get("emoji_changes", True): return
        payload = {"before": [e.id for e in before], "after": [e.id for e in after]}
        await self._send_and_store(guild, "emoji_update", payload=payload)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before, after):
        events = (await self._get_settings(guild.id))["events"]
        if not events.get("sticker_changes", True): return
        payload = {"before": [s.id for s in before], "after": [s.id for s in after]}
        await self._send_and_store(guild, "sticker_update", payload=payload)

    # ---------------- Invites / Webhooks / Integrations ----------------
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if not invite.guild: return
        events = (await self._get_settings(invite.guild.id))["events"]
        if not events.get("invites", True): return
        payload = {"code": invite.code, "channel_id": getattr(invite.channel, "id", None), "inviter": _u(invite.inviter), "max_uses": invite.max_uses, "max_age": invite.max_age, "temporary": invite.temporary}
        await self._send_and_store(invite.guild, "invite_create", payload=payload)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if not invite.guild: return
        events = (await self._get_settings(invite.guild.id))["events"]
        if not events.get("invites", True): return
        payload = {"code": invite.code, "channel_id": getattr(invite.channel, "id", None)}
        await self._send_and_store(invite.guild, "invite_delete", payload=payload)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        events = (await self._get_settings(channel.guild.id))["events"]
        if not events.get("webhooks", True): return
        await self._send_and_store(channel.guild, "webhooks_update", payload={"channel_id": channel.id})

    @commands.Cog.listener()
    async def on_integration_update(self, guild: discord.Guild):
        events = (await self._get_settings(guild.id))["events"]
        if not events.get("integrations", True): return
        await self._send_and_store(guild, "integration_update", payload={})

    # ---------------- Scheduled Events / Stage / Guild ----------------
    @commands.Cog.listener()
    async def on_guild_scheduled_event_create(self, event: discord.GuildScheduledEvent):
        ev = (await self._get_settings(event.guild.id))["events"]
        if not ev.get("scheduled_events", True): return
        await self._send_and_store(event.guild, "scheduled_event_create", payload={"id": event.id, "name": event.name, "start": str(event.start_time)})

    @commands.Cog.listener()
    async def on_guild_scheduled_event_update(self, before: discord.GuildScheduledEvent, after: discord.GuildScheduledEvent):
        ev = (await self._get_settings(after.guild.id))["events"]
        if not ev.get("scheduled_events", True): return
        diffs = _diff_dict(
            {"name": before.name, "start": str(before.start_time), "location": str(before.location)},
            {"name": after.name,  "start": str(after.start_time),  "location": str(after.location)},
            ["name","start","location"]
        )
        if diffs:
            await self._send_and_store(after.guild, "scheduled_event_update", payload={"id": after.id, "diffs": diffs})

    @commands.Cog.listener()
    async def on_guild_scheduled_event_delete(self, event: discord.GuildScheduledEvent):
        ev = (await self._get_settings(event.guild.id))["events"]
        if not ev.get("scheduled_events", True): return
        await self._send_and_store(event.guild, "scheduled_event_delete", payload={"id": event.id, "name": event.name})

    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage: discord.StageInstance):
        ev = (await self._get_settings(stage.guild.id))["events"]
        if not ev.get("stage", True): return
        await self._send_and_store(stage.guild, "stage_create", payload={"channel_id": stage.channel.id, "topic": stage.topic})

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before: discord.StageInstance, after: discord.StageInstance):
        ev = (await self._get_settings(after.guild.id))["events"]
        if not ev.get("stage", True): return
        diffs = _diff_dict({"topic": before.topic}, {"topic": after.topic}, ["topic"])
        if diffs:
            await self._send_and_store(after.guild, "stage_update", payload={"channel_id": after.channel.id, "diffs": diffs})

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage: discord.StageInstance):
        ev = (await self._get_settings(stage.guild.id))["events"]
        if not ev.get("stage", True): return
        await self._send_and_store(stage.guild, "stage_delete", payload={"channel_id": stage.channel.id})

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        events = (await self._get_settings(member.guild.id))["events"]
        if not events.get("voice_change", False): return
        payload = {
            "user": _u(member),
            "before": {"channel_id": getattr(before.channel, "id", None), "mute": before.mute, "deaf": before.deaf, "self_mute": before.self_mute, "self_deaf": before.self_deaf, "self_stream": before.self_stream, "self_video": before.self_video},
            "after":  {"channel_id": getattr(after.channel, "id", None),  "mute": after.mute,  "deaf": after.deaf,  "self_mute": after.self_mute,  "self_deaf": after.self_deaf,  "self_stream": after.self_stream,  "self_video": after.self_video},
        }
        await self._send_and_store(member.guild, "voice_state_update", payload=payload)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        events = (await self._get_settings(after.guild.id))["events"]
        if not events.get("presence_update", False): return
        payload = {"user": _u(after), "before": str(before.status), "after": str(after.status)}
        await self._send_and_store(after.guild, "presence_update", payload=payload)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        ev = (await self._get_settings(thread.guild.id))["events"]
        if not ev.get("thread_changes", True): return
        await self._send_and_store(thread.guild, "thread_create", payload={"id": thread.id, "name": thread.name, "parent_id": thread.parent_id})

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        ev = (await self._get_settings(after.guild.id))["events"]
        if not ev.get("thread_changes", True): return
        diffs = _diff_dict({"name": before.name, "archived": before.archived, "locked": before.locked},
                           {"name": after.name,  "archived": after.archived,  "locked": after.locked},
                           ["name","archived","locked"])
        if diffs:
            await self._send_and_store(after.guild, "thread_update", payload={"id": after.id, "diffs": diffs})

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        ev = (await self._get_settings(thread.guild.id))["events"]
        if not ev.get("thread_changes", True): return
        await self._send_and_store(thread.guild, "thread_delete", payload={"id": thread.id, "name": thread.name})

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        ev = (await self._get_settings(after.id))["events"]
        if not ev.get("guild_change", True): return
        b = {"name": before.name, "afk_timeout": before.afk_timeout, "system_channel_id": getattr(before.system_channel, "id", None)}
        a = {"name": after.name,  "afk_timeout": after.afk_timeout,  "system_channel_id": getattr(after.system_channel, "id", None)}
        diffs = _diff_dict(b, a, ["name","afk_timeout","system_channel_id"])
        if diffs:
            await self._send_and_store(after, "guild_update", payload={"diffs": diffs})

    # ---------------- Red Commands used ----------------
    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        if not ctx.guild:
            return
        ev = (await self._get_settings(ctx.guild.id))["events"]
        if not ev.get("commands_used", False): return
        payload = {"user": _u(ctx.author), "channel_id": getattr(ctx.channel, "id", None), "content": ctx.message.content, "qualified": ctx.command.qualified_name if ctx.command else None}
        await self._send_and_store(ctx.guild, "command_used", payload=payload)

    # ---------------- Audit Log: AutoMod rule changes ----------------
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if not entry.guild:
            return
        events = (await self._get_settings(entry.guild.id))["events"]
        if events.get("automod_rules", True) and str(entry.action).startswith("AuditLogAction.auto_moderation_rule_"):
            title = f"AutoMod rule {entry.action.name.split('_')[-1].title()}"
            desc = f"By: {entry.user.mention if entry.user else 'Unknown'}"
            emb = build_embed(guild=entry.guild, event="automod_rules", title=title, description=desc, author=entry.user)
            payload = {"action": entry.action.name, "user_id": getattr(entry.user, "id", None), "changes": [c.to_dict() for c in (entry.changes or [])]}
            await self._send_and_store(entry.guild, "automod_rules", embed=emb, payload=payload, force_embed=True)

    # ---------------- AutoMod execution (gateway) ----------------
    async def _on_automod_action_execution(self, payload):  # type: ignore
        try:
            guild = getattr(payload, "guild", None) or self.bot.get_guild(getattr(payload, "guild_id", 0))
            if not guild:
                return
            events = (await self._get_settings(guild.id))["events"]
            if not events.get("automod_action_execution", True):
                return
            user = getattr(payload, "user", None)
            matched = getattr(payload, "matched_content", None) or getattr(payload, "content", None)
            rule_id = getattr(payload, "rule_id", None)
            title = "AutoMod action executed"
            desc = (f"User: {user.mention if user else getattr(payload,'user_id','?')}\n"
                    f"Rule ID: `{rule_id}`\n"
                    f"Content: {matched!r}")[:3500]
            emb = build_embed(guild=guild, event="automod_action_execution", title=title, description=desc, author=user)
            pl = {"user_id": getattr(user, "id", None) or getattr(payload, "user_id", None),
                  "rule_id": rule_id, "content": matched}
            await self._send_and_store(guild, "automod_action_execution", embed=emb, payload=pl, force_embed=True)
        except Exception:
            log.exception("Failed handling automod execution")

    # ---------------- AutoMod preset (optional helper) ----------------
    async def _create_automod_preset(self, itx: discord.Interaction):
        try:
            guild = itx.guild
            # create rules where supported (discord.py 2.6.x exposes AutoMod HTTP)
            try:
                await guild.create_automod_rule(
                    name="ModLogV2 • Bad words",
                    event_type=1,  # message send
                    trigger={"type": 1, "keyword_filter": ["badword1", "badword2"]},
                    actions=[{"type": 1, "metadata": {"channel_id": itx.channel.id}}],
                    enabled=True,
                )
            except Exception:
                pass
            try:
                await guild.create_automod_rule(
                    name="ModLogV2 • Mention spam",
                    event_type=1,
                    trigger={"type": 5, "mention_total_limit": 6},
                    actions=[{"type": 3, "metadata": {"duration_seconds": 300}}],
                    enabled=True,
                )
            except Exception:
                pass
            await itx.response.send_message("AutoMod presets created (where supported).", ephemeral=True)
        except discord.Forbidden:
            await itx.response.send_message("I need **Manage Guild** to configure AutoMod.", ephemeral=True)
        except Exception as e:
            await itx.response.send_message(f"Couldn’t create rules: `{e}`", ephemeral=True)
