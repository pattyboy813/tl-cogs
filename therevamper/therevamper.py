# revamp_sync.py
# Red-DiscordBot Cog — Mirror roles, channels, and permission overwrites
# Source (revamp) ➜ Target (main) with dry-run, and **two-server confirmation**
#
# Added in this version:
# - Exact role ordering (matches source bottom→top) using relative moves
# - **Both servers must confirm** before applying (2/2 acks)
# - Live progress updates (edits the embed with counters/step)
# - Basic rate-limit friendliness (throttled ops + simple backoff)
# - Optional **lockdown**: temporarily deny @everyone from sending messages in target while applying
# - Quick command: `[p]revamp <target_id> [mode] [include_deletes] [lockdown]` (source is current server)
#
# Requirements: Red v3.5+ (discord.py 2.0+). Bot must be in BOTH guilds with Administrator.

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord import ui
from redbot.core import commands

__red_end_user_data_statement__ = "This cog stores no personal user data."  # noqa: N816


# ------------------------------ Data structures ------------------------------
@dataclass
class RoleAction:
    kind: str  # create|update|delete|skipped
    name: str
    id: Optional[int] = None
    data: Optional[dict] = None
    changes: Optional[dict] = None
    reason: Optional[str] = None


@dataclass
class ChannelAction:
    kind: str  # category|channel
    op: str  # create|update|delete|skipped
    id: Optional[int] = None
    name: Optional[str] = None
    type: Optional[discord.ChannelType] = None
    parent_name: Optional[str] = None
    what: Optional[str] = None


@dataclass
class Plan:
    key: str
    source: discord.Guild
    target: discord.Guild
    requested_by: discord.abc.User
    include_deletes: bool
    lockdown: bool = False
    role_actions: List[RoleAction] = field(default_factory=list)
    channel_actions: List[ChannelAction] = field(default_factory=list)
    role_id_map: Dict[int, int] = field(default_factory=dict)  # src role id -> tgt role id
    cat_id_map: Dict[int, int] = field(default_factory=dict)  # src cat id -> tgt cat id
    chan_id_map: Dict[int, int] = field(default_factory=dict)  # src chan id -> tgt chan id
    warnings: List[str] = field(default_factory=list)
    confirmed_guilds: set[int] = field(default_factory=set)
    source_msg: Optional[discord.Message] = None
    target_msg: Optional[discord.Message] = None
    lockdown_prev: Dict[int, discord.PermissionOverwrite] = field(default_factory=dict)  # ch.id -> prev overwrite for @everyone


# ------------------------------ Utility helpers ------------------------------
async def find_announce_channel(guild: discord.Guild, bot: discord.Client) -> Optional[discord.TextChannel]:
    # Prefer system channel, else the first sendable text channel
    chan = guild.system_channel
    if chan and chan.permissions_for(guild.me).send_messages:
        return chan
    for c in guild.text_channels:
        try:
            if c.permissions_for(guild.me).send_messages:
                return c
        except Exception:
            continue
    return None


def strip_role(r: discord.Role) -> dict:
    return {
        "name": r.name,
        "color": r.color.value,
        "hoist": r.hoist,
        "mentionable": r.mentionable,
        "permissions": r.permissions.value,
    }


def role_diff(target_like: dict, source_like: dict) -> Tuple[bool, dict]:
    changes = {}
    differs = False
    for k in ("color", "hoist", "mentionable", "permissions"):
        if target_like.get(k) != source_like.get(k):
            changes[k] = {"from": target_like.get(k), "to": source_like.get(k)}
            differs = True
    return differs, changes


