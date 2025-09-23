# revamp_sync_sync_only.py
# Red-DiscordBot Cog — Revamp -> Main Mirror (sync-only, non-destructive, nice UI, fail-fast + auto-rollback)
# - Single command: !revamp (hybrid). Source = current guild; Target = another mutual guild.
# - Control panel with Apply/Cancel/Rollback.
# - Mode fixed to SYNC (non-destructive). No deletes. Preserves target overwrites.
# - Only creates/updates what’s needed; ADMIN never moved (only ensured).
# - Progress embed with rolling "Recent" ops + "Skipped" counter.
# - Rollback of last apply per-target (roles, channels, role-overwrites, positions, lockdown).
# - Auto-rollback on ANY failure (fail-fast). Toggle removed.
#
# This cog stores no personal user data.

from __future__ import annotations
import asyncio, time, copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

import discord
from discord import ui
from redbot.core import commands

__red_end_user_data_statement__ = "This cog stores no personal user data."

# ---------- config ----------
ADMIN_ANCHOR_NAME = "ADMIN"  # anchor role; never moved by this cog
# Optional: only mirror these category names from the source guild. Empty => all categories.
CATEGORY_SCOPE: List[str] = []  # e.g., ["Revamp • Announcements", "Revamp • Community"]

# ---------- helpers ----------
def _norm(s: str) -> str: return (s or "").strip().lower()

def _icon(step: str) -> str:
    return {
        "ready": "🟡", "start": "🟡", "lock": "🔒", "roles": "👤",
        "cats": "🗂️", "chans": "📺", "overwrites": "🔐",
        "threads": "🧵", "unlock": "🔓", "done": "✅", "rollback": "↩️"
    }.get(step, "🔧")

# Minimal role shape for diffs

def _strip_role(r: discord.Role) -> dict:
    return {
        "name": r.name, "key": _norm(r.name), "color": r.color.value,
        "hoist": r.hoist, "mentionable": r.mentionable, "permissions": r.permissions.value
    }


def _role_diff(tgt_like: dict, src_like: dict) -> Tuple[bool, dict]:
    ch, diff = {}, False
    for k in ("color", "hoist", "mentionable", "permissions"):
        if tgt_like.get(k) != src_like.get(k):
            ch[k] = {"from": tgt_like.get(k), "to": src_like.get(k)}
            diff = True
    return diff, ch


# Trimmed, version-tolerant channel change detector (preserve overwrites)

def _chan_changed(tgt, src) -> bool:
    try:
        if tgt.type != src.type:
            return True
        def parent_name(c):
            return c.category.name if getattr(c, "category", None) else None
        def slowmode(c):
            return getattr(c, "slowmode_delay", 0) or getattr(c, "rate_limit_per_user", 0) or 0
        cur = {
            "parent": parent_name(tgt),
            "topic": (getattr(tgt, "topic", None) or ""),
            "nsfw": bool(getattr(tgt, "nsfw", False)),
            "slow": int(slowmode(tgt)),
        }
        des = {
            "parent": parent_name(src),
            "topic": (getattr(src, "topic", None) or ""),
            "nsfw": bool(getattr(src, "nsfw", False)),
            "slow": int(slowmode(src)),
        }
        return cur != des
    except Exception:
        return True


# ---------- plan / changelog ----------
@dataclass
class RoleAction:
    kind: str  # create|update
    name: str
    id: Optional[int] = None
    data: Optional[dict] = None
    changes: Optional[dict] = None


