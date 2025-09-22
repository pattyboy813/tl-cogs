# revamp_sync.py
# Red-DiscordBot Cog — Revamp -> Main Sync
# Single-server confirm, Control Panel embed + Live Log embed, role reorder fix, rollback, lockdown, backoff.

from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord import ui
from redbot.core import commands

__red_end_user_data_statement__ = "This cog stores no personal user data."

# ---------- helpers ----------
def _norm(s: str) -> str: return (s or "").strip().lower()
def _icon(step: str) -> str:
    return {"start":"🟡","lock":"🔒","roles_create":"👤","roles_update":"👤","roles_delete":"🧹",
            "roles_order":"📚","chan_delete":"🧹","cat_create":"🗂️","chan_create":"📺",
            "chan_update":"🛠️","overwrites":"🔐","unlock":"🔓","done":"✅"}.get(step,"🔧")

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
        if (getattr(tgt,"topic",None) or "")!=(getattr(src,"topic",None) or ""): return True
        if bool(getattr(tgt,"nsfw",False))!=bool(getattr(src,"nsfw",False)): return True
        trl=getattr(tgt,"slowmode_delay",0) or getattr(tgt,"rate_limit_per_user",0)
        srl=getattr(src,"slowmode_delay",0) or getattr(src,"rate_limit_per_user",0)
        if int(trl or 0)!=int(srl or 0): return True
        if (tgt.category.name if tgt.category else None)!=(src.category.name if src.category else None): return True
        for a in ("bitrate","user_limit"):
            if getattr(tgt,a,None)!=getattr(src,a,None): return True
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
        # Fallback if token/msg is gone
        try:
            await inter.channel.send(content or None, embed=embed)
        except Exception:
            pass
    except Exception:
        # Don’t let UI reply errors kill the flow
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

        # Acknowledge ASAP to keep the token valid
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
    """Mirror roles, channels, and role overwrites from a revamp server to a main server."""

    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self.pending: Dict[str, Plan]={}
        self.rate_delay=0.6
        self.progress_every=6
        self.progress_min_secs=2.5

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

        # pretty control embed + separate live log embed
        ctrl=self._control_embed(plan, mode)
        view=ConfirmView(self, plan.key)
        plan.control_msg=await ctx.send(embed=ctrl, view=view)
        plan.log_msg=await ctx.send(embed=self._log_embed(plan, init=True))

        # expire after 15m
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

        # channels
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

    def _log_embed(self, p: Plan, init: bool=False, tail_only: bool=False) -> discord.Embed:
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
                for snap in reversed(journal["deleted_channels"]):
                    try:
                        parent=t.get_channel(snap.get("parent_id")) if snap.get("parent_id") else None
                        if snap["type"]==int(discord.ChannelType.text):
                            await t.create_text_channel(name=snap["name"], category=parent, topic=snap.get("topic"), reason=f"Rollback: {reason}")
                        elif snap["type"] in (int(discord.ChannelType.voice), int(discord.ChannelType.stage_voice)):
                            await t.create_voice_channel(name=snap["name"], category=parent, reason=f"Rollback: {reason}")
                        else:
                            await t.create_text_channel(name=snap["name"], category=parent, reason=f"Rollback: {reason}")
                    except: pass
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
            # ping
            try:
                err=discord.Embed(title="❗ Sync rolled back due to error", description=reason, color=discord.Color.red())
                if p.control_msg: await p.control_msg.channel.send("<@BigPattyOG>", embed=err)
            except: pass

        # start
        await progress("start", "starting…")

        # lockdown
        if p.lockdown:
            await self._toggle_lock(t, enable=True, plan=p)
            await progress("lock", "target locked for maintenance")

        # roles create/update/delete
        for a in p.role_actions:
            if a.kind=="create" and a.data:
                created=await do(t.create_role(
                    name=a.data["name"], colour=discord.Colour(a.data["color"]), hoist=a.data["hoist"],
                    mentionable=a.data["mentionable"], permissions=discord.Permissions(a.data["permissions"]),
                    reason="Revamp sync: create role"
                ), f"Failed to create role {a.data.get('name')}", critical=True)
                if created:
                    journal["created_roles"].append(created)
                    results["role_create"]+=1
                    await progress("roles_create", f"created role {created.name!r}")

            elif a.kind=="update" and a.id and a.changes:
                tgt=t.get_role(a.id)
                if not tgt: continue
                prev=_strip_role(tgt)
                edited=await do(tgt.edit(
                    colour=discord.Colour(a.changes.get("color",{}).get("to", tgt.colour.value)),
                    hoist=a.changes.get("hoist",{}).get("to", tgt.hoist),
                    mentionable=a.changes.get("mentionable",{}).get("to", tgt.mentionable),
                    permissions=discord.Permissions(a.changes.get("permissions",{}).get("to", tgt.permissions.value)),
                    reason="Revamp sync: update role"
                ), f"Failed to update role {a.name}", critical=True)
                if edited is not None:
                    journal["updated_roles"].append((tgt.id, prev))
                    results["role_update"]+=1
                    await progress("roles_update", f"updated role {tgt.name!r}")

            elif a.kind=="delete" and a.id:
                tgt=t.get_role(a.id)
                if tgt:
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

        # reorder roles (absolute positions bottom -> top, min pos = 1, skip above bot top)
        await self._reorder_roles_like_source(p)
        await progress("roles_order", "reordered roles to match source")

        # channels deletes (optional)
        if p.include_deletes:
            all_t=await t.fetch_channels()
            for ch in sorted([c for c in all_t if not isinstance(c, discord.CategoryChannel)], key=lambda c:c.position, reverse=True):
                if any(x.op=="delete" and x.id==ch.id for x in p.channel_actions):
                    ok=await do(ch.delete(reason="Revamp sync: remove channel not in source"), f"Failed to delete channel {ch.name}")
                    if ok is not None:
                        results["chan_delete"]+=1; await progress("chan_delete", f"deleted channel #{ch.name}")
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

        # create/update channels
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
            if not ex:
                if src.type is discord.ChannelType.text:
                    ch=await do(t.create_text_channel(
                        name=src.name, category=t.get_channel(parent_id) if parent_id else None,
                        topic=getattr(src,"topic",None), nsfw=getattr(src,"nsfw",False),
                        slowmode_delay=getattr(src,"slowmode_delay",0) or getattr(src,"rate_limit_per_user",0),
                        reason="Revamp sync: create channel"
                    ), f"Failed to create text channel {src.name}")
                elif src.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                    ch=await do(t.create_voice_channel(
                        name=src.name, category=t.get_channel(parent_id) if parent_id else None,
                        bitrate=getattr(src,"bitrate",None), user_limit=getattr(src,"user_limit",None),
                        reason="Revamp sync: create channel"
                    ), f"Failed to create voice channel {src.name}")
                else:
                    ch=await do(t.create_text_channel(name=src.name, category=t.get_channel(parent_id) if parent_id else None,
                                                      reason="Revamp sync: create channel (fallback)"),
                                f"Failed to create channel {src.name}")
                if ch:
                    journal["created_channels"].append(ch)
                    created_map[src.id]=ch; results["chan_create"]+=1
                    await progress("chan_create", f"created channel #{src.name}")
            else:
                kwargs={"reason":"Revamp sync: update channel"}
                if hasattr(ex,"category"): kwargs["category"]=t.get_channel(parent_id) if parent_id else None
                if hasattr(ex,"topic"): kwargs["topic"]=getattr(src,"topic",None)
                if hasattr(ex,"nsfw"): kwargs["nsfw"]=getattr(src,"nsfw",False)
                if hasattr(ex,"slowmode_delay"): kwargs["slowmode_delay"]=getattr(src,"slowmode_delay",0) or getattr(src,"rate_limit_per_user",0)
                if hasattr(ex,"bitrate"): kwargs["bitrate"]=getattr(src,"bitrate",None)
                if hasattr(ex,"user_limit"): kwargs["user_limit"]=getattr(src,"user_limit",None)
                # snapshot before update for rollback
                snap={}
                for k in ("category","topic","nsfw","slowmode_delay","bitrate","user_limit"):
                    if hasattr(ex, k): snap[k]=getattr(ex,k)
                journal["updated_channels"].append((ex.id, snap))
                await do(ex.edit(**kwargs), f"Failed to update channel {src.name}")
                await do(ex.edit(position=src.position), f"Failed to set position for {src.name}")
                p.chan_id_map[src.id]=ex.id; created_map[src.id]=ex; results["chan_update"]+=1
                await progress("chan_update", f"updated channel #{src.name}")

        # overwrites (roles only)
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
            await progress("overwrites", f"set overwrites for #{getattr(src,'name','?')}")

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
            except Exception as e:
                plan.warnings.append(f"Lockdown change failed in #{ch.name}: {e}")

    async def _reorder_roles_like_source(self, p: Plan) -> None:
        """Place roles to match source exactly, bottom→top using absolute positions (1..n)."""
        target, source = p.target, p.source
        try:
            src_sorted = [r for r in await source.fetch_roles()
                          if not r.managed and r.id != source.default_role.id]
            src_sorted.sort(key=lambda r:r.position)  # bottom→top

            # Bot cannot move roles above its highest role
            try:
                bot_top = max((r.position for r in target.me.roles), default=1)
            except Exception:
                bot_top = 1

            desired: List[discord.Role] = []
            for sr in src_sorted:
                tr = target.get_role(p.role_id_map.get(sr.id, 0))
                if not tr or tr.managed:
                    continue
                if tr.position > bot_top:
                    p.warnings.append(f"Cannot move role {tr.name}: above bot’s top role")
                    continue
                desired.append(tr)

            pos = 1  # @everyone is 0
            for role in desired:
                try:
                    if role.position != pos:
                        await role.edit(position=max(1, pos), reason="Revamp sync: reorder roles")
                        await asyncio.sleep(self.rate_delay)
                    pos += 1
                except discord.HTTPException as e:
                    p.warnings.append(f"Unable to move role {role.name}: {e}")
        except Exception as e:
            p.warnings.append(f"Role reordering issues: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(RevampSync(bot))