def channel_needs_update(tgt: discord.abc.GuildChannel, src: discord.abc.GuildChannel) -> bool:
    try:
        if tgt.type != src.type:
            return True
        # text-like
        t_topic = getattr(tgt, "topic", None)
        s_topic = getattr(src, "topic", None)
        if (t_topic or "") != (s_topic or ""):
            return True
        t_nsfw = getattr(tgt, "nsfw", False)
        s_nsfw = getattr(src, "nsfw", False)
        if bool(t_nsfw) != bool(s_nsfw):
            return True
        t_rl = getattr(tgt, "slowmode_delay", 0) or getattr(tgt, "rate_limit_per_user", 0)
        s_rl = getattr(src, "slowmode_delay", 0) or getattr(src, "rate_limit_per_user", 0)
        if int(t_rl or 0) != int(s_rl or 0):
            return True
        if (tgt.category.name if tgt.category else None) != (src.category.name if src.category else None):
            return True
        # voice-like
        for attr in ("bitrate", "user_limit"):
            if getattr(tgt, attr, None) != getattr(src, attr, None):
                return True
    except Exception:
        return True
    return False


def _step_icon(step: str) -> str:
    return {
        "Starting…": "🟡",
        "Target locked down for maintenance": "🔒",
        "Creating roles…": "👤",
        "Updating roles…": "👤",
        "Deleting extra roles…": "🧹",
        "Reordering roles…": "📚",
        "Deleting channels…": "🧹",
        "Creating categories…": "🗂️",
        "Creating channels…": "📺",
        "Updating channels…": "🛠️",
        "Setting overwrites…": "🔐",
        "Unlocking target…": "🔓",
    }.get(step, "🔧")