@dataclass
class ChannelAction:
    kind: str  # category|channel
    op: str    # create|update
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
    sync_threads: bool = False
    mode: str = "sync"                # fixed: sync-only (preserve target overwrites)
    mirror_overwrites: bool = False    # sync => preserve
    auto_rollback: bool = True         # always on
    role_actions: List[RoleAction] = field(default_factory=list)
    channel_actions: List[ChannelAction] = field(default_factory=list)
    role_id_map: Dict[int, int] = field(default_factory=dict)
    cat_id_map: Dict[int, int] = field(default_factory=dict)
    chan_id_map: Dict[int, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    control_msg: Optional[discord.Message] = None


@dataclass
class ChangeLogEntry:
    kind: str               # role|channel|overwrites|roles_reorder|lockdown
    op: str                 # create|update|reorder|set
    payload: Dict[str, Any]


# ---------- basic safe reply ----------
async def _ireply(inter: discord.Interaction, content: Optional[str] = None,
                  *, embed: Optional[discord.Embed] = None, ephemeral: bool = False):
    try:
        if not inter.response.is_done():
            await inter.response.send_message(content or None, embed=embed, ephemeral=ephemeral)
        else:
            await inter.followup.send(content or None, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        try:
            if inter.channel:
                await inter.channel.send(content or None, embed=embed)
        except Exception:
            pass
    except Exception:
        pass


# ---------- the cog ----------
class RevampSync(commands.Cog):
    """
    Control panel to mirror (sync) from the current (source) guild into a target guild,
    preserving target overwrites and only creating/updating what's needed.
    Non-destructive. Fail-fast with auto-rollback.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending: Dict[str, Plan] = {}
        self.rate_delay = 0.20
        self.progress_min_secs = 5.0
        # rollback storage: last changelog per target guild id
        self.last_applied: Dict[int, List[ChangeLogEntry]] = {}
        # UI additions
        self.recent_ops: Dict[str, List[str]] = {}
        self.skipped: Dict[str, int] = {}
        # behavior switches
        self.fail_fast = True  # any op failure => rollback

    # ---------- UI helpers ----------
    def _push_recent(self, p: Plan, text: str):
        q = self.recent_ops.setdefault(p.key, [])
        q.append(text)
        if len(q) > 5:
            q.pop(0)

    def _panel_embed(self, p: Plan, step: str = "Ready", note: Optional[str] = None) -> discord.Embed:
        rc = sum(1 for a in p.role_actions if a.kind == "create")
        ru = sum(1 for a in p.role_actions if a.kind == "update")
        cc = sum(1 for c in p.channel_actions if c.op == "create")
        cu = sum(1 for c in p.channel_actions if c.op == "update")

        emb = discord.Embed(
            title="✨ Revamp Mirror Panel",
            color=discord.Color.blurple(),
            description=(
                f"**Source:** `{discord.utils.escape_markdown(p.source.name)}`\n"
                f"**Target:** `{discord.utils.escape_markdown(p.target.name) if p.target else '—'}`"
            ),
        )
        if getattr(p.source, "icon", None):
            try: emb.set_thumbnail(url=p.source.icon.url)
            except Exception: pass
        if p.target and getattr(p.target, "icon", None):
            try: emb.set_image(url=p.target.icon.url)
            except Exception: pass

        emb.add_field(name="Mode", value="`SYNC` (non-destructive)", inline=True)
        emb.add_field(name="Overwrites", value="`Preserve`", inline=True)
        emb.add_field(name="Lockdown", value=("`Yes`" if p.lockdown else "`No`"), inline=True)
        emb.add_field(name="Threads", value=("`Yes`" if p.sync_threads else "`No`"), inline=True)

        emb.add_field(name="👤 Roles", value=f"Create **{rc}** • Update **{ru}**", inline=False)
        emb.add_field(name="📺 Channels", value=f"Create **{cc}** • Update **{cu}**", inline=False)

        bullets = [
            "Mirror **roles** and **categories/channels** from Source.",
            "**Preserve** target channel permissions (no overwrite edits).",
            "No deletes; target-only items remain.",
        ]
        if CATEGORY_SCOPE:
            bullets.append(f"Scope: only {len(CATEGORY_SCOPE)} category(ies) from Source")
        emb.add_field(name="Plan Summary", value="• " + "\n• ".join(bullets), inline=False)

        emb.add_field(name="Status", value=f"{_icon(step.lower())} **{step}**", inline=False)
        if note:
            emb.add_field(name="Hint", value=note, inline=False)

        if p.warnings:
            preview = "\n".join(f"• {w}" for w in p.warnings[:5])
            if len(p.warnings) > 5: preview += f"\n… +{len(p.warnings)-5} more"
            emb.add_field(name="Notes", value=preview, inline=False)

        footer = f"ADMIN is never moved • Requested by {p.requested_by}"
        try:
            emb.set_footer(text=footer, icon_url=(p.requested_by.display_avatar.url if hasattr(p.requested_by,'display_avatar') else discord.Embed.Empty))
        except Exception:
            emb.set_footer(text=footer)
        emb.timestamp = discord.utils.utcnow()
        return emb

    def _progress_embed(self, p: Plan, results: dict, step: str) -> discord.Embed:
        emb = self._panel_embed(p, step)
        done = sum(results.values())
        target = max(done, len(p.role_actions) + len(p.channel_actions))
        slots = 16
        filled = int((done / target) * slots) if target else 0
        bar = "█" * filled + "░" * (slots - filled)
        emb.add_field(
            name="Progress",
            value=(
                f"`{bar}`  {done}/{target}\n"
                f"Roles — C:{results['role_create']} U:{results['role_update']}\n"
                f"Chans — C:{results['chan_create']} U:{results['chan_update']}"
            ),
            inline=False,
        )
        recent = "\n".join(f"• {t}" for t in self.recent_ops.get(p.key, [])) or "—"
        emb.add_field(name="Recent", value=recent, inline=False)
        skipped = self.skipped.get(p.key, 0)
        if skipped:
            emb.add_field(name="Skipped", value=f"{skipped} (insufficient role position/permissions)", inline=True)
        return emb

    # ---------- public command (source = ctx.guild) ----------
    @commands.hybrid_command(name="revamp", with_app_command=True)  # call as !revamp
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def revamp_open(self, ctx: commands.Context):
        source = ctx.guild
        mutual_targets = [g for g in self.bot.guilds if g.id != source.id]

        if not source.me or not source.me.guild_permissions.administrator:
            return await ctx.send("I need **Administrator** in this (source) server.")

        await ctx.typing()

        dummy_target = mutual_targets[0] if mutual_targets else source
        plan = Plan(
            key=f"{source.id}:{int(time.time())}",
            source=source,
            target=dummy_target,
            requested_by=ctx.author,
            include_deletes=False,
            lockdown=False,
            sync_threads=False,
            mode="sync",
            mirror_overwrites=False,
            auto_rollback=True,
        )
        self.pending[plan.key] = plan

        view = ControlView(self, plan_key=plan.key)
        emb = self._panel_embed(plan, step="Ready", note="Pick a target server, then **Apply**.")
        msg = await ctx.send(embed=emb, view=view)
        plan.control_msg = msg

    # ---------- core helpers ----------
    async def _is_empty_guild(self, g: discord.Guild) -> bool:
        roles = [r for r in await g.fetch_roles() if not r.managed and r.id != g.default_role.id]
        chans = await g.fetch_channels()
        cats = [c for c in chans if isinstance(c, discord.CategoryChannel)]
        noncats = [c for c in chans if not isinstance(c, discord.CategoryChannel)]
        return (len(roles) == 0) and (len(cats) == 0) and (len(noncats) == 0)

    def _in_scope(self, ch) -> bool:
        if not CATEGORY_SCOPE:
            return True
        cat = ch.category.name if getattr(ch, "category", None) else None
        return cat in CATEGORY_SCOPE

    async def _build_plan(self, source: discord.Guild, target: discord.Guild,
                          requested_by: discord.abc.User, lockdown: bool,
                          sync_threads: bool) -> Plan:
        key = f"{source.id}:{target.id}:{int(time.time())}"
        plan = Plan(key, source, target, requested_by, include_deletes=False,
                    lockdown=lockdown, sync_threads=sync_threads,
                    mode="sync", mirror_overwrites=False, auto_rollback=True)

        # roles
        src_roles = {r.id: r for r in (await source.fetch_roles())}
        tgt_roles = {r.id: r for r in (await target.fetch_roles())}
        tgt_by_name = {_norm(r.name): r for r in tgt_roles.values() if not r.managed}

        for r in sorted(src_roles.values(), key=lambda x: x.position):
            if r.id == source.default_role.id or r.managed:
                continue
            if CATEGORY_SCOPE:
                # roles are global; if scoping, still sync roles because channels may rely on them
                pass
            t = tgt_by_name.get(_norm(r.name))
            if not t:
                plan.role_actions.append(RoleAction("create", r.name, data=_strip_role(r)))
            else:
                diff, ch = _role_diff(_strip_role(t), _strip_role(r))
                if diff:
                    plan.role_actions.append(RoleAction("update", r.name, id=t.id, changes=ch))
                plan.role_id_map[r.id] = t.id

        # channels
        src_all = await source.fetch_channels(); tgt_all = await target.fetch_channels()
        src_cats = [c for c in src_all if isinstance(c, discord.CategoryChannel)]
        if CATEGORY_SCOPE:
            src_cats = [c for c in src_cats if c.name in CATEGORY_SCOPE]
        tgt_cats = [c for c in tgt_all if isinstance(c, discord.CategoryChannel)]
        tgt_cat_by = {c.name: c for c in tgt_cats}

        for c in sorted(src_cats, key=lambda c: c.position):
            ex = tgt_cat_by.get(c.name)
            if not ex:
                plan.channel_actions.append(ChannelAction("category", "create", name=c.name))
            else:
                plan.cat_id_map[c.id] = ex.id
                if ex.position != c.position:
                    plan.channel_actions.append(ChannelAction("category", "update", id=ex.id, name=c.name, what="position"))

        def key_of(ch):
            parent = ch.category.name if ch.category else "__ROOT__"
            return f"{parent}|{ch.type}|{ch.name}"

        tgt_non = [c for c in tgt_all if not isinstance(c, discord.CategoryChannel)]
        tgt_by_key = {key_of(c): c for c in tgt_non}
        src_non = [c for c in src_all if not isinstance(c, discord.CategoryChannel)]
        # scope filter for non-category channels
        src_non = [c for c in src_non if self._in_scope(c)]

        for s in sorted(src_non, key=lambda c: c.position):
            k = key_of(s)
            ex = tgt_by_key.get(k)
            if not ex:
                plan.channel_actions.append(ChannelAction("channel", "create", name=s.name, type=s.type,
                                                          parent_name=(s.category.name if s.category else None)))
            else:
                plan.chan_id_map[s.id] = ex.id
                if _chan_changed(ex, s):
                    plan.channel_actions.append(ChannelAction("channel", "update", id=ex.id, name=s.name, type=s.type,
                                                              parent_name=(s.category.name if s.category else None),
                                                              what="config/position"))
        return plan

    # ---------- ADMIN anchor (never moved) ----------
    def _bot_top_position(self, guild: discord.Guild) -> int:
        me = guild.me
        return max((r.position for r in (me.roles if me else [])), default=0)

    async def _ensure_admin_anchor(self, plan: Plan) -> discord.Role:
        guild = plan.target
        me = guild.me
        if not me:
            raise RuntimeError("Bot member not found in target guild.")
        candidates = [r for r in guild.roles if not r.managed and _norm(r.name) == _norm(ADMIN_ANCHOR_NAME)]
        if candidates:
            admin_role = max(candidates, key=lambda r: r.position)
        else:
            admin_role = await guild.create_role(
                name=ADMIN_ANCHOR_NAME,
                permissions=discord.Permissions.all(),
                colour=discord.Colour.from_rgb(230, 76, 60),
                hoist=False,
                mentionable=False,
                reason="Revamp sync: create ADMIN anchor role",
            )
            await asyncio.sleep(self.rate_delay)
        if admin_role not in me.roles:
            try:
                await me.add_roles(admin_role, reason="Revamp sync: grant ADMIN anchor to bot")
                await asyncio.sleep(self.rate_delay)
            except discord.Forbidden:
                raise RuntimeError("Missing permissions to assign ADMIN anchor to the bot.")
        top_pos = len(guild.roles) - 1
        if admin_role.position != top_pos:
            plan.warnings.append(
                f"ADMIN not at very top (pos {admin_role.position} < {top_pos}). This cog will NOT move it."
            )
        return admin_role

    # ---------- apply (fail-fast + auto-rollback) ----------
    async def apply_plan(self, p: Plan) -> discord.Embed:
        s, t = p.source, p.target
        results = {"role_create": 0, "role_update": 0, "chan_create": 0, "chan_update": 0}
        changelog: List[ChangeLogEntry] = []
        self.last_applied[t.id] = changelog  # expose live changelog for rollback
        self.recent_ops[p.key] = []
        self.skipped[p.key] = 0

        last_edit = time.monotonic()
        async def progress(step_text: str):
            now = time.monotonic()
            if (now - last_edit) >= self.progress_min_secs:
                if p.control_msg:
                    try: await p.control_msg.edit(embed=self._progress_embed(p, results, step_text))
                    except Exception: pass
                nonlocal last_edit
                last_edit = now

        async def do(coro, note: str):
            retries, delay = 3, 0.8
            while True:
                try:
                    out = await coro
                    await asyncio.sleep(self.rate_delay)
                    return out
                except discord.Forbidden as e:
                    p.warnings.append(f"{note}: Forbidden ({e})");
                    raise
                except discord.HTTPException as e:
                    if retries <= 0:
                        p.warnings.append(f"{note}: {e}")
                        raise
                    await asyncio.sleep(delay); retries -= 1; delay = min(delay * 2, 4.0)
                except Exception as e:
                    p.warnings.append(f"{note}: {e}")
                    raise

        try:
            # start
            if p.control_msg:
                try: await p.control_msg.edit(embed=self._panel_embed(p, "Starting"))
                except Exception: pass

            # optional lockdown
            if p.lockdown:
                snapshot = await self._snapshot_lockdown(t)
                changelog.append(ChangeLogEntry("lockdown", "set", {"snapshot": snapshot}))
                await self._toggle_lock(t, enable=True, plan=p)
                await progress("Locked")

            # ADMIN role (never moved)
            await self._ensure_admin_anchor(p)
            bot_top = self._bot_top_position(t)

            # ---- roles ----
            for a in p.role_actions:
                if a.kind == "create" and a.data:
                    created = await do(t.create_role(
                        name=a.data["name"], colour=discord.Colour(a.data["color"]), hoist=a.data["hoist"],
                        mentionable=a.data["mentionable"], permissions=discord.Permissions(a.data["permissions"]),
                        reason="Revamp sync: create role"
                    ), f"Create role {a.data.get('name')}")
                    if created:
                        changelog.append(ChangeLogEntry("role", "create", {"role_id": created.id}))
                        try:
                            if created.position != 1:
                                await created.edit(position=1, reason="Revamp sync: seed under ADMIN")
                                await asyncio.sleep(self.rate_delay)
                        except Exception:
                            pass
                        results["role_create"] += 1
                        self._push_recent(p, f"Role + {created.name}")
                        await progress("Roles")

                elif a.kind == "update" and a.id and a.changes:
                    tgt = t.get_role(a.id)
                    if not tgt:
                        continue
                    if tgt.position >= bot_top:
                        p.warnings.append(f"Skip update for {tgt.name!r}: at/above bot's highest role (pos {tgt.position} ≥ {bot_top})")
                        self.skipped[p.key] = self.skipped.get(p.key, 0) + 1
                        continue
                    pre = {
                        "id": tgt.id, "color": tgt.colour.value, "hoist": tgt.hoist,
                        "mentionable": tgt.mentionable, "permissions": tgt.permissions.value
                    }
                    kwargs = {}
                    if "color" in a.changes:
                        newcol = a.changes["color"]["to"]
                        if tgt.colour.value != newcol:
                            kwargs["colour"] = discord.Colour(newcol)
                    if "hoist" in a.changes and tgt.hoist != a.changes["hoist"]["to"]:
                        kwargs["hoist"] = a.changes["hoist"]["to"]
                    if "mentionable" in a.changes and tgt.mentionable != a.changes["mentionable"]["to"]:
                        kwargs["mentionable"] = a.changes["mentionable"]["to"]
                    if "permissions" in a.changes and tgt.permissions.value != a.changes["permissions"]["to"]:
                        kwargs["permissions"] = discord.Permissions(a.changes["permissions"]["to"])
                    if kwargs:
                        await do(tgt.edit(reason="Revamp sync: update role", **kwargs), f"Update role {tgt.name}")
                        changelog.append(ChangeLogEntry("role", "update", {"pre": pre}))
                        results["role_update"] += 1
                        self._push_recent(p, f"Role ~ {tgt.name}")
                        await progress("Roles")

            # refresh mapping
            fresh = await t.fetch_roles()
            by_name = {_norm(r.name): r for r in fresh if not r.managed}
            for sr in (await s.fetch_roles()):
                if sr.id == s.default_role.id or sr.managed:
                    continue
                tr = by_name.get(_norm(sr.name))
                if tr:
                    p.role_id_map[sr.id] = tr.id

            # reorder roles under ADMIN (snapshot positions for rollback)
            try:
                pos_snapshot = {r.id: r.position for r in t.roles}
                await self._reorder_roles_like_source(p)
                changelog.append(ChangeLogEntry("roles_reorder", "reorder", {"positions": pos_snapshot}))
                await progress("Roles")
            except Exception as e:
                p.warnings.append(f"Role reordering issue: {e}")

            # ensure categories exist/order
            src_cats = [c for c in await s.fetch_channels() if isinstance(c, discord.CategoryChannel)]
            if CATEGORY_SCOPE:
                src_cats = [c for c in src_cats if c.name in CATEGORY_SCOPE]
            tgt_cats = [c for c in await t.fetch_channels() if isinstance(c, discord.CategoryChannel)]
            by_name_cat = {c.name: c for c in tgt_cats}
            for cat in sorted(src_cats, key=lambda c: c.position):
                ex = by_name_cat.get(cat.name)
                if not ex:
                    created = await do(t.create_category(name=cat.name, reason="Revamp sync: create category"), f"Create category {cat.name}")
                    if created:
                        changelog.append(ChangeLogEntry("channel", "create", {"id": created.id}))
                        p.cat_id_map[cat.id] = created.id
                        results["chan_create"] += 1
                        self._push_recent(p, f"Cat + {cat.name}")
                        await progress("Categories")
                else:
                    p.cat_id_map[cat.id] = ex.id
                    if ex.position != cat.position:
                        prepos = ex.position
                        await do(ex.edit(position=cat.position, reason="Revamp sync: reorder category"), f"Reorder category {ex.name}")
                        changelog.append(ChangeLogEntry("channel", "update", {"id": ex.id, "pre": {"position": prepos}}))
                        self._push_recent(p, f"Cat ↕ {ex.name}")
                        await progress("Categories")

            # create/update non-category channels + diffs (preserve overwrites)
            src_non = [c for c in await s.fetch_channels() if not isinstance(c, discord.CategoryChannel)]
            src_non = [c for c in src_non if self._in_scope(c)]
            tgt_non = [c for c in await t.fetch_channels() if not isinstance(c, discord.CategoryChannel)]

            def find_match(src):
                for c in tgt_non:
                    if c.name == src.name and c.type == src.type and ((c.category.name if c.category else None) == (src.category.name if src.category else None)):
                        return c

            created_map = {}
            for src_ch in sorted(src_non, key=lambda c: c.position):
                ex = find_match(src_ch)
                parent_id = p.cat_id_map.get(getattr(src_ch, "category_id", None)) if getattr(src_ch, "category_id", None) else None
                parent_obj = t.get_channel(parent_id) if parent_id else None

                if not ex:
                    ch = None
                    if src_ch.type is discord.ChannelType.text:
                        ch = await do(t.create_text_channel(
                            name=src_ch.name, category=parent_obj,
                            topic=getattr(src_ch, "topic", None), nsfw=getattr(src_ch, "nsfw", False),
                            slowmode_delay=getattr(src_ch, "slowmode_delay", 0) or getattr(src_ch, "rate_limit_per_user", 0),
                            reason="Revamp sync: create channel"
                        ), f"Create channel #{src_ch.name}")
                    elif src_ch.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                        ch = await do(t.create_voice_channel(
                            name=src_ch.name, category=parent_obj,
                            bitrate=getattr(src_ch, "bitrate", None), user_limit=getattr(src_ch, "user_limit", None),
                            reason="Revamp sync: create channel"
                        ), f"Create channel #{src_ch.name}")
                    elif src_ch.type is discord.ChannelType.forum:
                        ch = await do(t.create_forum(
                            name=src_ch.name, category=parent_obj,
                            reason="Revamp sync: create forum"
                        ), f"Create forum #{src_ch.name}")
                    else:
                        ch = await do(t.create_text_channel(name=src_ch.name, category=parent_obj,
                                                            reason="Revamp sync: create channel"), f"Create channel #{src_ch.name}")
                    if ch:
                        changelog.append(ChangeLogEntry("channel", "create", {"id": ch.id}))
                        created_map[src_ch.id] = ch
                        results["chan_create"] += 1
                        self._push_recent(p, f"Chan + #{src_ch.name}")
                        await progress("Channels")
                else:
                    pre = await self._snapshot_channel(ex)
                    kwargs = {"reason": "Revamp sync: update channel"}
                    if hasattr(ex, "category"):
                        if (ex.category.id if ex.category else None) != (parent_obj.id if parent_obj else None):
                            kwargs["category"] = parent_obj
                    if hasattr(ex, "topic"):
                        src_topic = getattr(src_ch, "topic", None)
                        if (ex.topic or None) != (src_topic or None):
                            kwargs["topic"] = src_topic
                    if hasattr(ex, "nsfw"):
                        src_nsfw = bool(getattr(src_ch, "nsfw", False))
                        if bool(getattr(ex, "nsfw", False)) != src_nsfw:
                            kwargs["nsfw"] = src_nsfw
                    if hasattr(ex, "slowmode_delay"):
                        src_sd = getattr(src_ch, "slowmode_delay", 0) or getattr(src_ch, "rate_limit_per_user", 0)
                        if int(getattr(ex, "slowmode_delay", 0) or getattr(ex, "rate_limit_per_user", 0)) != int(src_sd or 0):
                            kwargs["slowmode_delay"] = src_sd

                    edit_payload = {k: v for k, v in kwargs.items() if k != "reason"}
                    if any(k for k in edit_payload):
                        await do(ex.edit(**edit_payload, reason=kwargs.get("reason")), f"Update channel #{ex.name}")
                        changelog.append(ChangeLogEntry("channel", "update", {"id": ex.id, "pre": pre}))
                        results["chan_update"] += 1
                        self._push_recent(p, f"Chan ~ #{ex.name}")
                        await progress("Channels")

                    if getattr(ex, "position", None) is not None and ex.position != src_ch.position:
                        prepos = ex.position
                        await do(ex.edit(position=src_ch.position, reason="Revamp sync: reorder channel"), f"Reorder channel #{ex.name}")
                        changelog.append(ChangeLogEntry("channel", "update", {"id": ex.id, "pre": {"position": prepos}}))
                        self._push_recent(p, f"Chan ↕ #{ex.name}")
                        await progress("Channels")

                    p.chan_id_map[src_ch.id] = ex.id
                    created_map[src_ch.id] = ex

            # threads (optional, default off)
            if p.sync_threads:
                await self._sync_threads(p, src_non, created_map)
                await progress("Threads")

            # unlock
            if p.lockdown:
                await self._toggle_lock(t, enable=False, plan=p)
                await progress("Unlocked")

            # done
            final = discord.Embed(title="✅ Revamp — Completed", color=discord.Color.green())
            final.add_field(name="Mode", value=p.mode.upper(), inline=True)
            final.add_field(name="Overwrites", value="Preserve", inline=True)
            final.add_field(name="👤 Roles", value=f"C **{results['role_create']}** • U **{results['role_update']}**", inline=False)
            final.add_field(name="📺 Channels", value=f"C **{results['chan_create']}** • U **{results['chan_update']}**", inline=False)
            if p.warnings:
                preview = "\n".join(f"• {w}" for w in p.warnings[:8])
                if len(p.warnings) > 8: preview += f"\n… +{len(p.warnings)-8} more"
                final.add_field(name="Notes", value=preview, inline=False)
            final.timestamp = discord.utils.utcnow()
            return final

        except Exception as e:
            p.warnings.append(f"Fatal error during apply: {e}")
            rollback_embed = None
            try:
                if p.auto_rollback:
                    rollback_embed = await self._rollback(t)
            except Exception as re:
                p.warnings.append(f"Auto-rollback also failed: {re}")
            err = discord.Embed(title="❌ Revamp — Failed", color=discord.Color.red())
            err.add_field(name="Error", value=f"```{e}```", inline=False)
            err.add_field(name="Auto-Rollback", value=("Completed." if rollback_embed else "Attempted but failed — see Notes."), inline=False)
            if p.warnings:
                preview = "\n".join(f"• {w}" for w in p.warnings[:8])
                if len(p.warnings) > 8: preview += f"\n… +{len(p.warnings)-8} more"
                err.add_field(name="Notes", value=preview, inline=False)
            err.timestamp = discord.utils.utcnow()
            return err

    # ---------- rollback ----------
    async def _rollback(self, target: discord.Guild) -> discord.Embed:
        log = self.last_applied.get(target.id)
        if not log:
            return discord.Embed(title=f"{_icon('rollback')} Rollback", description="Nothing to rollback.", color=discord.Color.orange())

        reversed_log = list(reversed(log))
        restored = {"role": 0, "channel": 0, "overwrites": 0, "reorder": 0, "lockdown": 0}
        for entry in reversed_log:
            try:
                if entry.kind == "role":
                    if entry.op == "create":
                        r = target.get_role(entry.payload["role_id"])
                        if r:
                            await r.delete(reason="Revamp rollback: remove created role"); restored["role"] += 1
                    elif entry.op == "update":
                        pre = entry.payload["pre"]; r = target.get_role(pre["id"])
                        if r:
                            await r.edit(
                                colour=discord.Colour(pre["color"]),
                                hoist=pre["hoist"],
                                mentionable=pre["mentionable"],
                                permissions=discord.Permissions(pre["permissions"]),
                                reason="Revamp rollback: role update revert"
                            ); restored["role"] += 1

                elif entry.kind == "channel":
                    if entry.op == "create":
                        ch = target.get_channel(entry.payload["id"])
                        if ch:
                            await ch.delete(reason="Revamp rollback: remove created channel"); restored["channel"] += 1
                    elif entry.op == "update":
                        pre = entry.payload["pre"]; ch = target.get_channel(entry.payload["id"])
                        if ch:
                            await self._restore_channel_partial(ch, pre); restored["channel"] += 1

                elif entry.kind == "overwrites" and entry.op == "set":
                    # not used in sync mode (we preserve), but keep for safety
                    ch = target.get_channel(entry.payload["channel_id"])
                    pre = entry.payload["pre"]
                    if ch:
                        ov = {}
                        for rid, (a_val, d_val) in pre.items():
                            role = target.get_role(int(rid))
                            if role:
                                ov[role] = discord.PermissionOverwrite.from_pair(
                                    discord.Permissions(a_val), discord.Permissions(d_val)
                                )
                        await ch.edit(overwrites=ov, reason="Revamp rollback: restore role overwrites")
                        restored["overwrites"] += 1

                elif entry.kind == "roles_reorder" and entry.op == "reorder":
                    pos = entry.payload["positions"]  # {role_id: position}
                    for rid, rpos in pos.items():
                        role = target.get_role(int(rid))
                        if role:
                            try: await role.edit(position=rpos, reason="Revamp rollback: restore role positions")
                            except Exception: pass
                    restored["reorder"] += 1

                elif entry.kind == "lockdown" and entry.op == "set":
                    snap = entry.payload["snapshot"]  # {channel_id: (allow,deny)}
                    everyone = target.default_role
                    for cid, pair in snap.items():
                        ch = target.get_channel(int(cid))
                        if not ch or not isinstance(ch, discord.TextChannel):
                            continue
                        a_val, d_val = pair
                        po = discord.PermissionOverwrite.from_pair(discord.Permissions(a_val), discord.Permissions(d_val))
                        try:
                            await ch.set_permissions(everyone, overwrite=po, reason="Revamp rollback: restore lockdown state")
                        except Exception:
                            pass
                    restored["lockdown"] += 1

                await asyncio.sleep(self.rate_delay)
            except Exception:
                continue

        self.last_applied.pop(target.id, None)

        emb = discord.Embed(title=f"{_icon('rollback')} Rollback — Completed", color=discord.Color.blurple())
        emb.add_field(name="Roles", value=f"{restored['role']} changes reverted", inline=True)
        emb.add_field(name="Channels", value=f"{restored['channel']} changes reverted", inline=True)
        emb.add_field(name="Overwrites", value=f"{restored['overwrites']} restored", inline=True)
        emb.add_field(name="Positions", value=f"{restored['reorder']} batch", inline=True)
        emb.add_field(name="Lockdown", value=f"{restored['lockdown']} restored", inline=True)
        emb.timestamp = discord.utils.utcnow()
        return emb

    # ---------- snapshots / restore helpers ----------
    async def _snapshot_lockdown(self, guild: discord.Guild) -> Dict[int, Tuple[int, int]]:
        out = {}
        everyone = guild.default_role
        for ch in [c for c in await guild.fetch_channels() if isinstance(c, discord.TextChannel)]:
            po = ch.overwrites_for(everyone) or discord.PermissionOverwrite()
            a, d = po.pair()
            out[ch.id] = (a.value, d.value)
        return out

    async def _snapshot_role_overwrites(self, ch: discord.abc.GuildChannel) -> Dict[int, Tuple[int, int]]:
        out = {}
        for obj, po in getattr(ch, "overwrites", {}).items():
            if isinstance(obj, discord.Role):
                a, d = po.pair()
                out[obj.id] = (a.value, d.value)
        return out

    async def _snapshot_channel(self, ch: discord.abc.GuildChannel) -> dict:
        type_value = getattr(getattr(ch, "type", None), "value", None)
        snap = {
            "type": type_value,
            "name": ch.name,
            "id": ch.id,
            "category": (ch.category.id if getattr(ch, "category", None) else None),
            "position": getattr(ch, "position", 0),
            "topic": getattr(ch, "topic", None),
            "nsfw": getattr(ch, "nsfw", False),
            "slowmode": getattr(ch, "slowmode_delay", 0) or getattr(ch, "rate_limit_per_user", 0),
            "bitrate": getattr(ch, "bitrate", None),
            "user_limit": getattr(ch, "user_limit", None),
        }
        snap["role_overwrites"] = await self._snapshot_role_overwrites(ch)
        return snap

    async def _restore_channel_partial(self, ch: discord.abc.GuildChannel, pre: dict):
        kwargs = {}
        if "category" in pre:
            cat = ch.guild.get_channel(pre["category"]) if pre["category"] else None
            kwargs["category"] = cat
        if "name" in pre and ch.name != pre["name"]:
            kwargs["name"] = pre["name"]
        if "topic" in pre and hasattr(ch, "topic") and (ch.topic or None) != (pre["topic"] or None):
            kwargs["topic"] = pre["topic"]
        if "nsfw" in pre and hasattr(ch, "nsfw") and bool(getattr(ch, "nsfw", False)) != bool(pre["nsfw"]):
            kwargs["nsfw"] = pre["nsfw"]
        if "slowmode" in pre and hasattr(ch, "slowmode_delay"):
            cur = getattr(ch, "slowmode_delay", 0) or getattr(ch, "rate_limit_per_user", 0)
            if int(cur or 0) != int(pre["slowmode"] or 0):
                kwargs["slowmode_delay"] = pre["slowmode"]
        if "bitrate" in pre and hasattr(ch, "bitrate") and getattr(ch, "bitrate", None) != pre["bitrate"]:
            kwargs["bitrate"] = pre["bitrate"]
        if "user_limit" in pre and hasattr(ch, "user_limit") and getattr(ch, "user_limit", None) != pre["user_limit"]:
            kwargs["user_limit"] = pre["user_limit"]
        if kwargs:
            await ch.edit(**kwargs, reason="Revamp rollback: channel revert")
        if "position" in pre and getattr(ch, "position", None) is not None and ch.position != pre["position"]:
            try:
                await ch.edit(position=pre["position"], reason="Revamp rollback: channel position")
            except Exception:
                pass
        if "role_overwrites" in pre:
            ov = {}
            for rid, (a_val, d_val) in pre["role_overwrites"].items():
                role = ch.guild.get_role(int(rid))
                if role:
                    ov[role] = discord.PermissionOverwrite.from_pair(
                        discord.Permissions(a_val), discord.Permissions(d_val)
                    )
            try:
                await ch.edit(overwrites=ov, reason="Revamp rollback: channel overwrites")
            except Exception:
                pass

    # ---------- threads helper (optional) ----------
    async def _gather_threads(self, parent) -> List[discord.Thread]:
        threads: List[discord.Thread] = []
        try:
            threads.extend(getattr(parent, "threads", []))
        except Exception:
            pass
        try:
            async for th in parent.archived_threads(limit=50, private=False):
                threads.append(th)
        except Exception:
            pass
        try:
            async for th in parent.archived_threads(limit=50, private=True):
                threads.append(th)
        except Exception:
            pass
        seen = set(); uniq = []
        for th in threads:
            if th.id in seen: continue
            seen.add(th.id); uniq.append(th)
        return uniq

    async def _sync_threads(self, p: Plan, src_non: List[discord.abc.GuildChannel], created_map: Dict[int, discord.abc.GuildChannel]):
        t = p.target
        for src_parent in src_non:
            if not isinstance(src_parent, (discord.TextChannel, discord.ForumChannel)):
                continue
            tgt_parent = created_map.get(src_parent.id)
            if not tgt_parent:
                parent_id = p.cat_id_map.get(getattr(src_parent, "category_id", None)) if getattr(src_parent, "category_id", None) else None
                for c in (await t.fetch_channels()):
                    if not isinstance(c, (discord.TextChannel, discord.ForumChannel)): continue
                    if c.name == src_parent.name and c.type == src_parent.type and ((c.category.id if c.category else None) == parent_id or parent_id is None):
                        tgt_parent = c; break
                else:
                    continue

            try: src_threads = await self._gather_threads(src_parent)
            except Exception: src_threads = []
            try: tgt_threads = await self._gather_threads(tgt_parent)
            except Exception: tgt_threads = []

            src_by_name = {_norm(th.name): th for th in src_threads if th and th.name}
            tgt_by_name = {_norm(th.name): th for th in tgt_threads if th and th.name}

            for key, th in src_by_name.items():
                if key in tgt_by_name:
                    continue
                try:
                    if isinstance(tgt_parent, discord.ForumChannel):
                        await tgt_parent.create_thread(name=th.name, content="Imported by Revamp", reason="Revamp sync: create forum thread")
                    elif isinstance(tgt_parent, discord.TextChannel):
                        await tgt_parent.create_thread(name=th.name, type=discord.ChannelType.public_thread, reason="Revamp sync: create thread")
                    await asyncio.sleep(self.rate_delay)
                except Exception as e:
                    p.warnings.append(f"Could not create thread '{th.name}' in {getattr(tgt_parent,'name','?')}: {e}")

    # ---------- lockdown ----------
    async def _toggle_lock(self, guild: discord.Guild, enable: bool, plan: Plan):
        everyone = guild.default_role
        for ch in [c for c in await guild.fetch_channels() if isinstance(c, discord.TextChannel)]:
            try:
                current = ch.overwrites_for(everyone)
                new = copy.copy(current)
                if enable:
                    new.send_messages = False; new.add_reactions = False
                await ch.set_permissions(everyone, overwrite=new, reason="Revamp sync: lockdown" if enable else "Revamp sync: unlock")
                await asyncio.sleep(self.rate_delay)
            except discord.Forbidden as e:
                plan.warnings.append(f"Lockdown change forbidden in #{ch.name}: {e}")
            except Exception as e:
                plan.warnings.append(f"Lockdown change failed in #{ch.name}: {e}")

    # ---------- role ordering ----------
    async def _reorder_roles_like_source(self, p: Plan) -> None:
        target, source = p.target, p.source
        admin = next((r for r in target.roles if not r.managed and _norm(r.name) == _norm(ADMIN_ANCHOR_NAME)), None)
        if not admin:
            raise RuntimeError(f"{ADMIN_ANCHOR_NAME} anchor not found on target.")
        admin_pos = admin.position

        src_sorted = [r for r in await source.fetch_roles()
                      if not r.managed and r.id != source.default_role.id]
        src_sorted.sort(key=lambda r: r.position)  # bottom→top

        desired: List[discord.Role] = []
        for sr in src_sorted:
            tr = target.get_role(p.role_id_map.get(sr.id, 0))
            if not tr: continue
            if tr.managed or tr.id == target.default_role.id or tr.id == admin.id:
                continue
            if tr.position >= admin_pos:
                continue
            desired.append(tr)

        pos = 1
        for role in desired:
            new_pos = min(admin_pos - 1, max(1, pos))
            if role.position != new_pos:
                try:
                    await role.edit(position=new_pos, reason="Revamp sync: reorder under ADMIN")
                    await asyncio.sleep(self.rate_delay)
                except discord.Forbidden:
                    p.warnings.append(f"Forbidden moving role {role.name}")
                    self.skipped[p.key] = self.skipped.get(p.key, 0) + 1
                except discord.HTTPException as e:
                    p.warnings.append(f"Failed moving role {role.name}: {e}")
            pos += 1


# ---------- UI view & components ----------
class ControlView(ui.View):
    def __init__(self, cog: "RevampSync", plan_key: str, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.plan_key = plan_key
        # row 0: target selector
        self.add_item(TargetSelect(cog, plan_key))
        # row 2: optional toggles (lockdown/threads)
        self.add_item(ToggleLockdown(cog, plan_key))
        self.add_item(ToggleThreads(cog, plan_key))

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not inter.user.guild_permissions.administrator:
            await _ireply(inter, "Admin only.", ephemeral=True)
            return False
        return True

    @ui.button(label="Apply", style=discord.ButtonStyle.danger, row=3)
    async def apply(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.get(self.plan_key)
        if not plan:
            return await _ireply(inter, "This request expired.", ephemeral=True)

        t = plan.target
        if not (t and t.me and t.me.guild_permissions.administrator):
            return await _ireply(inter, "I need **Administrator** in the target server.", ephemeral=True)

        full = await self.cog._build_plan(plan.source, plan.target, inter.user, plan.lockdown, plan.sync_threads)
        full.auto_rollback = True
        full.control_msg = plan.control_msg
        self.cog.pending[self.plan_key] = full

        confirm = ConfirmApplyView(self.cog, self.plan_key)
        try:
            await plan.control_msg.edit(embed=self.cog._panel_embed(full, step="Review Plan"), view=confirm)
        except Exception:
            pass
        await _ireply(inter, "Plan prepared. Review and confirm.", ephemeral=True)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.pop(self.plan_key, None)
        await _ireply(inter, "Cancelled. No changes made.", ephemeral=True)
        if plan and plan.control_msg:
            try:
                cancelled = discord.Embed(title="❎ Revamp — Cancelled", color=discord.Color.red())
                await plan.control_msg.edit(embed=cancelled, view=None)
            except Exception:
                pass

    @ui.button(label="Rollback last", style=discord.ButtonStyle.primary, row=3)
    async def rollback(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.get(self.plan_key)
        if not plan:
            return await _ireply(inter, "This request expired.", ephemeral=True)
        target_id = plan.target.id if plan.target else 0
        if target_id not in self.cog.last_applied:
            return await _ireply(inter, "No previous apply found for rollback on the selected target.", ephemeral=True)
        try:
            if not inter.response.is_done():
                await inter.response.defer(thinking=True, ephemeral=True)
        except Exception:
            pass
        emb = await self.cog._rollback(plan.target)
        try:
            if plan.control_msg:
                await plan.control_msg.edit(embed=emb, view=None)
        except Exception:
            pass
        await _ireply(inter, embed=emb, ephemeral=True)


class TargetSelect(ui.Select):
    def __init__(self, cog: "RevampSync", plan_key: str):
        self.cog = cog; self.plan_key = plan_key
        options = []
        plan = self.cog.pending.get(plan_key)
        src = plan.source if plan else None
        for g in cog.bot.guilds:
            if not src or g.id == src.id:
                continue
            label = g.name[:100]
            options.append(discord.SelectOption(label=label, value=str(g.id)))
        if not options:
            options = [discord.SelectOption(label="No other servers available", value="0", default=True)]
        super().__init__(placeholder="Select target server…", options=options, min_values=1, max_values=1, row=0)

    async def callback(self, inter: discord.Interaction):
        plan = self.cog.pending.get(self.plan_key)
        if not plan: return await _ireply(inter, "Expired.", ephemeral=True)
        gid = int(self.values[0])
        tgt = next((g for g in self.cog.bot.guilds if g.id == gid), None)
        if not tgt:
            return await _ireply(inter, "Target not found.", ephemeral=True)
        plan.target = tgt
        try:
            await plan.control_msg.edit(embed=self.cog._panel_embed(plan, step="Target selected"))
        except Exception:
            pass
        await _ireply(inter, f"Target set to **{tgt.name}**.", ephemeral=True)


class ToggleLockdown(ui.Button):
    def __init__(self, cog: "RevampSync", plan_key: str):
        self.cog = cog; self.plan_key = plan_key
        super().__init__(style=discord.ButtonStyle.secondary, label="Lockdown: Off", row=2)

    async def callback(self, inter: discord.Interaction):
        plan = self.cog.pending.get(self.plan_key)
        if not plan: return await _ireply(inter, "Expired.", ephemeral=True)
        plan.lockdown = not plan.lockdown
        self.label = f"Lockdown: {'On' if plan.lockdown else 'Off'}"
        try:
            await plan.control_msg.edit(embed=self.cog._panel_embed(plan, step="Toggled Lockdown"))
        except Exception:
            pass
        await _ireply(inter, f"Lockdown set to **{'On' if plan.lockdown else 'Off'}**.", ephemeral=True)


class ToggleThreads(ui.Button):
    def __init__(self, cog: "RevampSync", plan_key: str):
        self.cog = cog; self.plan_key = plan_key
        super().__init__(style=discord.ButtonStyle.secondary, label="Threads: Off", row=2)

    async def callback(self, inter: discord.Interaction):
        plan = self.cog.pending.get(self.plan_key)
        if not plan: return await _ireply(inter, "Expired.", ephemeral=True)
        plan.sync_threads = not plan.sync_threads
        self.label = f"Threads: {'On' if plan.sync_threads else 'Off'}"
        try:
            await plan.control_msg.edit(embed=self.cog._panel_embed(plan, step="Toggled Threads"))
        except Exception:
            pass
        await _ireply(inter, f"Threads set to **{'On' if plan.sync_threads else 'Off'}**.", ephemeral=True)


class ConfirmApplyView(ui.View):
    def __init__(self, cog: "RevampSync", plan_key: str, timeout: float = 900):
        super().__init__(timeout=timeout); self.cog = cog; self.plan_key = plan_key

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not inter.user.guild_permissions.administrator:
            await _ireply(inter, "Admin only.", ephemeral=True)
            return False
        return True

    @ui.button(label="Confirm Mirror", style=discord.ButtonStyle.danger, row=3)
    async def confirm(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.get(self.plan_key)
        if not plan: return await _ireply(inter, "Expired.", ephemeral=True)

        # turn green + disable entire view to prevent double-submit
        try:
            btn.label = "Confirmed"
            btn.style = discord.ButtonStyle.success
            btn.disabled = True
            for child in self.children:
                if child is not btn:
                    child.disabled = True
            if plan.control_msg:
                await plan.control_msg.edit(view=self)
        except Exception:
            pass

        try:
            if not inter.response.is_done():
                await inter.response.defer(thinking=True)
        except Exception:
            pass
        try:
            result = await self.cog.apply_plan(plan)
            self.cog.pending.pop(self.plan_key, None)
            try:
                if plan.control_msg: await plan.control_msg.edit(embed=result, view=None)
            except Exception:
                pass
            await _ireply(inter, embed=result)
            self.stop()
        except Exception as e:
            await _ireply(inter, f"❌ Apply failed: {e}")
            self.stop()

    @ui.button(label="Rollback last", style=discord.ButtonStyle.primary, row=3)
    async def rollback(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.get(self.plan_key)
        if not plan: return await _ireply(inter, "Expired.", ephemeral=True)
        try:
            if not inter.response.is_done():
                await inter.response.defer(thinking=True, ephemeral=True)
        except Exception:
            pass
        emb = await self.cog._rollback(plan.target)
        await _ireply(inter, embed=emb, ephemeral=True)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.pop(self.plan_key, None)
        await _ireply(inter, "Cancelled. No changes made.", ephemeral=True)
        if plan and plan.control_msg:
            try:
                cancelled = discord.Embed(title="❎ Revamp — Cancelled", color=discord.Color.red())
                await plan.control_msg.edit(embed=cancelled, view=None)
            except Exception:
                pass
        self.stop()
