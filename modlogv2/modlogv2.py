from __future__ import annotations
import logging, traceback
from typing import Any, Dict, Optional

import discord
from redbot.core import commands, checks, Config
from . import db
from .embeds import build_embed
from .ui import SetupView

log = logging.getLogger("red.modlogv2")

# Red config (for DSN only; all other state persists in Postgres)
CONF_IDENT = 0xA5D0F2C3BEEF2025

DEFAULT_EVENTS = db.DEFAULT_EVENTS

class ModLogV2(commands.Cog):
    """Modern moderation logging with a one-command UI, AutoMod support, and PostgreSQL logging."""
    __author__ = "you"
    __version__ = "2.0.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_IDENT, force_registration=True)
        self.config.register_global(dsn=None)

        # Try to register AutoMod action execution if d.py exposes it
        try:
            self.bot.add_listener(self._on_automod_action_execution, "on_automod_action_execution")
            log.info("Registered on_automod_action_execution (if supported by discord.py 2.6.2).")
        except Exception:
            log.debug("AutoMod action exec not available:\n%s", traceback.format_exc())

    # ---------------- Public Commands ----------------
    @commands.group(name="modlog", invoke_without_command=True)
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def modlog_root(self, ctx: commands.Context):
        """Open the setup UI."""
        # lazy DB init if DSN present
        dsn = await self.config.dsn()
        if dsn:
            try:
                await db.init_pool(dsn)
            except Exception as e:
                return await ctx.send(f"DB init failed: `{e}`")
        data = await self._get_settings(ctx.guild.id)
        ch = ctx.guild.get_channel(data["log_channel_id"]) if data["log_channel_id"] else None
        await ctx.send(
            f"**ModLogV2** v{self.__version__}\n"
            f"Enabled: `{data['enabled']}` • Embeds: `{data['use_embeds']}` • "
            f"Log channel: {ch.mention if ch else 'Not set'}\n"
            f"Click below to configure.",
            view=SetupView(self, ctx.guild),
        )

    @modlog_root.command(name="db")
    @commands.guild_only()
    @checks.admin()
    async def modlog_db(self, ctx: commands.Context):
        """DB helpers: set DSN / test / init."""
        await ctx.send(
            "Use the UI button **Set Postgres DSN**, or commands:\n"
            f"`{ctx.clean_prefix}modlog db setdsn postgresql://user:pass@host:5432/dbname`\n"
            f"`{ctx.clean_prefix}modlog db test`"
        )

    @modlog_db.command(name="setdsn")
    @commands.guild_only()
    @checks.admin()
    async def modlog_db_setdsn(self, ctx: commands.Context, dsn: str):
        await self._set_dsn(dsn)
        await ctx.tick()

    @modlog_db.command(name="test")
    @commands.guild_only()
    @checks.admin()
    async def modlog_db_test(self, ctx: commands.Context):
        dsn = await self.config.dsn()
        if not dsn:
            return await ctx.send("No DSN set.")
        try:
            await db.init_pool(dsn)
            await ctx.send("✅ Connected and schema ensured.")
        except Exception as e:
            await ctx.send(f"❌ DB error: `{e}`")

    # ---------------- Internals ----------------
    async def _set_dsn(self, dsn: str):
        await self.config.dsn.set(dsn)
        await db.init_pool(dsn)

    async def _get_settings(self, guild_id: int) -> Dict[str, Any]:
        try:
            return await db.load_settings(guild_id)
        except Exception:
            # fallback (pre-DB config)
            return dict(enabled=True, log_channel_id=None, use_embeds=True, events=DEFAULT_EVENTS.copy())

    async def _save_settings(self, guild_id: int, data: Dict[str, Any]):
        dsn = await self.config.dsn()
        if not dsn:
            return  # allow UI before DB is configured; nothing persisted yet
        await db.save_settings(guild_id, data)

    async def _send(self, guild: discord.Guild, event: str, *, embed: Optional[discord.Embed] = None, content: Optional[str] = None, payload: Optional[Dict[str, Any]] = None):
        settings = await self._get_settings(guild.id)
        if not settings["enabled"]:
            return
        # channel
        ch = guild.get_channel(settings["log_channel_id"]) if settings["log_channel_id"] else None
        if ch:
            try:
                await ch.send(content=content, embed=(embed if settings["use_embeds"] else None))
            except discord.Forbidden:
                log.warning("Forbidden sending to log channel", extra={"guild_id": guild.id, "channel_id": ch.id})
            except Exception:
                log.exception("Unexpected error sending log")
        # database
        try:
            if payload is not None:
                await db.write_event(guild.id, event, payload)
        except Exception:
            log.exception("Failed to write event to DB")

    # ---------------- Event listeners (examples) ----------------
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
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
        }
        await self._send(message.guild, "message_delete", embed=emb, payload=payload)
        log.info("modlog_event", extra={"event":"message_delete","guild_id":message.guild.id})

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or (after.author and after.author.bot):
            return
        events = (await self._get_settings(after.guild.id))["events"]
        if not events.get("message_edit", True):
            return
        if before.content == after.content:
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
        await self._send(after.guild, "message_edit", embed=emb, payload=payload)

    # ---- AutoMod rule changes (via audit log)
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if not entry.guild:
            return
        events = (await self._get_settings(entry.guild.id))["events"]
        if not events.get("automod_rules", True):
            return
        if str(entry.action).startswith("AuditLogAction.auto_moderation_rule_"):
            title = f"AutoMod rule {entry.action.name.split('_')[-1].title()}"
            desc = f"By: {entry.user.mention if entry.user else 'Unknown'}"
            emb = build_embed(guild=entry.guild, event="automod_rules", title=title, description=desc, author=entry.user)
            payload = {"action": entry.action.name, "user_id": getattr(entry.user, "id", None), "changes": [c.to_dict() for c in (entry.changes or [])]}
            await self._send(entry.guild, "automod_rules", embed=emb, payload=payload)

    # ---- AutoMod action execution (when surfaced by d.py)
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
            await self._send(guild, "automod_action_execution", embed=emb, payload=pl)
        except Exception:
            log.exception("Failed handling automod execution")

    # ---- Simple preset (keyword + mention spam)
    async def _create_automod_preset(self, itx: discord.Interaction):
        try:
            guild = itx.guild
            # create rules where supported (d.py 2.6.2 exposes HTTP for AutoMod)
            try:
                await guild.create_automod_rule(
                    name="ModLogV2 • Bad words",
                    event_type=1,
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