# ------------------------------ Confirmation View ------------------------------
class ConfirmView(ui.View):
    def __init__(self, cog: "RevampSync", plan_key: str, for_guild_id: int, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.plan_key = plan_key
        self.for_guild_id = for_guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # permissions gate
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You must be an Administrator to confirm.", ephemeral=True)
            return False
        return True

    @ui.button(label="Confirm Apply (this server)", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):  # noqa: D401
        plan = self.cog.pending_plans.get(self.plan_key)
        if not plan:
            return await interaction.response.send_message("This sync request expired.", ephemeral=True)
        if interaction.guild and interaction.guild.id not in {plan.source.id, plan.target.id}:
            return await interaction.response.send_message("This confirmation is not for your server.", ephemeral=True)

        # Mark confirmation for this guild
        if interaction.guild:
            plan.confirmed_guilds.add(interaction.guild.id)
        conf_count = len(plan.confirmed_guilds)

        # Update both messages to show status
        summary = self.cog.plan_summary_embed(plan, mode="apply", include_deletes=plan.include_deletes)
        try:
            if plan.source_msg:
                await plan.source_msg.edit(embed=summary, view=self.cog.build_view(plan, plan.source.id))
            if plan.target_msg:
                await plan.target_msg.edit(embed=summary, view=self.cog.build_view(plan, plan.target.id))
        except Exception:
            pass

        if conf_count < 2:
            return await interaction.response.send_message("Confirmation recorded for this server. Waiting for the other server (need 2/2).", ephemeral=True)

        # Both sides confirmed — apply
        await interaction.response.defer(thinking=True)
        try:
            result_embed = await self.cog.apply_plan(plan)
            self.cog.pending_plans.pop(self.plan_key, None)
            # Edit both messages final
            try:
                if plan.source_msg:
                    await plan.source_msg.edit(embed=result_embed, view=None)
                if plan.target_msg:
                    await plan.target_msg.edit(embed=result_embed, view=None)
            except Exception:
                pass
            # Also send a final message here
            await interaction.followup.send(embed=result_embed)
            self.stop()
        except Exception as e:
            await interaction.followup.send(f"❌ Apply failed: {e}")
            self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        plan = self.cog.pending_plans.pop(self.plan_key, None)
        await interaction.response.send_message("Sync cancelled. No changes were made.")
        if plan:
            try:
                cancelled = discord.Embed(title="❎ Sync Cancelled", color=discord.Color.red())
                if plan.source_msg:
                    await plan.source_msg.edit(embed=cancelled, view=None)
                if plan.target_msg:
                    await plan.target_msg.edit(embed=cancelled, view=None)
            except Exception:
                pass
        self.stop()


# ------------------------------ The Cog ------------------------------
class RevampSync(commands.Cog):
    """Mirror roles, channels, and role overwrites from a revamp server to a main server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_plans: Dict[str, Plan] = {}
        # Tunables for rate limiting / progress
        self.rate_delay: float = 0.6  # seconds between mutating API calls
        self.progress_every: int = 6  # edit progress after N operations
        self.progress_min_secs: float = 2.5  # or after this many seconds

    # Quick command at group root
    @commands.hybrid_group(name="revamp", invoke_without_command=True)
    @commands.guild_only()
    async def revamp_group(
        self,
        ctx: commands.Context,
        target_guild_id: Optional[int] = None,
        mode: str = "apply",
        include_deletes: Optional[bool] = False,
        lockdown: Optional[bool] = True,
    ):
        """Quick command: `[p]revamp <target_id> [mode] [include_deletes] [lockdown]`
        - If `target_id` is provided, assumes **source = this server** and runs the sync flow.
        - Otherwise, shows help.
        """
        if target_guild_id:
            try:
                await self.revamp_sync.callback(  # type: ignore
                    self, ctx, ctx.guild.id, int(target_guild_id), mode, include_deletes, lockdown
                )
            except Exception as e:
                await ctx.send(f"Error: {e}")
            return
        await ctx.send_help()

    @revamp_group.command(name="sync", with_app_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def revamp_sync(
        self,
        ctx: commands.Context,
        source_guild_id: int,
        target_guild_id: int,
        mode: str = "dry",
        include_deletes: Optional[bool] = False,
        lockdown: Optional[bool] = True,
    ):
        """
        Build a plan to mirror roles/channels/overwrites from SOURCE to TARGET.
        Nothing changes until BOTH servers press **Confirm Apply** (2/2 required).

        mode: "dry" (default) or "apply" (still requires both confirmations)
        include_deletes: also delete roles/channels in target that don't exist in source
        lockdown: temporarily deny @everyone from sending messages in target while applying
        """
        source = self.bot.get_guild(source_guild_id)
        target = self.bot.get_guild(target_guild_id)
        if not source or not target:
            return await ctx.send("I must be in both guilds. Check the IDs and that the bot has access.")

        me_src = source.me
        me_tgt = target.me
        if not (me_src.guild_permissions.administrator and me_tgt.guild_permissions.administrator):
            return await ctx.send("I need **Administrator** permission in both guilds.")

        await ctx.typing()
        plan = await self.build_plan(source, target, include_deletes=bool(include_deletes), requested_by=ctx.author, lockdown=bool(lockdown))
        plan_key = plan.key
        self.pending_plans[plan_key] = plan

        summary = self.plan_summary_embed(plan, mode=mode, include_deletes=bool(include_deletes))

        # Post interactive messages in BOTH servers
        src_view = self.build_view(plan, source.id)
        tgt_view = self.build_view(plan, target.id)

        plan.source_msg = await ctx.send(
            content=("DRY RUN — no changes will be made until both servers Confirm." if mode.lower() == "dry" else "READY — review and have both servers press **Confirm Apply**."),
            embed=summary,
            view=src_view,
        )

        target_channel_msg = await self.post_with_view(target, summary, tgt_view, prefix=f"Sync requested by {ctx.author} from {ctx.guild.name if ctx.guild else 'unknown'}")
        plan.target_msg = target_channel_msg

        async def expire_later():
            await asyncio.sleep(900)
            if self.pending_plans.pop(plan_key, None):
                cancelled = discord.Embed(title="⏲️ Sync request expired", color=discord.Color.orange())
                try:
                    if plan.source_msg:
                        await plan.source_msg.edit(embed=cancelled, view=None)
                    if plan.target_msg:
                        await plan.target_msg.edit(embed=cancelled, view=None)
                except Exception:
                    pass
        self.bot.loop.create_task(expire_later())

    def build_view(self, plan: Plan, for_guild_id: int) -> ui.View:
        v = ConfirmView(self, plan.key, for_guild_id)
        confirmed_here = for_guild_id in plan.confirmed_guilds
        for child in v.children:
            if isinstance(child, ui.Button) and child.label.startswith("Confirm") and confirmed_here:
                child.disabled = True
        return v

    async def post_with_view(self, guild: discord.Guild, embed: discord.Embed, view: ui.View, prefix: Optional[str] = None) -> Optional[discord.Message]:
        ch = await find_announce_channel(guild, guild._state._get_client())  # type: ignore
        if not ch:
            return None
        try:
            return await ch.send(content=prefix, embed=embed, view=view)
        except Exception:
            return None

    # ------------------------------ Planning ------------------------------
    async def build_plan(
        self,
        source: discord.Guild,
        target: discord.Guild,
        include_deletes: bool,
        requested_by: discord.abc.User,
        lockdown: bool = False,
    ) -> Plan:
        key = f"{source.id}:{target.id}:{int(time.time())}"
        plan = Plan(
            key=key,
            source=source,
            target=target,
            requested_by=requested_by,
            include_deletes=include_deletes,
            lockdown=lockdown,
        )

        src_roles = {r.id: r for r in (await source.fetch_roles())}
        tgt_roles = {r.id: r for r in (await target.fetch_roles())}
        tgt_by_name = {r.name: r for r in tgt_roles.values() if not r.managed}

        for r in sorted(src_roles.values(), key=lambda x: x.position):
            if r.id == source.default_role.id:
                continue
            if r.managed:
                plan.warnings.append(f"Skipping managed role: {r.name}")
                continue
            t = tgt_by_name.get(r.name)
            if not t:
                plan.role_actions.append(RoleAction("create", r.name, data=strip_role(r)))
            else:
                differs, changes = role_diff(strip_role(t), strip_role(r))
                if differs:
                    plan.role_actions.append(RoleAction("update", r.name, id=t.id, changes=changes))
                plan.role_id_map[r.id] = t.id

        if include_deletes:
            for r in tgt_roles.values():
                if r.id == target.default_role.id or r.managed:
                    continue
                if not any(sr.name == r.name for sr in src_roles.values()):
                    plan.role_actions.append(RoleAction("delete", r.name, id=r.id))

        src_chans = await source.fetch_channels()
        tgt_chans = await target.fetch_channels()

        src_cats = [c for c in src_chans if isinstance(c, discord.CategoryChannel)]
        tgt_cats = [c for c in tgt_chans if isinstance(c, discord.CategoryChannel)]
        tgt_cat_by_name = {c.name: c for c in tgt_cats}

        for cat in sorted(src_cats, key=lambda c: c.position):
            existing = tgt_cat_by_name.get(cat.name)
            if not existing:
                plan.channel_actions.append(ChannelAction("category", "create", name=cat.name))
            else:
                plan.cat_id_map[cat.id] = existing.id
                if existing.position != cat.position:
                    plan.channel_actions.append(ChannelAction("category", "update", id=existing.id, name=cat.name, what="position"))

        if include_deletes:
            for tgtcat in tgt_cats:
                if tgtcat.name not in {c.name for c in src_cats}:
                    plan.channel_actions.append(ChannelAction("category", "delete", id=tgtcat.id, name=tgtcat.name))

        def composite_key(ch: discord.abc.GuildChannel) -> str:
            parent = ch.category.name if ch.category else "__ROOT__"
            return f"{parent}|{ch.type}|{ch.name}"

        tgt_by_key = {composite_key(c): c for c in tgt_chans if not isinstance(c, discord.CategoryChannel)}

        src_noncat = [c for c in src_chans if not isinstance(c, discord.CategoryChannel)]
        for src in sorted(src_noncat, key=lambda c: c.position):
            keyk = composite_key(src)
            existing = tgt_by_key.get(keyk)
            if not existing:
                plan.channel_actions.append(
                    ChannelAction("channel", "create", name=src.name, type=src.type, parent_name=(src.category.name if src.category else None))
                )
            else:
                plan.chan_id_map[src.id] = existing.id
                if channel_needs_update(existing, src):
                    plan.channel_actions.append(
                        ChannelAction("channel", "update", id=existing.id, name=src.name, type=src.type, parent_name=(src.category.name if src.category else None), what="config/position/overwrites")
                    )

        if include_deletes:
            for tgtc in [c for c in tgt_chans if not isinstance(c, discord.CategoryChannel)]:
                parent = tgtc.category.name if tgtc.category else "__ROOT__"
                keyt = f"{parent}|{tgtc.type}|{tgtc.name}"
                if not any(keyt == composite_key(s) for s in src_noncat):
                    plan.channel_actions.append(ChannelAction("channel", "delete", id=tgtc.id, name=tgtc.name, type=tgtc.type))

        return plan

    def plan_summary_embed(self, plan: Plan, mode: str, include_deletes: bool) -> discord.Embed:
        roles_create = sum(1 for a in plan.role_actions if a.kind == "create")
        roles_update = sum(1 for a in plan.role_actions if a.kind == "update")
        roles_delete = sum(1 for a in plan.role_actions if a.kind == "delete")
        chans_create = sum(1 for c in plan.channel_actions if c.op == "create")
        chans_update = sum(1 for c in plan.channel_actions if c.op == "update")
        chans_delete = sum(1 for c in plan.channel_actions if c.op == "delete")

        confs = len(plan.confirmed_guilds)
        need = 2

        emb = discord.Embed(
            title="Revamp → Main Sync Plan",
            description=(
                f"From **{discord.utils.escape_markdown(plan.source.name)}** ➜ **{discord.utils.escape_markdown(plan.target.name)}**\n"
                f"Requested by {plan.requested_by.mention}\n"
                f"Mode: **{'DRY RUN' if mode.lower() == 'dry' else 'APPLY'}** | Include deletes: **{'Yes' if include_deletes else 'No'}** | Lockdown: **{'Yes' if plan.lockdown else 'No'}**\n"
                f"Confirmations: **{confs}/{need}** (need both servers)"
            ),
            color=discord.Color.blurple(),
        )
        emb.add_field(name="Roles", value=f"Create: **{roles_create}** | Update: **{roles_update}** | Delete: **{roles_delete}**", inline=False)
        emb.add_field(name="Channels", value=f"Create: **{chans_create}** | Update: **{chans_update}** | Delete: **{chans_delete}**", inline=False)
        if plan.warnings:
            preview = "\n".join(f"• {w}" for w in plan.warnings[:6])
            if len(plan.warnings) > 6:
                preview += f"\n… +{len(plan.warnings)-6} more"
            emb.add_field(name="Warnings", value=preview, inline=False)
        emb.set_footer(text="Nothing will change until both servers press Confirm Apply.")
        emb.timestamp = discord.utils.utcnow()
        return emb

    # ------------------------------ Application ------------------------------
    async def apply_plan(self, plan: Plan) -> discord.Embed:
        source, target = plan.source, plan.target
        results = {"role_create": 0, "role_update": 0, "role_delete": 0, "chan_create": 0, "chan_update": 0, "chan_delete": 0}

        # Live progress control
        last_edit = time.monotonic()
        ops_since = 0

        async def progress(step: str):
            nonlocal last_edit, ops_since
            now = time.monotonic()
            if (ops_since >= self.progress_every) or (now - last_edit >= self.progress_min_secs):
                emb = self._progress_embed(plan, results, step)
                try:
                    if plan.source_msg:
                        await plan.source_msg.edit(embed=emb, view=self.build_view(plan, plan.source.id))
                    if plan.target_msg:
                        await plan.target_msg.edit(embed=emb, view=self.build_view(plan, plan.target.id))
                except Exception:
                    pass
                last_edit = now
                ops_since = 0

        async def do(coro, on_error_note: str):
            nonlocal ops_since
            retries = 5
            delay = 1.0
            while True:
                try:
                    out = await coro
                    await asyncio.sleep(self.rate_delay)
                    ops_since += 1
                    return out
                except discord.HTTPException as e:
                    if retries <= 0:
                        plan.warnings.append(f"{on_error_note}: {e}")
                        return None
                    await asyncio.sleep(delay)
                    retries -= 1
                    delay = min(delay * 2, 8.0)
                except Exception as e:
                    plan.warnings.append(f"{on_error_note}: {e}")
                    return None

        await progress("Starting…")

        # Optional lockdown at start
        if plan.lockdown:
            await self._toggle_lockdown(plan, enable=True)
            await progress("Target locked down for maintenance")

        # Roles: create / update
        tgt_roles = {r.id: r for r in (await target.fetch_roles())}
        for action in plan.role_actions:
            if action.kind == "create" and action.data:
                created = await do(target.create_role(
                        name=action.data["name"],
                        colour=discord.Colour(action.data["color"]),
                        hoist=action.data["hoist"],
                        mentionable=action.data["mentionable"],
                        permissions=discord.Permissions(action.data["permissions"]),
                        reason="Revamp sync: create role",
                    ), f"Failed to create role {action.data.get('name')}")
                if created:
                    results["role_create"] += 1
                    await progress("Creating roles…")
            elif action.kind == "update" and action.id and action.changes:
                try:
                    tgt = target.get_role(action.id)
                    if not tgt:
                        continue
                    edited = await do(tgt.edit(
                        colour=discord.Colour(action.changes.get("color", {}).get("to", tgt.colour.value)),
                        hoist=action.changes.get("hoist", {}).get("to", tgt.hoist),
                        mentionable=action.changes.get("mentionable", {}).get("to", tgt.mentionable),
                        permissions=discord.Permissions(action.changes.get("permissions", {}).get("to", tgt.permissions.value)),
                        reason="Revamp sync: update role",
                    ), f"Failed to update role {action.name}")
                    if edited is not None:
                        results["role_update"] += 1
                        await progress("Updating roles…")
                except Exception as e:
                    plan.warnings.append(f"Failed to update role {action.name}: {e}")
            elif action.kind == "delete" and action.id:
                try:
                    tgt = target.get_role(action.id)
                    if tgt:
                        deleted = await do(tgt.delete(reason="Revamp sync: remove role not in source"), f"Failed to delete role {action.name}")
                        if deleted is not None:
                            results["role_delete"] += 1
                            await progress("Deleting extra roles…")
                except Exception as e:
                    plan.warnings.append(f"Failed to delete role {action.name}: {e}")

        # Refresh role mapping by name
        fresh_tgt_roles = await target.fetch_roles()
        by_name = {r.name: r for r in fresh_tgt_roles if not r.managed}
        for sr in (await source.fetch_roles()):
            if sr.id == source.default_role.id or sr.managed:
                continue
            tr = by_name.get(sr.name)
            if tr:
                plan.role_id_map[sr.id] = tr.id

        # Reorder roles to match source EXACTLY (within bot limits)
        await self._reorder_roles_like_source(plan)
        await progress("Reordering roles…")

        # Channels: optional deletes first (children before parents)
        if plan.include_deletes:
            t_all = await target.fetch_channels()
            for ch in sorted([c for c in t_all if not isinstance(c, discord.CategoryChannel)], key=lambda c: c.position, reverse=True):
                ok = await do(ch.delete(reason="Revamp sync: remove channel not in source"), f"Failed to delete channel {ch.name}")
                if ok is not None:
                    results["chan_delete"] += 1
                    await progress("Deleting channels…")
            for cat in sorted([c for c in t_all if isinstance(c, discord.CategoryChannel)], key=lambda c: c.position, reverse=True):
                ok = await do(cat.delete(reason="Revamp sync: remove category not in source"), f"Failed to delete category {cat.name}")
                if ok is not None:
                    results["chan_delete"] += 1
                    await progress("Deleting channels…")

        # Ensure categories exist and order them
        src_cats = [c for c in await source.fetch_channels() if isinstance(c, discord.CategoryChannel)]
        tgt_cats = [c for c in await target.fetch_channels() if isinstance(c, discord.CategoryChannel)]
        by_name_cat = {c.name: c for c in tgt_cats}

        for cat in sorted(src_cats, key=lambda c: c.position):
            existing = by_name_cat.get(cat.name)
            if not existing:
                created = await do(target.create_category(name=cat.name, reason="Revamp sync: create category"), f"Failed to create category {cat.name}")
                if created:
                    plan.cat_id_map[cat.id] = created.id
                    results["chan_create"] += 1
                    await progress("Creating categories…")
            else:
                plan.cat_id_map[cat.id] = existing.id
                if existing.position != cat.position:
                    await do(existing.edit(position=cat.position), f"Category order issue for {cat.name}")

        # Create/update channels
        src_all = [c for c in await source.fetch_channels() if not isinstance(c, discord.CategoryChannel)]
        tgt_all = [c for c in await target.fetch_channels() if not isinstance(c, discord.CategoryChannel)]

        def find_target_match(src: discord.abc.GuildChannel) -> Optional[discord.abc.GuildChannel]:
            for c in tgt_all:
                if c.name == src.name and c.type == src.type and ((c.category.name if c.category else None) == (src.category.name if src.category else None)):
                    return c
            return None

        created_map: Dict[int, discord.abc.GuildChannel] = {}

        for src in sorted(src_all, key=lambda c: c.position):
            existing = find_target_match(src)
            parent_id = plan.cat_id_map.get(src.category_id) if hasattr(src, "category_id") and src.category_id else None
            try:
                if not existing:
                    if src.type is discord.ChannelType.text:
                        ch = await do(target.create_text_channel(
                            name=src.name,
                            category=target.get_channel(parent_id) if parent_id else None,
                            topic=getattr(src, "topic", None),
                            nsfw=getattr(src, "nsfw", False),
                            slowmode_delay=getattr(src, "slowmode_delay", 0) or getattr(src, "rate_limit_per_user", 0),
                            reason="Revamp sync: create channel",
                        ), f"Failed to create text channel {src.name}")
                    elif src.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                        ch = await do(target.create_voice_channel(
                            name=src.name,
                            category=target.get_channel(parent_id) if parent_id else None,
                            bitrate=getattr(src, "bitrate", None),
                            user_limit=getattr(src, "user_limit", None),
                            reason="Revamp sync: create channel",
                        ), f"Failed to create voice channel {src.name}")
                    else:
                        ch = await do(target.create_text_channel(
                            name=src.name,
                            category=target.get_channel(parent_id) if parent_id else None,
                            reason="Revamp sync: create channel (fallback)",
                        ), f"Failed to create channel {src.name}")
                    if ch:
                        created_map[src.id] = ch
                        results["chan_create"] += 1
                        await progress("Creating channels…")
                else:
                    kwargs = {"reason": "Revamp sync: update channel"}
                    if hasattr(existing, "edit"):
                        if hasattr(existing, "category"):
                            kwargs["category"] = target.get_channel(parent_id) if parent_id else None
                        if hasattr(existing, "topic"):
                            kwargs["topic"] = getattr(src, "topic", None)
                        if hasattr(existing, "nsfw"):
                            kwargs["nsfw"] = getattr(src, "nsfw", False)
                        if hasattr(existing, "slowmode_delay"):
                            kwargs["slowmode_delay"] = getattr(src, "slowmode_delay", 0) or getattr(src, "rate_limit_per_user", 0)
                        if hasattr(existing, "bitrate"):
                            kwargs["bitrate"] = getattr(src, "bitrate", None)
                        if hasattr(existing, "user_limit"):
                            kwargs["user_limit"] = getattr(src, "user_limit", None)
                        await do(existing.edit(**kwargs), f"Failed to update channel {src.name}")
                        await do(existing.edit(position=src.position), f"Failed to set position for {src.name}")
                        plan.chan_id_map[src.id] = existing.id
                        created_map[src.id] = existing
                        results["chan_update"] += 1
                        await progress("Updating channels…")
            except Exception as e:
                plan.warnings.append(f"Channel create/update issue for {src.name}: {e}")

        # Permission overwrites: mirror ROLE overwrites only (@everyone included)
        for src in src_all:
            try:
                tgt_ch = created_map.get(src.id)
                if not tgt_ch:
                    continue
                ow_map = {}
                for target_obj, po in src.overwrites.items():
                    if isinstance(target_obj, discord.Role):
                        target_role = (
                            target.default_role if target_obj == source.default_role else target.get_role(plan.role_id_map.get(target_obj.id, 0))
                        )
                        if not target_role:
                            continue
                        allow = discord.Permissions(); deny = discord.Permissions()
                        a, d = po.pair()
                        allow.value = int(a.value); deny.value = int(d.value)
                        ow_map[target_role] = discord.PermissionOverwrite.from_pair(allow, deny)
                await do(tgt_ch.edit(overwrites=ow_map, reason="Revamp sync: mirror role overwrites"), f"Overwrite issue in {getattr(src, 'name', 'unknown')}")
                await progress("Setting overwrites…")
            except Exception as e:
                plan.warnings.append(f"Overwrite issue in {getattr(src, 'name', 'unknown')}: {e}")

        # Restore lockdown if enabled
        if plan.lockdown:
            await self._toggle_lockdown(plan, enable=False)
            await progress("Unlocking target…")

        emb = discord.Embed(
            title="✅ Revamp → Main Sync Completed",
            color=discord.Color.green(),
        )
        emb.add_field(
            name="Roles",
            value=f"Created: **{results['role_create']}** | Updated: **{results['role_update']}** | Deleted: **{results['role_delete']}**",
            inline=False,
        )
        emb.add_field(
            name="Channels",
            value=f"Created: **{results['chan_create']}** | Updated: **{results['chan_update']}** | Deleted: **{results['chan_delete']}**",
            inline=False,
        )
        if plan.warnings:
            preview = "\n".join(f"• {w}" for w in plan.warnings[:10])
            if len(plan.warnings) > 10:
                preview += f"\n… +{len(plan.warnings)-10} more"
            emb.add_field(name="Notes", value=preview, inline=False)
        emb.timestamp = discord.utils.utcnow()
        return emb

    async def _reorder_roles_like_source(self, plan: Plan) -> None:
        """Reorder target guild roles to match source order exactly (as much as permissions allow).
        Uses *relative* moves (above=@everyone, then above the previously placed role)
        to avoid off-by-one/top-vs-bottom confusion with absolute positions.
        """
        target = plan.target
        source = plan.source
        try:
            src_sorted = [r for r in await source.fetch_roles() if not r.managed and r.id != source.default_role.id]
            src_sorted.sort(key=lambda r: r.position)  # bottom→top

            to_place: List[discord.Role] = []
            for sr in src_sorted:
                tgt_id = plan.role_id_map.get(sr.id)
                if not tgt_id:
                    continue
                tr = target.get_role(tgt_id)
                if tr is None or tr.managed:
                    continue
                to_place.append(tr)

            anchor: discord.Role = target.default_role
            for role in to_place:
                try:
                    await role.move(above=anchor, reason="Revamp sync: reorder roles (relative)")
                    await asyncio.sleep(self.rate_delay)
                    anchor = role
                except Exception as e:
                    plan.warnings.append(f"Unable to move role {role.name}: {e}")
        except Exception as e:
            plan.warnings.append(f"Role reordering issues: {e}")

    # ------- Progress embed + Lockdown helpers -------
    def _progress_embed(self, plan: Plan, results: dict, step: str) -> discord.Embed:
        emb = self.plan_summary_embed(plan, mode="apply", include_deletes=plan.include_deletes)
        emb.add_field(name="Progress", value=(
            f"{_step_icon(step)} {step}\n"
            f"Roles — C:{results['role_create']} U:{results['role_update']} D:{results['role_delete']}\n"
            f"Chans — C:{results['chan_create']} U:{results['chan_update']} D:{results['chan_delete']}"
        ), inline=False)
        return emb

    async def _toggle_lockdown(self, plan: Plan, enable: bool) -> None:
        target = plan.target
        everyone = target.default_role
        if enable:
            plan.lockdown_prev.clear()
        channels = [c for c in await target.fetch_channels() if isinstance(c, discord.TextChannel)]
        for ch in channels:
            try:
                current = ch.overwrites_for(everyone)
                if enable:
                    plan.lockdown_prev[ch.id] = current
                    new = current
                    new.send_messages = False
                    new.add_reactions = False
                    await ch.set_permissions(everyone, overwrite=new, reason="Revamp sync: lockdown")
                else:
                    prev = plan.lockdown_prev.get(ch.id, current)
                    await ch.set_permissions(everyone, overwrite=prev, reason="Revamp sync: unlock")
                await asyncio.sleep(self.rate_delay)
            except Exception as e:
                plan.warnings.append(f"Lockdown change failed in #{ch.name}: {e}")

