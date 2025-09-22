# revamp_sync.py
# Red-DiscordBot Cog — Revamp -> Main Sync (ADMIN position is never changed)
# - Never moves the ADMIN role; only verifies and logs if it's not top-most
# - Role edits/deletes respect the bot's movable range; forbidden moves skipped with log notes
# - Reordering only under ADMIN
# - Safer channel/thread ops; clearer Forbidden handling
# - Forum creation: uses Guild.create_forum (compat with discord.py 2.x)

from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord import ui
from redbot.core import commands

__red_end_user_data_statement__ = "This cog stores no personal user data."

# ---------- config ----------
ADMIN_ANCHOR_NAME = "ADMIN"  # anchor role the bot uses as a ceiling for synced roles

# ---------- helpers ----------
def _norm(s: str) -> str: return (s or "").strip().lower()

def _icon(step: str) -> str:
    return {"start":"🟡","lock":"🔒","roles_create":"👤","roles_update":"👤","roles_delete":"🧹",
            "roles_order":"📚","chan_delete":"🧹","cat_create":"🗂️","chan_create":"📺",
            "chan_update":"🛠️","overwrites":"🔐","threads":"🧵","unlock":"🔓","done":"✅"}.get(step,"🔧")

async def _find_post_channel(g: discord.Guild) -> Optional[discord.TextChannel]:
    ch=g.system_channel
    if ch and ch.permissions_for(g.me).send_messages: return ch
    for c in g.text_channels:
        try:
            if c.permissions_for(g.me).send_messages: return c
        except: pass
    return None

def _strip_role(r: discord.Role) -> dict:
    return {"name": r.name, "key": _norm(r.name), "color": r.color.value,
            "hoist": r.hoist, "mentionable": r.mentionable, "permissions": r.permissions.value}

def _role_diff(tgt_like: dict, src_like: dict) -> Tuple[bool, dict]:
    ch, diff = {}, False
    for k in ("color","hoist","mentionable","permissions"):
        if tgt_like.get(k)!=src_like.get(k): ch[k]={"from":tgt_like.get(k),"to":src_like.get(k)}; diff=True
    return diff, ch

def _chan_changed(tgt, src) -> bool:
    try:
        if tgt.type!=src.type: return True
        t_topic = getattr(tgt,"topic",None); s_topic=getattr(src,"topic",None)
        if (t_topic or "")!=(s_topic or ""): return True
        if bool(getattr(tgt,"nsfw",False))!=bool(getattr(src,"nsfw",False)): return True
        trl=getattr(tgt,"slowmode_delay",0) or getattr(tgt,"rate_limit_per_user",0)
        srl=getattr(src,"slowmode_delay",0) or getattr(src,"rate_limit_per_user",0)
        if int(trl or 0)!=int(srl or 0): return True
        t_parent = tgt.category.name if tgt.category else None
        s_parent = src.category.name if src.category else None
        if t_parent!=s_parent: return True
        for a in ("bitrate","user_limit"):
            if getattr(tgt,a,None)!=getattr(src,a,None): return True
        if isinstance(tgt, discord.ForumChannel) and isinstance(src, discord.ForumChannel):
            if tgt.default_thread_slowmode_delay != src.default_thread_slowmode_delay: return True
            if getattr(tgt, "default_sort_order", None) != getattr(src, "default_sort_order", None): return True
            if getattr(tgt, "default_layout", None) != getattr(src, "default_layout", None): return True
            if (getattr(getattr(tgt,"default_reaction_emoji",None),"name",None) !=
                getattr(getattr(src,"default_reaction_emoji",None),"name",None)): return True
            t_tags = { _norm(t.name) for t in getattr(tgt, "available_tags", []) }
            s_tags = { _norm(t.name) for t in getattr(src, "available_tags", []) }
            if t_tags != s_tags: return True
    except: return True
    return False

# --- safe interaction replies (prevents 10008 Unknown Message) ---
async def _ireply(inter: discord.Interaction, content: Optional[str] = None,
                  *, embed: Optional[discord.Embed] = None, ephemeral: bool = False):
    try:
        if not inter.response.is_done():
            await inter.response.send_message(content or None, embed=embed, ephemeral=ephemeral)
        else:
            await inter.followup.send(content or None, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        try:
            await inter.channel.send(content or None, embed=embed)
        except Exception:
            pass
    except Exception:
        pass

# ---------- plan data ----------
@dataclass
class RoleAction:
    kind: str  # create|update|delete
    name: str
    id: Optional[int]=None
    data: Optional[dict]=None
    changes: Optional[dict]=None

@dataclass
class ChannelAction:
    kind: str  # category|channel
    op: str    # create|update|delete
    id: Optional[int]=None
    name: Optional[str]=None
    type: Optional[discord.ChannelType]=None
    parent_name: Optional[str]=None
    what: Optional[str]=None

@dataclass
class Plan:
    key: str
    source: discord.Guild
    target: discord.Guild
    requested_by: discord.abc.User
    include_deletes: bool
    lockdown: bool=False
    role_actions: List[RoleAction]=field(default_factory=list)
    channel_actions: List[ChannelAction]=field(default_factory=list)
    role_id_map: Dict[int,int]=field(default_factory=dict)
    cat_id_map: Dict[int,int]=field(default_factory=dict)
    chan_id_map: Dict[int,int]=field(default_factory=dict)
    warnings: List[str]=field(default_factory=list)
    control_msg: Optional[discord.Message]=None
    log_msg: Optional[discord.Message]=None
    lockdown_prev: Dict[int, discord.PermissionOverwrite]=field(default_factory=dict)
    log_lines: List[str]=field(default_factory=list)

# ---------- confirmation view (single-server) ----------
class ConfirmView(ui.View):
    def __init__(self, cog:"RevampSync", plan_key:str, timeout:float=900):
        super().__init__(timeout=timeout); self.cog=cog; self.plan_key=plan_key

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not inter.user.guild_permissions.administrator:
            await _ireply(inter, "Admin only.", ephemeral=True)
            return False
        return True

    @ui.button(label="Confirm Apply", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.get(self.plan_key)
        if not plan:
            return await _ireply(inter, "This sync request expired.", ephemeral=True)

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
                if plan.log_msg:     await plan.log_msg.edit(embed=self.cog._log_embed(plan))
            except Exception:
                pass
            await _ireply(inter, embed=result)
            self.stop()
        except Exception as e:
            await _ireply(inter, f"❌ Apply failed: {e}")
            self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, btn: ui.Button):
        plan = self.cog.pending.pop(self.plan_key, None)
        await _ireply(inter, "Cancelled. No changes made.")
        if plan:
            cancelled = discord.Embed(title="❎ Sync Cancelled", color=discord.Color.red())
            try:
                if plan.control_msg: await plan.control_msg.edit(embed=cancelled, view=None)
            except Exception:
                pass
        self.stop()

# ---------- the cog ----------
class RevampSync(commands.Cog):
    """Mirror roles, channels, forums, threads, and role overwrites from a revamp server to a main server.

    ADMIN handling: This cog **never moves ADMIN**. It only ensures it exists and the bot has it.
    If ADMIN is not the top-most role, a warning is logged, but no movement occurs.
    """

    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self.pending: Dict[str, Plan]={}
        self.rate_delay=0.35
        self.progress_every=6
        self.progress_min_secs=2.2

    # shortcut: [p]revamp <target> [mode] [include_deletes] [lockdown]
    @commands.hybrid_group(name="revamp", invoke_without_command=True)
    @commands.guild_only()
    async def revamp_group(self, ctx: commands.Context,
                           target_guild_id: Optional[int]=None,
                           mode: str="apply",
                           include_deletes: Optional[bool]=False,
                           lockdown: Optional[bool]=True):
        if target_guild_id:
            await self.revamp_sync.callback(self, ctx, ctx.guild.id, int(target_guild_id), mode, include_deletes, lockdown)  # type: ignore
        else:
            await ctx.send_help()

    @revamp_group.command(name="sync", with_app_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def revamp_sync(self, ctx: commands.Context,
                          source_guild_id: int, target_guild_id: int,
                          mode: str="dry",
                          include_deletes: Optional[bool]=False,
                          lockdown: Optional[bool]=True):
        source=self.bot.get_guild(source_guild_id); target=self.bot.get_guild(target_guild_id)
        if not source or not target: return await ctx.send("I must be in both guilds.")
        if not (source.me.guild_permissions.administrator and target.me.guild_permissions.administrator):
            return await ctx.send("I need **Administrator** in both guilds.")

        await ctx.typing()
        plan=await self._build_plan(source, target, bool(include_deletes), ctx.author, bool(lockdown))
        self.pending[plan.key]=plan

        ctrl=self._control_embed(plan, mode)
        view=ConfirmView(self, plan.key)
        plan.control_msg=await ctx.send(embed=ctrl, view=view)
        plan.log_msg=await ctx.send(embed=self._log_embed(plan, init=True))

        async def expire():
            await asyncio.sleep(900)
            if self.pending.pop(plan.key, None):
                try:
                    if plan.control_msg: await plan.control_msg.edit(embed=discord.Embed(title="⏲️ Sync request expired", color=discord.Color.orange()), view=None)
                except: pass
        self.bot.loop.create_task(expire())

    # ---------- planning ----------
    async def _build_plan(self, source: discord.Guild, target: discord.Guild, include_deletes: bool,
                          requested_by: discord.abc.User, lockdown: bool) -> Plan:
        key=f"{source.id}:{target.id}:{int(time.time())}"
        plan=Plan(key, source, target, requested_by, include_deletes, lockdown)

        # roles
        src_roles={r.id:r for r in (await source.fetch_roles())}
        tgt_roles={r.id:r for r in (await target.fetch_roles())}
        tgt_by_name={_norm(r.name):r for r in tgt_roles.values() if not r.managed}

        for r in sorted(src_roles.values(), key=lambda x: x.position):  # bottom->top
            if r.id==source.default_role.id or r.managed: continue
            t=tgt_by_name.get(_norm(r.name))
            if not t:
                plan.role_actions.append(RoleAction("create", r.name, data=_strip_role(r)))
            else:
                diff, ch=_role_diff(_strip_role(t), _strip_role(r))
                if diff: plan.role_actions.append(RoleAction("update", r.name, id=t.id, changes=ch))
                plan.role_id_map[r.id]=t.id

        if include_deletes:
            for r in tgt_roles.values():
                if r.id==target.default_role.id or r.managed: continue
                if not any(_norm(sr.name)==_norm(r.name) for sr in src_roles.values()):
                    plan.role_actions.append(RoleAction("delete", r.name, id=r.id))

        # channels (categories + non-categories incl. forums)
        src_all=await source.fetch_channels(); tgt_all=await target.fetch_channels()
        src_cats=[c for c in src_all if isinstance(c, discord.CategoryChannel)]
        tgt_cats=[c for c in tgt_all if isinstance(c, discord.CategoryChannel)]
        tgt_cat_by={c.name:c for c in tgt_cats}

        for c in sorted(src_cats, key=lambda c:c.position):
            ex=tgt_cat_by.get(c.name)
            if not ex: plan.channel_actions.append(ChannelAction("category","create", name=c.name))
            else:
                plan.cat_id_map[c.id]=ex.id
                if ex.position!=c.position:
                    plan.channel_actions.append(ChannelAction("category","update", id=ex.id, name=c.name, what="position"))

        if include_deletes:
            for tc in tgt_cats:
                if tc.name not in {c.name for c in src_cats}:
                    plan.channel_actions.append(ChannelAction("category","delete", id=tc.id, name=tc.name))

        def key_of(ch):
            parent=ch.category.name if ch.category else "__ROOT__"
            return f"{parent}|{ch.type}|{ch.name}"

        tgt_non=[c for c in tgt_all if not isinstance(c, discord.CategoryChannel)]
        tgt_by_key={key_of(c):c for c in tgt_non}
        src_non=[c for c in src_all if not isinstance(c, discord.CategoryChannel)]

        for s in sorted(src_non, key=lambda c:c.position):
            k=key_of(s); ex=tgt_by_key.get(k)
            if not ex:
                plan.channel_actions.append(ChannelAction("channel","create", name=s.name, type=s.type,
                                                          parent_name=(s.category.name if s.category else None)))
            else:
                plan.chan_id_map[s.id]=ex.id
                if _chan_changed(ex, s):
                    plan.channel_actions.append(ChannelAction("channel","update", id=ex.id, name=s.name, type=s.type,
                                                              parent_name=(s.category.name if s.category else None),
                                                              what="config/position/overwrites"))

        if include_deletes:
            for tc in tgt_non:
                parent=tc.category.name if tc.category else "__ROOT__"
                kk=f"{parent}|{tc.type}|{tc.name}"
                if not any(kk==key_of(s) for s in src_non):
                    plan.channel_actions.append(ChannelAction("channel","delete", id=tc.id, name=tc.name, type=tc.type))

        return plan

    # ---------- embeds ----------
    def _control_embed(self, p: Plan, mode: str) -> discord.Embed:
        rc=sum(1 for a in p.role_actions if a.kind=="create")
        ru=sum(1 for a in p.role_actions if a.kind=="update")
        rd=sum(1 for a in p.role_actions if a.kind=="delete")
        cc=sum(1 for c in p.channel_actions if c.op=="create")
        cu=sum(1 for c in p.channel_actions if c.op=="update")
        cd=sum(1 for c in p.channel_actions if c.op=="delete")
        emb=discord.Embed(
            title="✨ Revamp → Main — Control Panel",
            color=discord.Color.blurple(),
            description=(
                f"**Source:** {discord.utils.escape_markdown(p.source.name)}\n"
                f"**Target:** {discord.utils.escape_markdown(p.target.name)}\n"
                f"**Requested by:** {p.requested_by.mention}\n\n"
                f"**Mode:** {'DRY RUN' if mode.lower()=='dry' else 'APPLY'}   "
                f"• **Deletes:** {'Yes' if p.include_deletes else 'No'}   "
                f"• **Lockdown:** {'Yes' if p.lockdown else 'No'}"
            ),
        )
        emb.add_field(name="👤 Roles", value=f"Create **{rc}** • Update **{ru}** • Delete **{rd}**", inline=False)
        emb.add_field(name="📺 Channels", value=f"Create **{cc}** • Update **{cu}** • Delete **{cd}**", inline=False)
        if p.warnings:
            preview="\n".join(f"• {w}" for w in p.warnings[:6])
            if len(p.warnings)>6: preview+=f"\n… +{len(p.warnings)-6} more"
            emb.add_field(name="⚠️ Warnings", value=preview, inline=False)
        emb.set_footer(text="Press Confirm Apply to start. You can Cancel to stop.")
        emb.timestamp=discord.utils.utcnow()
        return emb

    def _log_embed(self, p: Plan, init: bool=False) -> discord.Embed:
        emb=discord.Embed(title="📜 Live Log", color=discord.Color.dark_grey())
        if init and not p.log_lines:
            p.log_lines.append("🟡 waiting for confirmation…")
        text="\n".join(p.log_lines[-25:]) or "—"
        emb.description=text
        emb.timestamp=discord.utils.utcnow()
        return emb

    async def _append_log(self, p: Plan, line: str):
        p.log_lines.append(line)
        if p.log_msg:
            try: await p.log_msg.edit(embed=self._log_embed(p))
            except: pass

    # ---------- ADMIN anchor (never moved) ----------
    def _bot_top_position(self, guild: discord.Guild) -> int:
        me = guild.me
        return max((r.position for r in (me.roles if me else [])), default=0)

    async def _ensure_admin_anchor(self, plan: Plan) -> discord.Role:
        """Ensure ADMIN exists and the bot has it. Do NOT move it; only report if not top."""
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

        # give it to the bot if missing
        if admin_role not in me.roles:
            try:
                await me.add_roles(admin_role, reason="Revamp sync: grant ADMIN anchor to bot")
                await asyncio.sleep(self.rate_delay)
            except discord.Forbidden:
                raise RuntimeError("Missing permissions to assign ADMIN anchor to the bot.")

        # confirm/announce position; DO NOT MOVE
        top_pos = len(guild.roles) - 1
        pos_msg = f"🔐 ADMIN position is {admin_role.position}/{top_pos} (top index = {top_pos})"
        await self._append_log(plan, pos_msg)
        if admin_role.position != top_pos:
            warn = (f"ADMIN is not at the very top (pos {admin_role.position} < {top_pos}). "
                    f"This cog will NOT move it; drag ADMIN to the top manually if required.")
            plan.warnings.append(warn)
            await self._append_log(plan, f"⚠️ {warn}")

        return admin_role

    # ---------- apply + rollback ----------
    async def apply_plan(self, p: Plan) -> discord.Embed:
        s, t = p.source, p.target
        results={"role_create":0,"role_update":0,"role_delete":0,"chan_create":0,"chan_update":0,"chan_delete":0}

        journal={"created_roles":[], "updated_roles":[], "deleted_roles":[],
                 "created_channels":[], "updated_channels":[], "deleted_channels":[]}

        last_edit=time.monotonic(); ops=0
        async def progress(step_key:str, human_line:str):
            nonlocal last_edit, ops
            await self._append_log(p, f"{_icon(step_key)} {human_line}")
            now=time.monotonic()
            if ops>=self.progress_every or (now-last_edit)>=self.progress_min_secs:
                if p.control_msg:
                    try: await p.control_msg.edit(embed=self._progress_embed(p, results, human_line))
                    except: pass
                last_edit=now; ops=0

        async def do(coro, note:str, critical:bool=False):
            nonlocal ops
            retries, delay=5, 1.0
            while True:
                try:
                    out=await coro
                    await asyncio.sleep(self.rate_delay); ops+=1
                    return out
                except discord.Forbidden as e:
                    msg=f"{note}: Forbidden ({e})"; p.warnings.append(msg)
                    if critical:
                        await rollback(msg); raise
                    return None
                except discord.HTTPException as e:
                    if retries<=0:
                        msg=f"{note}: {e}"; p.warnings.append(msg)
                        if critical: await rollback(msg); raise
                        return None
                    await asyncio.sleep(delay); retries-=1; delay=min(delay*2, 8.0)
                except Exception as e:
                    msg=f"{note}: {e}"; p.warnings.append(msg)
                    if critical: await rollback(msg); raise
                    return None

        async def rollback(reason:str):
            try:
                for cid, prev in reversed(journal["updated_channels"]):
                    ch=t.get_channel(cid)
                    if ch:
                        try: await ch.edit(**prev, reason=f"Rollback: {reason}")
                        except: pass
                for ch in reversed(journal["created_channels"]):
                    try: await ch.delete(reason=f"Rollback: {reason}")
                    except: pass
                for snap in reversed(journal["deleted_roles"]):
                    try:
                        await t.create_role(name=snap["name"], colour=discord.Colour(snap["color"]), hoist=snap["hoist"],
                                            mentionable=snap["mentionable"], permissions=discord.Permissions(snap["permissions"]),
                                            reason=f"Rollback: {reason}")
                    except: pass
                for rid, prev in reversed(journal["updated_roles"]):
                    r=t.get_role(rid)
                    if r:
                        try:
                            await r.edit(colour=discord.Colour(prev["color"]), hoist=prev["hoist"], mentionable=prev["mentionable"],
                                         permissions=discord.Permissions(prev["permissions"]), reason=f"Rollback: {reason}")
                        except: pass
                for r in reversed(journal["created_roles"]):
                    try: await r.delete(reason=f"Rollback: {reason}")
                    except: pass
            except: pass
            try:
                err=discord.Embed(title="❗ Sync rolled back due to error", description=reason, color=discord.Color.red())
                if p.control_msg: await p.control_msg.channel.send(embed=err)
            except: pass

        # start
        await progress("start", "starting…")

        # lockdown
        if p.lockdown:
            await self._toggle_lock(t, enable=True, plan=p)
            await progress("lock", "target locked for maintenance")

        # ***** ADMIN anchor exists + bot has it (NEVER MOVED) *****
        try:
            admin_role = await self._ensure_admin_anchor(p)
        except Exception as e:
            await self._append_log(p, f"❌ ADMIN anchor setup failed: {e}")
            raise

        bot_top = self._bot_top_position(t)

        # roles create/update/delete (respect hierarchy)
        for a in p.role_actions:
            if a.kind=="create" and a.data:
                created=await do(t.create_role(
                    name=a.data["name"], colour=discord.Colour(a.data["color"]), hoist=a.data["hoist"],
                    mentionable=a.data["mentionable"], permissions=discord.Permissions(a.data["permissions"]),
                    reason="Revamp sync: create role"
                ), f"Failed to create role {a.data.get('name')}", critical=True)
                if created:
                    try:
                        await created.edit(position=1, reason="Revamp sync: seed under ADMIN")
                        await asyncio.sleep(self.rate_delay)
                    except Exception: pass
                    journal["created_roles"].append(created)
                    results["role_create"]+=1
                    await progress("roles_create", f"created role {created.name!r}")

            elif a.kind=="update" and a.id and a.changes:
                tgt=t.get_role(a.id)
                if not tgt: continue
                if tgt.position >= bot_top:
                    p.warnings.append(f"Skip update for role {tgt.name!r}: at/above bot's highest role (pos {tgt.position} ≥ {bot_top})")
                    await self._append_log(p, f"⚠️ skipping update for {tgt.name!r} due to hierarchy")
                    continue
                prev=_strip_role(tgt)
                edited=await do(tgt.edit(
                    colour=discord.Colour(a.changes.get("color",{}).get("to", tgt.colour.value)),
                    hoist=a.changes.get("hoist",{}).get("to", tgt.hoist),
                    mentionable=a.changes.get("mentionable",{}).get("to", tgt.mentionable),
                    permissions=discord.Permissions(a.changes.get("permissions",{}).get("to", tgt.permissions.value)),
                    reason="Revamp sync: update role"
                ), f"Failed to update role {a.name}")
                if edited is not None:
                    journal["updated_roles"].append((tgt.id, prev))
                    results["role_update"]+=1
                    await progress("roles_update", f"updated role {tgt.name!r}")

            elif a.kind=="delete" and a.id:
                tgt=t.get_role(a.id)
                if tgt:
                    if tgt.position >= bot_top:
                        p.warnings.append(f"Skip delete for role {tgt.name!r}: at/above bot's highest role (pos {tgt.position} ≥ {bot_top})")
                        await self._append_log(p, f"⚠️ skipping delete for {tgt.name!r} due to hierarchy")
                        continue
                    if any(_norm(sr.name)==_norm(tgt.name) for sr in (await s.fetch_roles())):
                        continue
                    prev=_strip_role(tgt)
                    ok=await do(tgt.delete(reason="Revamp sync: remove role not in source"), f"Failed to delete role {tgt.name}")
                    if ok is not None:
                        journal["deleted_roles"].append(prev)
                        results["role_delete"]+=1
                        await progress("roles_delete", f"deleted role {prev['name']!r}")

        # refresh mapping by name
        fresh=await t.fetch_roles()
        by_name={_norm(r.name):r for r in fresh if not r.managed}
        for sr in (await s.fetch_roles()):
            if sr.id==s.default_role.id or sr.managed: continue
            tr=by_name.get(_norm(sr.name))
            if tr: p.role_id_map[sr.id]=tr.id

        # reorder roles strictly between @everyone and ADMIN (ADMIN never moved)
        await self._reorder_roles_like_source(p)
        await progress("roles_order", "reordered roles under ADMIN")

        # channels deletes (optional first)
        if p.include_deletes:
            all_t=await t.fetch_channels()
            for ch in sorted([c for c in all_t if not isinstance(c, discord.CategoryChannel)], key=lambda c:c.position, reverse=True):
                if any(x.op=="delete" and x.id==ch.id for x in p.channel_actions):
                    ok=await do(ch.delete(reason="Revamp sync: remove channel not in source"), f"Failed to delete channel {ch.name}")
                    if ok is not None:
                        results["chan_delete"]+=1; await progress("chan_delete", f"deleted channel {ch.name}")
            for cat in sorted([c for c in all_t if isinstance(c, discord.CategoryChannel)], key=lambda c:c.position, reverse=True):
                if any(x.op=="delete" and x.id==cat.id for x in p.channel_actions):
                    ok=await do(cat.delete(reason="Revamp sync: remove category not in source"), f"Failed to delete category {cat.name}")
                    if ok is not None:
                        results["chan_delete"]+=1; await progress("chan_delete", f"deleted category {cat.name!r}")

        # ensure categories exist/order
        src_cats=[c for c in await s.fetch_channels() if isinstance(c, discord.CategoryChannel)]
        tgt_cats=[c for c in await t.fetch_channels() if isinstance(c, discord.CategoryChannel)]
        by_name_cat={c.name:c for c in tgt_cats}
        for cat in sorted(src_cats, key=lambda c:c.position):
            ex=by_name_cat.get(cat.name)
            if not ex:
                created=await do(t.create_category(name=cat.name, reason="Revamp sync: create category"), f"Failed to create category {cat.name}")
                if created:
                    p.cat_id_map[cat.id]=created.id; results["chan_create"]+=1
                    await progress("cat_create", f"created category {cat.name!r}")
            else:
                p.cat_id_map[cat.id]=ex.id
                if ex.position!=cat.position:
                    await do(ex.edit(position=cat.position), f"Category order issue for {cat.name}")

        # create/update channels (Text, Voice, Forum)
        src_non=[c for c in await s.fetch_channels() if not isinstance(c, discord.CategoryChannel)]
        tgt_non=[c for c in await t.fetch_channels() if not isinstance(c, discord.CategoryChannel)]
        def find_match(src):
            for c in tgt_non:
                if c.name==src.name and c.type==src.type and ((c.category.name if c.category else None)==(src.category.name if src.category else None)):
                    return c
        created_map={}
        for src in sorted(src_non, key=lambda c:c.position):
            ex=find_match(src)
            parent_id=p.cat_id_map.get(getattr(src,"category_id",None)) if getattr(src,"category_id",None) else None
            parent_obj=t.get_channel(parent_id) if parent_id else None
            if not ex:
                if src.type is discord.ChannelType.text:
                    ch=await do(t.create_text_channel(
                        name=src.name, category=parent_obj,
                        topic=getattr(src,"topic",None), nsfw=getattr(src,"nsfw",False),
                        slowmode_delay=getattr(src,"slowmode_delay",0) or getattr(src,"rate_limit_per_user",0),
                        reason="Revamp sync: create channel"
                    ), f"Failed to create text channel {src.name}")
                elif src.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                    ch=await do(t.create_voice_channel(
                        name=src.name, category=parent_obj,
                        bitrate=getattr(src,"bitrate",None), user_limit=getattr(src,"user_limit",None),
                        reason="Revamp sync: create channel"
                    ), f"Failed to create voice channel {src.name}")
                elif src.type is discord.ChannelType.forum:
                    # Use Guild.create_forum (discord.py 2.x). Avoid unknown kwargs for cross-version safety.
                    fkw = {
                        "category": parent_obj,
                        "nsfw": getattr(src,"nsfw",False),
                        "default_layout": getattr(src,"default_layout", None),
                        "default_sort_order": getattr(src,"default_sort_order", None),
                        "default_reaction_emoji": getattr(src,"default_reaction_emoji", None),
                        "default_thread_slowmode_delay": getattr(src,"default_thread_slowmode_delay", 0),
                        "reason": "Revamp sync: create forum",
                    }
                    src_tags = getattr(src, "available_tags", [])
                    if src_tags:
                        fkw["available_tags"] = [discord.ForumTag(name=tag.name) for tag in src_tags]
                    # filter Nones
                    kwargs = {k:v for k,v in fkw.items() if v is not None}
                    ch=await do(t.create_forum(name=src.name, **kwargs),
                                f"Failed to create forum channel {src.name}")
                else:
                    ch=await do(t.create_text_channel(name=src.name, category=parent_obj,
                                                      reason="Revamp sync: create channel (fallback)"),
                                f"Failed to create channel {src.name}")
                if ch:
                    journal["created_channels"].append(ch)
                    created_map[src.id]=ch; results["chan_create"]+=1
                    await progress("chan_create", f"created channel {src.name}")
            else:
                kwargs={"reason":"Revamp sync: update channel"}
                if hasattr(ex,"category"): kwargs["category"]=parent_obj
                if hasattr(ex,"topic"): kwargs["topic"]=getattr(src,"topic",None)
                if hasattr(ex,"nsfw"): kwargs["nsfw"]=getattr(src,"nsfw",False)
                if hasattr(ex,"slowmode_delay"): kwargs["slowmode_delay"]=getattr(src,"slowmode_delay",0) or getattr(src,"rate_limit_per_user",0)
                if hasattr(ex,"bitrate"): kwargs["bitrate"]=getattr(src,"bitrate",None)
                if hasattr(ex,"user_limit"): kwargs["user_limit"]=getattr(src,"user_limit",None)
                if isinstance(ex, discord.ForumChannel) and isinstance(src, discord.ForumChannel):
                    kwargs["default_layout"] = getattr(src,"default_layout", None)
                    kwargs["default_sort_order"] = getattr(src,"default_sort_order", None)
                    kwargs["default_reaction_emoji"] = getattr(src,"default_reaction_emoji", None)
                    kwargs["default_thread_slowmode_delay"] = getattr(src,"default_thread_slowmode_delay",0)
                    src_tags = getattr(src, "available_tags", [])
                    if src_tags:
                        kwargs["available_tags"] = [discord.ForumTag(name=tag.name) for tag in src_tags]
                snap={}
                for k in ("category","topic","nsfw","slowmode_delay","bitrate","user_limit"):
                    if hasattr(ex, k): snap[k]=getattr(ex,k)
                journal["updated_channels"].append((ex.id, snap))
                await do(ex.edit(**{k:v for k,v in kwargs.items() if v is not None}), f"Failed to update channel {src.name}")
                await do(ex.edit(position=src.position), f"Failed to set position for {src.name}")
                p.chan_id_map[src.id]=ex.id; created_map[src.id]=ex; results["chan_update"]+=1
                await progress("chan_update", f"updated channel {src.name}")

        # overwrites (roles only) on channel objects we touched
        for src in src_non:
            tgt_ch=created_map.get(src.id)
            if not tgt_ch: continue
            ow={}
            for obj, po in src.overwrites.items():
                if isinstance(obj, discord.Role):
                    target_role = (t.default_role if obj==s.default_role else t.get_role(p.role_id_map.get(obj.id,0)))
                    if not target_role: continue
                    a,d=po.pair()
                    allow, deny = discord.Permissions(a.value), discord.Permissions(d.value)
                    ow[target_role]=discord.PermissionOverwrite.from_pair(allow, deny)
            await do(tgt_ch.edit(overwrites=ow, reason="Revamp sync: mirror role overwrites"), f"Overwrite issue in {getattr(src,'name','?')}")

        # Threads (Text + Forum)
        await self._append_log(p, "🧵 syncing threads…")
        await self._sync_threads(p, src_non, created_map)

        if p.lockdown:
            await self._toggle_lock(t, enable=False, plan=p)
            await progress("unlock", "target unlocked")

        # done
        final=discord.Embed(title="✅ Revamp → Main Sync Completed", color=discord.Color.green())
        final.add_field(name="👤 Roles", value=f"Created **{results['role_create']}** • Updated **{results['role_update']}** • Deleted **{results['role_delete']}**", inline=False)
        final.add_field(name="📺 Channels", value=f"Created **{results['chan_create']}** • Updated **{results['chan_update']}** • Deleted **{results['chan_delete']}**", inline=False)
        if p.warnings:
            preview="\n".join(f"• {w}" for w in p.warnings[:10])
            if len(p.warnings)>10: preview+=f"\n… +{len(p.warnings)-10} more"
            final.add_field(name="Notes", value=preview, inline=False)
        final.timestamp=discord.utils.utcnow()
        await progress("done", "completed")
        return final

    # ---- threads helper ----
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
        seen=set(); uniq=[]
        for th in threads:
            if th.id in seen: continue
            seen.add(th.id); uniq.append(th)
        return uniq

    async def _sync_threads(self, p: Plan, src_non: List[discord.abc.GuildChannel], created_map: Dict[int, discord.abc.GuildChannel]):
        s, t = p.source, p.target
        for src_parent in src_non:
            if not isinstance(src_parent, (discord.TextChannel, discord.ForumChannel)):
                continue
            tgt_parent = created_map.get(src_parent.id)
            if not tgt_parent:
                # resolve by name/type/category if not touched
                parent_id = p.cat_id_map.get(getattr(src_parent,"category_id",None)) if getattr(src_parent,"category_id",None) else None
                for c in (await t.fetch_channels()):
                    if not isinstance(c, (discord.TextChannel, discord.ForumChannel)): continue
                    if c.name==src_parent.name and c.type==src_parent.type and ((c.category.id if c.category else None)==parent_id or parent_id is None):
                        tgt_parent=c; break
                else:
                    continue

            try:
                src_threads = await self._gather_threads(src_parent)
            except Exception:
                src_threads = []
            try:
                tgt_threads = await self._gather_threads(tgt_parent)
            except Exception:
                tgt_threads = []

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
                    p.warnings.append(f"Could not create thread '{th.name}' in {tgt_parent.name}: {e}")

            if p.include_deletes:
                for key, th in tgt_by_name.items():
                    if key in src_by_name:
                        continue
                    try:
                        await th.delete(reason="Revamp sync: remove thread not in source")
                        await asyncio.sleep(self.rate_delay)
                    except Exception as e:
                        p.warnings.append(f"Could not delete thread '{th.name}' in {tgt_parent.name}: {e}")

    # ---------- progress/embeds ----------
    def _progress_embed(self, p: Plan, results: dict, step_text: str) -> discord.Embed:
        emb=discord.Embed(title="✨ Revamp → Main — Control Panel", color=discord.Color.blurple(),
                          description=(f"**Source:** {discord.utils.escape_markdown(p.source.name)}\n"
                                       f"**Target:** {discord.utils.escape_markdown(p.target.name)}"))
        emb.add_field(name="⏳ Progress", value=(
            f"{step_text}\n"
            f"Roles — C:{results['role_create']} U:{results['role_update']} D:{results['role_delete']}\n"
            f"Chans — C:{results['chan_create']} U:{results['chan_update']} D:{results['chan_delete']}"
        ), inline=False)
        emb.timestamp=discord.utils.utcnow()
        return emb

    # ---------- lockdown ----------
    async def _toggle_lock(self, guild: discord.Guild, enable: bool, plan: Plan):
        everyone=guild.default_role
        if enable: plan.lockdown_prev.clear()
        for ch in [c for c in await guild.fetch_channels() if isinstance(c, discord.TextChannel)]:
            try:
                current=ch.overwrites_for(everyone)
                if enable:
                    plan.lockdown_prev[ch.id]=current
                    new=current; new.send_messages=False; new.add_reactions=False
                    await ch.set_permissions(everyone, overwrite=new, reason="Revamp sync: lockdown")
                else:
                    prev=plan.lockdown_prev.get(ch.id, current)
                    await ch.set_permissions(everyone, overwrite=prev, reason="Revamp sync: unlock")
                await asyncio.sleep(self.rate_delay)
            except discord.Forbidden as e:
                plan.warnings.append(f"Lockdown change forbidden in #{ch.name}: {e}")
            except Exception as e:
                plan.warnings.append(f"Lockdown change failed in #{ch.name}: {e}")

    # ---------- role ordering (between @everyone and ADMIN only) ----------
    async def _reorder_roles_like_source(self, p: Plan) -> None:
        """
        Reorder roles bottom→top to match source, but strictly between:
          @everyone (pos 0)  <  [ALL SYNCED ROLES]  <  ADMIN anchor (our ceiling)
        We never move @everyone or ADMIN; we never place anything at/above ADMIN or below 1.
        """
        target, source = p.target, p.source
        try:
            admin = next((r for r in target.roles if not r.managed and _norm(r.name) == _norm(ADMIN_ANCHOR_NAME)), None)
            if not admin:
                raise RuntimeError(f"{ADMIN_ANCHOR_NAME} anchor not found on target.")
            admin_pos = admin.position

            src_sorted = [r for r in await source.fetch_roles()
                          if not r.managed and r.id != source.default_role.id]
            src_sorted.sort(key=lambda r:r.position)  # bottom→top

            desired: List[discord.Role] = []
            skipped: List[str] = []  # for log
            for sr in src_sorted:
                tr = target.get_role(p.role_id_map.get(sr.id, 0))
                if not tr:
                    skipped.append(f"{sr.name!r} (missing in target)")
                    continue
                if tr.managed or tr.id == target.default_role.id or tr.id == admin.id:
                    skipped.append(f"{tr.name!r} (managed/@everyone/ADMIN)")
                    continue
                if tr.position >= admin_pos:
                    skipped.append(f"{tr.name!r} (at/above ADMIN pos {tr.position} ≥ {admin_pos})")
                    continue
                desired.append(tr)

            for why in skipped:
                await self._append_log(p, f"⚠️ skipping role {why}")

            pos = 1
            for role in desired:
                try:
                    new_pos = min(admin_pos - 1, max(1, pos))
                    if role.position != new_pos:
                        await role.edit(position=new_pos, reason="Revamp sync: reorder under ADMIN")
                        await asyncio.sleep(self.rate_delay)
                        await self._append_log(p, f"📚 moved {role.name!r}: {role.position} → {new_pos}")
                    pos += 1
                except discord.Forbidden as e:
                    msg = f"Unable to move role {role.name} (forbidden): {e}"
                    p.warnings.append(msg)
                    await self._append_log(p, f"⚠️ {msg}")
                except discord.HTTPException as e:
                    msg = f"Unable to move role {role.name}: {e}"
                    p.warnings.append(msg)
                    await self._append_log(p, f"⚠️ {msg}")

            snap = [r for r in target.roles if not r.managed and r != target.default_role]
            snap.sort(key=lambda r: r.position)
            preview = ", ".join([r.name for r in snap[:8]]) + ("…" if len(snap) > 8 else "")
            await self._append_log(p, f"🧭 bottom now: {preview}; ADMIN pos={admin_pos}")

        except Exception as e:
            msg = f"Role reordering issues: {e}"
            p.warnings.append(msg)
            await self._append_log(p, f"⚠️ {msg}")
