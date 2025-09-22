# revamp_sync.py
# Red-DiscordBot Cog — Revamp -> Main Sync (fast/minimal logging edition)
# - Never moves the ADMIN role; only verifies/assigns it if needed
# - Minimal logging: no live log message; only a single control panel updated occasionally
# - Speed-ups: skip no-op edits (roles/channels/positions/overwrites), lighter sleeps
# - Optional thread sync (off by default)
# - Cleaner, more helpful control/progress embed
#
# NEW:
# - Modes: auto | clone | update | sync  (plus "dry" preview UI)
# - Empty-server detection for first-time clone
# - "sync" preserves target channel overwrites (permissions) and only changes what's needed
# - "update" keeps your previous smart-mirror behavior (incl. deletes if requested)

from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord import ui
from redbot.core import commands

__red_end_user_data_statement__ = "This cog stores no personal user data."

# ---------- config ----------
ADMIN_ANCHOR_NAME = "ADMIN"  # anchor role; never moved by this cog

# ---------- helpers ----------
def _norm(s: str) -> str: return (s or "").strip().lower()

def _icon(step: str) -> str:
    return {
        "ready": "🟡", "start": "🟡", "lock": "🔒", "roles": "👤",
        "cats": "🗂️", "chans": "📺", "overwrites": "🔐",
        "threads": "🧵", "unlock": "🔓", "done": "✅"
    }.get(step, "🔧")

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

def _chan_changed(tgt, src) -> bool:
    # coarse check used in planning; we’ll still do fine-grained diffs at apply time
    try:
        if tgt.type != src.type: return True
        t_topic = getattr(tgt, "topic", None); s_topic = getattr(src, "topic", None)
        if (t_topic or "") != (s_topic or ""): return True
        if bool(getattr(tgt, "nsfw", False)) != bool(getattr(src, "nsfw", False)): return True
        trl=getattr(tgt, "slowmode_delay", 0) or getattr(tgt, "rate_limit_per_user", 0)
        srl=getattr(src, "slowmode_delay", 0) or getattr(src, "rate_limit_per_user", 0)
        if int(trl or 0) != int(srl or 0): return True
        t_parent = tgt.category.name if tgt.category else None
        s_parent = src.category.name if src.category else None
        if t_parent != s_parent: return True
        for a in ("bitrate", "user_limit"):
            if getattr(tgt, a, None) != getattr(src, a, None): return True
        if isinstance(tgt, discord.ForumChannel) and isinstance(src, discord.ForumChannel):
            if getattr(tgt, "default_thread_slowmode_delay", 0) != getattr(src, "default_thread_slowmode_delay", 0): return True
            if getattr(tgt, "default_sort_order", None) != getattr(src, "default_sort_order", None): return True
            if getattr(tgt, "default_layout", None) != getattr(src, "default_layout", None): return True
            if (getattr(getattr(tgt, "default_reaction_emoji", None), "name", None)
                != getattr(getattr(src, "default_reaction_emoji", None), "name", None)): return True
            t_tags = {_norm(t.name) for t in getattr(tgt, "available_tags", [])}
            s_tags = {_norm(t.name) for t in getattr(src, "available_tags", [])}
            if t_tags != s_tags: return True
    except:
        return True
    return False

def _diff_overwrites_roles(src_overwrites, tgt_overwrites, role_map) -> Optional[Dict[discord.Role, discord.PermissionOverwrite]]:
    """
    Build a minimal overwrites mapping for roles only if any difference exists.
    Returns None if no change is needed.
    """
    def to_pair(po: discord.PermissionOverwrite):
        a, d = po.pair()
        return (a.value, d.value)

    want: Dict[discord.Role, discord.PermissionOverwrite] = {}
    changed = False

    # Build desired from source roles mapped into target roles
    for obj, po in src_overwrites.items():
        if not isinstance(obj, discord.Role): 
            continue
        tgt_role = role_map.get(obj.id)
        if not tgt_role:
            continue
        a, d = po.pair()
        want[tgt_role] = discord.PermissionOverwrite.from_pair(discord.Permissions(a.value), discord.Permissions(d.value))

    # Determine if any role overwrite differs
    for role, desired_po in want.items():
        current_po = tgt_overwrites.get(role)
        if current_po is None or to_pair(current_po) != to_pair(desired_po):
            changed = True
            break

    # Also if there are extra role overwrites on target that source doesn’t have
    src_role_targets = set(want.keys())
    tgt_role_targets = {r for r in tgt_overwrites if isinstance(r, discord.Role)}
    if tgt_role_targets - src_role_targets:
        changed = True

    return want if changed else None

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
    sync_threads: bool=False
    # NEW/CHANGED:
    mode: str="auto"                       # auto|clone|update|sync|dry (display only for 'dry')
    mirror_overwrites: bool=True           # False => preserve target perms (sync mode)
    role_actions: List[RoleAction]=field(default_factory=list)
    channel_actions: List[ChannelAction]=field(default_factory=list)
    role_id_map: Dict[int,int]=field(default_factory=dict)
    cat_id_map: Dict[int,int]=field(default_factory=dict)
    chan_id_map: Dict[int,int]=field(default_factory=dict)
    warnings: List[str]=field(default_factory=list)
    control_msg: Optional[discord.Message]=None

# ---------- confirmation view ----------
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
        if plan and plan.control_msg:
            try:
                cancelled = discord.Embed(title="❎ Sync Cancelled", color=discord.Color.red())
                await plan.control_msg.edit(embed=cancelled, view=None)
            except Exception:
                pass
        self.stop()

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
            await inter.channel.send(content or None, embed=embed)
        except Exception:
            pass
    except Exception:
        pass

# ---------- the cog ----------
class RevampSync(commands.Cog):
    """
    Fast/minimal-logging sync of roles, categories/channels, and (optionally) threads.
    - Never moves ADMIN; only ensures it exists and the bot has it.
    - Aggressively avoids no-op API calls.
    - Modes:
        • auto  — if target is "empty", do a full clone; otherwise do update
        • clone — copy everything from source (first-time bring-up)
        • update— create/update/delete as planned (your previous behavior)
        • sync  — only propagate changes from source; PRESERVE target channel overwrites
        • dry   — just show the plan UI (Confirm still applies)
    """

    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self.pending: Dict[str, Plan]={}
        self.rate_delay=0.20
        self.progress_every=30
        self.progress_min_secs=5.0

    # [p]revamp <target> [mode] [include_deletes] [lockdown] [sync_threads]
    @commands.hybrid_group(name="revamp", invoke_without_command=True)
    @commands.guild_only()
    async def revamp_group(self, ctx: commands.Context,
                           target_guild_id: Optional[int]=None,
                           mode: str="auto",
                           include_deletes: Optional[bool]=False,
                           lockdown: Optional[bool]=True,
                           sync_threads: Optional[bool]=False):
        if target_guild_id:
            await self.revamp_sync.callback(self, ctx, ctx.guild.id, int(target_guild_id),
                                            mode, include_deletes, lockdown, sync_threads)  # type: ignore
        else:
            await ctx.send_help()

    @revamp_group.command(name="sync", with_app_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def revamp_sync(self, ctx: commands.Context,
                          source_guild_id: int, target_guild_id: int,
                          mode: str="auto",                         # NEW default
                          include_deletes: Optional[bool]=False,
                          lockdown: Optional[bool]=True,
                          sync_threads: Optional[bool]=False):
        source=self.bot.get_guild(source_guild_id); target=self.bot.get_guild(target_guild_id)
        if not source or not target: return await ctx.send("I must be in both guilds.")
        if not (source.me.guild_permissions.administrator and target.me.guild_permissions.administrator):
            return await ctx.send("I need **Administrator** in both guilds.")

        # Normalize mode
        mode = (_norm(mode) or "auto")
        if mode not in {"auto","clone","update","sync","dry"}:
            mode = "auto"

        await ctx.typing()
        plan=await self._build_plan(source, target, bool(include_deletes), ctx.author, bool(lockdown), bool(sync_threads), mode)
        self.pending[plan.key]=plan

        ctrl=self._control_embed(plan, mode, "Ready")
        view=ConfirmView(self, plan.key)
        plan.control_msg=await ctx.send(embed=ctrl, view=view)

        async def expire():
            await asyncio.sleep(900)
            if self.pending.pop(plan.key, None):
                try:
                    if plan.control_msg:
                        await plan.control_msg.edit(
                            embed=discord.Embed(title="⏲️ Sync request expired", color=discord.Color.orange()),
                            view=None
                        )
                except: pass
        self.bot.loop.create_task(expire())

    # ---------- NEW: empty target detection ----------
    async def _is_empty_guild(self, g: discord.Guild) -> bool:
        roles = [r for r in await g.fetch_roles() if not r.managed and r.id != g.default_role.id]
        chans = await g.fetch_channels()
        cats = [c for c in chans if isinstance(c, discord.CategoryChannel)]
        noncats = [c for c in chans if not isinstance(c, discord.CategoryChannel)]
        return (len(roles) == 0) and (len(cats) == 0) and (len(noncats) == 0)

    # ---------- planning ----------
    async def _build_plan(self, source: discord.Guild, target: discord.Guild, include_deletes: bool,
                          requested_by: discord.abc.User, lockdown: bool, sync_threads: bool,
                          mode: str) -> Plan:
        key=f"{source.id}:{target.id}:{int(time.time())}"

        # Decide final mode if auto
        mirror_overwrites = True
        final_mode = mode
        if mode == "auto":
            if await self._is_empty_guild(target):
                final_mode = "clone"
            else:
                final_mode = "update"
        if final_mode == "sync":
            mirror_overwrites = False  # PRESERVE target perms

        plan=Plan(key, source, target, requested_by, include_deletes, lockdown, sync_threads,
                  mode=final_mode, mirror_overwrites=mirror_overwrites)

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

        if include_deletes and plan.mode in {"update","sync"}:
            # In clone we don't need delete; in sync we keep deletes optional
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

        if include_deletes and plan.mode in {"update","sync"}:
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

        if include_deletes and plan.mode in {"update","sync"}:
            for tc in tgt_non:
                parent=tc.category.name if tc.category else "__ROOT__"
                kk=f"{parent}|{tc.type}|{tc.name}"
                if not any(kk==key_of(s) for s in src_non):
                    plan.channel_actions.append(ChannelAction("channel","delete", id=tc.id, name=tc.name, type=tc.type))

        return plan

    # ---------- PRETTY embeds ----------
    def _control_embed(self, p: Plan, mode: str, step: str="Ready") -> discord.Embed:
        rc=sum(1 for a in p.role_actions if a.kind=="create")
        ru=sum(1 for a in p.role_actions if a.kind=="update")
        rd=sum(1 for a in p.role_actions if a.kind=="delete")
        cc=sum(1 for c in p.channel_actions if c.op=="create")
        cu=sum(1 for c in p.channel_actions if c.op=="update")
        cd=sum(1 for c in p.channel_actions if c.op=="delete")

        emb=discord.Embed(
            title="✨ Revamp → Main",
            color=discord.Color.blurple(),
            description=(
                f"**Source:** `{discord.utils.escape_markdown(p.source.name)}`\n"
                f"**Target:** `{discord.utils.escape_markdown(p.target.name)}`"
            ),
        )
        mode_label= p.mode.upper() if p.mode!="dry" else "DRY (Preview)"
        emb.add_field(name="Mode", value=f"`{mode_label}`", inline=True)
        emb.add_field(name="Deletes", value=("`Yes`" if p.include_deletes else "`No`"), inline=True)
        emb.add_field(name="Lockdown", value=("`Yes`" if p.lockdown else "`No`"), inline=True)
        emb.add_field(name="Threads", value=("`Yes`" if p.sync_threads else "`No`"), inline=True)
        emb.add_field(name="Perms Strategy", value=("`Mirror`" if p.mirror_overwrites else "`Preserve`"), inline=True)

        emb.add_field(name="👤 Roles", value=f"Create **{rc}** • Update **{ru}** • Delete **{rd}**", inline=False)
        emb.add_field(name="📺 Channels", value=f"Create **{cc}** • Update **{cu}** • Delete **{cd}**", inline=False)

        # Helpful summary of "what will happen"
        bullets = []
        if p.mode=="clone":
            bullets.append("Copy **all roles** (ordered under ADMIN) and **all categories/channels**.")
            bullets.append("Apply **role overwrites** to channels.")
        elif p.mode=="sync":
            bullets.append("Apply new/changed **roles/channels** from Source.")
            bullets.append("**Preserve** target channel permissions (no overwrite edits).")
            if p.include_deletes: bullets.append("Delete roles/channels not in Source.")
        else:  # update
            bullets.append("Create/Update roles & channels to match Source.")
            if p.include_deletes: bullets.append("Delete items not in Source.")

        emb.add_field(name="Plan", value="• " + "\n• ".join(bullets), inline=False)

        if step:
            emb.add_field(name="Status", value=f"{_icon(step.lower())} **{step}**", inline=False)

        if p.warnings:
            preview="\n".join(f"• {w}" for w in p.warnings[:5])
            if len(p.warnings)>5: preview+=f"\n… +{len(p.warnings)-5} more"
            emb.add_field(name="Notes", value=preview, inline=False)

        emb.set_footer(text="Press Confirm Apply to start. ADMIN is never moved by this cog.")
        emb.timestamp=discord.utils.utcnow()
        return emb

    def _progress_embed(self, p: Plan, mode: str, results: dict, step: str) -> discord.Embed:
        emb=self._control_embed(p, mode, step)

        # tiny progress bar
        total_roles = sum(results[k] for k in ("role_create","role_update","role_delete"))
        total_chans = sum(results[k] for k in ("chan_create","chan_update","chan_delete"))
        # not exact target counts, but still informative
        done = total_roles + total_chans
        target = max(done, len(p.role_actions)+len(p.channel_actions))
        slots = 14
        filled = int((done/target)*slots) if target else 0
        bar = "█"*filled + "░"*(slots-filled)

        emb.add_field(
            name="Progress",
            value=(
                f"`{bar}`  {done}/{target}\n"
                f"Roles — C:{results['role_create']} U:{results['role_update']} D:{results['role_delete']}\n"
                f"Chans — C:{results['chan_create']} U:{results['chan_update']} D:{results['chan_delete']}"
            ),
            inline=False
        )
        return emb

    # ---------- ADMIN anchor (never moved) ----------
    def _bot_top_position(self, guild: discord.Guild) -> int:
        me = guild.me
        return max((r.position for r in (me.roles if me else [])), default=0)

    async def _ensure_admin_anchor(self, plan: Plan) -> discord.Role:
        """Ensure ADMIN exists and the bot has it. Never move ADMIN; only report if not top."""
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

        # Report (do not move)
        top_pos = len(guild.roles) - 1
        if admin_role.position != top_pos:
            plan.warnings.append(
                f"ADMIN not at very top (pos {admin_role.position} < {top_pos}). "
                f"This cog will NOT move it."
            )
        return admin_role

    # ---------- apply ----------
    async def apply_plan(self, p: Plan) -> discord.Embed:
        s, t = p.source, p.target
        mode_label = p.mode  # for embed wording
        results={"role_create":0,"role_update":0,"role_delete":0,"chan_create":0,"chan_update":0,"chan_delete":0}

        last_edit=time.monotonic(); ops=0
        async def progress(step_text:str):
            nonlocal last_edit, ops
            now=time.monotonic()
            if (ops >= self.progress_every) or ((now - last_edit) >= self.progress_min_secs):
                if p.control_msg:
                    try: await p.control_msg.edit(embed=self._progress_embed(p, mode_label, results, step_text))
                    except: pass
                last_edit=now; ops=0

        async def do(coro, note:str, critical:bool=False):
            nonlocal ops
            retries, delay=3, 0.8
            while True:
                try:
                    out=await coro
                    await asyncio.sleep(self.rate_delay); ops+=1
                    return out
                except discord.Forbidden as e:
                    msg=f"{note}: Forbidden ({e})"; p.warnings.append(msg)
                    if critical: raise
                    return None
                except discord.HTTPException as e:
                    if retries<=0:
                        msg=f"{note}: {e}"; p.warnings.append(msg)
                        if critical: raise
                        return None
                    await asyncio.sleep(delay); retries-=1; delay=min(delay*2, 4.0)
                except Exception as e:
                    msg=f"{note}: {e}"; p.warnings.append(msg)
                    if critical: raise
                    return None

        # start
        if p.control_msg:
            try: await p.control_msg.edit(embed=self._control_embed(p, mode_label, "Starting"))
            except: pass

        # lockdown
        if p.lockdown:
            await self._toggle_lock(t, enable=True, plan=p)
            await progress("Locked")

        # ADMIN role (never moved)
        await self._ensure_admin_anchor(p)

        bot_top = self._bot_top_position(t)

        # ---- roles ----
        for a in p.role_actions:
            if a.kind=="create" and a.data:
                created=await do(t.create_role(
                    name=a.data["name"], colour=discord.Colour(a.data["color"]), hoist=a.data["hoist"],
                    mentionable=a.data["mentionable"], permissions=discord.Permissions(a.data["permissions"]),
                    reason="Revamp sync: create role"
                ), f"Create role {a.data.get('name')}", critical=True)
                if created:
                    # seed at position 1 if needed (no-op if already 1)
                    if created.position != 1:
                        try:
                            await created.edit(position=1, reason="Revamp sync: seed under ADMIN")
                            await asyncio.sleep(self.rate_delay); ops+=1
                        except Exception: pass
                    results["role_create"]+=1
                    await progress("Roles")

            elif a.kind=="update" and a.id and a.changes:
                tgt=t.get_role(a.id)
                if not tgt: continue
                if tgt.position >= bot_top:
                    p.warnings.append(f"Skip update for {tgt.name!r}: at/above bot's highest role (pos {tgt.position} ≥ {bot_top})")
                    continue

                # Only send fields that actually change
                kwargs={}
                if "color" in a.changes:
                    newcol=a.changes["color"]["to"]
                    if tgt.colour.value != newcol:
                        kwargs["colour"]=discord.Colour(newcol)
                if "hoist" in a.changes and tgt.hoist != a.changes["hoist"]["to"]:
                    kwargs["hoist"]=a.changes["hoist"]["to"]
                if "mentionable" in a.changes and tgt.mentionable != a.changes["mentionable"]["to"]:
                    kwargs["mentionable"]=a.changes["mentionable"]["to"]
                if "permissions" in a.changes and tgt.permissions.value != a.changes["permissions"]["to"]:
                    kwargs["permissions"]=discord.Permissions(a.changes["permissions"]["to"])

                if kwargs:
                    ok=await do(tgt.edit(reason="Revamp sync: update role", **kwargs), f"Update role {tgt.name}")
                    if ok is not None:
                        results["role_update"]+=1
                        await progress("Roles")

            elif a.kind=="delete" and a.id:
                tgt=t.get_role(a.id)
                if tgt:
                    if tgt.position >= bot_top:
                        p.warnings.append(f"Skip delete for {tgt.name!r}: at/above bot's highest role (pos {tgt.position} ≥ {bot_top})")
                        continue
                    if any(_norm(sr.name)==_norm(tgt.name) for sr in (await s.fetch_roles())):
                        continue
                    ok=await do(tgt.delete(reason="Revamp sync: remove role not in source"), f"Delete role {tgt.name}")
                    if ok is not None:
                        results["role_delete"]+=1
                        await progress("Roles")

        # refresh mapping by name (after any creates)
        fresh=await t.fetch_roles()
        by_name={_norm(r.name):r for r in fresh if not r.managed}
        for sr in (await s.fetch_roles()):
            if sr.id==s.default_role.id or sr.managed: continue
            tr=by_name.get(_norm(sr.name))
            if tr: p.role_id_map[sr.id]=tr.id

        # reorder roles strictly under ADMIN (ADMIN not moved)
        await self._reorder_roles_like_source(p)
        await progress("Roles")

        # ---- channels: deletes first (optional) ----
        if p.include_deletes and p.mode in {"update","sync"}:
            all_t=await t.fetch_channels()
            for ch in sorted([c for c in all_t if not isinstance(c, discord.CategoryChannel)], key=lambda c:c.position, reverse=True):
                if any(x.op=="delete" and x.id==ch.id for x in p.channel_actions):
                    ok=await do(ch.delete(reason="Revamp sync: cleanup channel"), f"Delete channel {ch.name}")
                    if ok is not None:
                        results["chan_delete"]+=1; await progress("Channels")
            for cat in sorted([c for c in all_t if isinstance(c, discord.CategoryChannel)], key=lambda c:c.position, reverse=True):
                if any(x.op=="delete" and x.id==cat.id for x in p.channel_actions):
                    ok=await do(cat.delete(reason="Revamp sync: cleanup category"), f"Delete category {cat.name}")
                    if ok is not None:
                        results["chan_delete"]+=1; await progress("Channels")

        # ensure categories exist/order
        src_cats=[c for c in await s.fetch_channels() if isinstance(c, discord.CategoryChannel)]
        tgt_cats=[c for c in await t.fetch_channels() if isinstance(c, discord.CategoryChannel)]
        by_name_cat={c.name:c for c in tgt_cats}
        for cat in sorted(src_cats, key=lambda c:c.position):
            ex=by_name_cat.get(cat.name)
            if not ex:
                created=await do(t.create_category(name=cat.name, reason="Revamp sync: create category"), f"Create category {cat.name}")
                if created:
                    p.cat_id_map[cat.id]=created.id; results["chan_create"]+=1; await progress("Categories")
            else:
                p.cat_id_map[cat.id]=ex.id
                if ex.position!=cat.position:
                    await do(ex.edit(position=cat.position), f"Reorder category {cat.name}")
                    await progress("Categories")

        # create/update non-category channels w/ fine-grained diffs
        src_non=[c for c in await s.fetch_channels() if not isinstance(c, discord.CategoryChannel)]
        tgt_non=[c for c in await t.fetch_channels() if not isinstance(c, discord.CategoryChannel)]

        def find_match(src):
            for c in tgt_non:
                if c.name==src.name and c.type==src.type and ((c.category.name if c.category else None)==(src.category.name if src.category else None)):
                    return c

        created_map={}
        for src_ch in sorted(src_non, key=lambda c:c.position):
            ex=find_match(src_ch)
            parent_id=p.cat_id_map.get(getattr(src_ch,"category_id",None)) if getattr(src_ch,"category_id",None) else None
            parent_obj=t.get_channel(parent_id) if parent_id else None

            if not ex:
                # create
                if src_ch.type is discord.ChannelType.text:
                    ch=await do(t.create_text_channel(
                        name=src_ch.name, category=parent_obj,
                        topic=getattr(src_ch,"topic",None), nsfw=getattr(src_ch,"nsfw",False),
                        slowmode_delay=getattr(src_ch,"slowmode_delay",0) or getattr(src_ch,"rate_limit_per_user",0),
                        reason="Revamp sync: create channel"
                    ), f"Create text {src_ch.name}")
                elif src_ch.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                    ch=await do(t.create_voice_channel(
                        name=src_ch.name, category=parent_obj,
                        bitrate=getattr(src_ch,"bitrate",None), user_limit=getattr(src_ch,"user_limit",None),
                        reason="Revamp sync: create channel"
                    ), f"Create voice {src_ch.name}")
                elif src_ch.type is discord.ChannelType.forum:
                    fkw = {
                        "category": parent_obj,
                        "nsfw": getattr(src_ch,"nsfw",False),
                        "default_layout": getattr(src_ch,"default_layout", None),
                        "default_sort_order": getattr(src_ch,"default_sort_order", None),
                        "default_reaction_emoji": getattr(src_ch,"default_reaction_emoji", None),
                        "default_thread_slowmode_delay": getattr(src_ch,"default_thread_slowmode_delay", 0),
                        "reason": "Revamp sync: create forum",
                    }
                    src_tags = getattr(src_ch, "available_tags", [])
                    if src_tags:
                        fkw["available_tags"] = [discord.ForumTag(name=tag.name) for tag in src_tags]
                    kwargs = {k:v for k,v in fkw.items() if v is not None}
                    ch=await do(t.create_forum(name=src_ch.name, **kwargs), f"Create forum {src_ch.name}")
                else:
                    ch=await do(t.create_text_channel(name=src_ch.name, category=parent_obj,
                                                      reason="Revamp sync: create channel (fallback)"),
                                f"Create channel {src_ch.name}")
                if ch:
                    created_map[src_ch.id]=ch; results["chan_create"]+=1; await progress("Channels")
            else:
                # fine-grained update; include only diffs
                kwargs={"reason":"Revamp sync: update channel"}
                if hasattr(ex,"category"):
                    if (ex.category.id if ex.category else None) != (parent_obj.id if parent_obj else None):
                        kwargs["category"]=parent_obj
                if hasattr(ex,"topic"):
                    src_topic=getattr(src_ch,"topic",None); 
                    if (ex.topic or None) != (src_topic or None):
                        kwargs["topic"]=src_topic
                if hasattr(ex,"nsfw"):
                    src_nsfw=bool(getattr(src_ch,"nsfw",False))
                    if bool(getattr(ex,"nsfw",False)) != src_nsfw:
                        kwargs["nsfw"]=src_nsfw
                if hasattr(ex,"slowmode_delay"):
                    src_sd=getattr(src_ch,"slowmode_delay",0) or getattr(src_ch,"rate_limit_per_user",0)
                    if int(getattr(ex,"slowmode_delay",0) or getattr(ex,"rate_limit_per_user",0)) != int(src_sd or 0):
                        kwargs["slowmode_delay"]=src_sd
                if hasattr(ex,"bitrate"):
                    if getattr(ex,"bitrate",None) != getattr(src_ch,"bitrate",None):
                        kwargs["bitrate"]=getattr(src_ch,"bitrate",None)
                if hasattr(ex,"user_limit"):
                    if getattr(ex,"user_limit",None) != getattr(src_ch,"user_limit",None):
                        kwargs["user_limit"]=getattr(src_ch,"user_limit",None)
                if isinstance(ex, discord.ForumChannel) and isinstance(src_ch, discord.ForumChannel):
                    dl = getattr(src_ch,"default_layout", None)
                    if getattr(ex,"default_layout", None) != dl:
                        kwargs["default_layout"]=dl
                    ds = getattr(src_ch,"default_sort_order", None)
                    if getattr(ex,"default_sort_order", None) != ds:
                        kwargs["default_sort_order"]=ds
                    dre = getattr(src_ch,"default_reaction_emoji", None)
                    if getattr(ex,"default_reaction_emoji", None) != dre:
                        kwargs["default_reaction_emoji"] = dre
                    dts = getattr(src_ch,"default_thread_slowmode_delay", 0)
                    if getattr(ex,"default_thread_slowmode_delay", 0) != dts:
                        kwargs["default_thread_slowmode_delay"]=dts
                    src_tags = getattr(src_ch, "available_tags", [])
                    tgt_tags = getattr(ex, "available_tags", [])
                    if { _norm(t.name) for t in src_tags } != { _norm(t.name) for t in tgt_tags }:
                        kwargs["available_tags"] = [discord.ForumTag(name=tag.name) for tag in src_tags]

                # apply only if there is any change
                edit_payload = {k:v for k,v in kwargs.items() if k!="reason"}
                if any(k for k in edit_payload):
                    ok=await do(ex.edit(**edit_payload, reason=kwargs.get("reason")), f"Update channel {src_ch.name}")
                    if ok is not None:
                        results["chan_update"]+=1; await progress("Channels")

                # position change only if different
                if getattr(ex, "position", None) is not None and ex.position != src_ch.position:
                    ok=await do(ex.edit(position=src_ch.position), f"Reorder channel {src_ch.name}")
                    if ok is not None:
                        results["chan_update"]+=1; await progress("Channels")

                p.chan_id_map[src_ch.id]=ex.id
                created_map[src_ch.id]=ex

        # ---- overwrites (roles only)
        # CLONE/UPDATE => mirror role overwrites
        # SYNC (preserve) => skip this section
        if p.mirror_overwrites:
            role_map_full: Dict[int, discord.Role] = {sid: t.get_role(tid) for sid, tid in p.role_id_map.items() if t.get_role(tid)}
            for src_ch in src_non:
                tgt_ch = created_map.get(src_ch.id)
                if not tgt_ch:
                    continue
                desired = _diff_overwrites_roles(src_ch.overwrites, tgt_ch.overwrites, role_map_full)
                if desired is not None:
                    ok=await do(tgt_ch.edit(overwrites=desired, reason="Revamp sync: mirror role overwrites"),
                                f"Overwrites in {getattr(src_ch,'name','?')}")
                    if ok is not None:
                        results["chan_update"]+=1; await progress("Permissions")

        # ---- Threads (optional) ----
        if p.sync_threads:
            await self._sync_threads(p, src_non, created_map)
            await progress("Threads")

        # unlock
        if p.lockdown:
            await self._toggle_lock(t, enable=False, plan=p)
            await progress("Unlocked")

        # done
        final=discord.Embed(title="✅ Revamp → Main — Completed", color=discord.Color.green())
        final.add_field(name="Mode", value=p.mode.upper(), inline=True)
        final.add_field(name="Perms", value=("Mirror" if p.mirror_overwrites else "Preserve"), inline=True)
        final.add_field(name="Deletes", value=("Yes" if p.include_deletes else "No"), inline=True)
        final.add_field(name="👤 Roles", value=f"C **{results['role_create']}** • U **{results['role_update']}** • D **{results['role_delete']}**", inline=False)
        final.add_field(name="📺 Channels", value=f"C **{results['chan_create']}** • U **{results['chan_update']}** • D **{results['chan_delete']}**", inline=False)
        if p.warnings:
            preview="\n".join(f"• {w}" for w in p.warnings[:8])
            if len(p.warnings)>8: preview+=f"\n… +{len(p.warnings)-8} more"
            final.add_field(name="Notes", value=preview, inline=False)
        final.timestamp=discord.utils.utcnow()
        return final

    # ---- threads helper (unchanged behavior; optional) ----
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
                    p.warnings.append(f"Could not create thread '{th.name}' in {getattr(tgt_parent,'name','?')}: {e}")

    # ---------- lockdown ----------
    async def _toggle_lock(self, guild: discord.Guild, enable: bool, plan: Plan):
        everyone=guild.default_role
        for ch in [c for c in await guild.fetch_channels() if isinstance(c, discord.TextChannel)]:
            try:
                current=ch.overwrites_for(everyone)
                if enable:
                    new=current; new.send_messages=False; new.add_reactions=False
                    await ch.set_permissions(everyone, overwrite=new, reason="Revamp sync: lockdown")
                else:
                    await ch.set_permissions(everyone, overwrite=current, reason="Revamp sync: unlock")
                await asyncio.sleep(self.rate_delay)
            except discord.Forbidden as e:
                plan.warnings.append(f"Lockdown change forbidden in #{ch.name}: {e}")
            except Exception as e:
                plan.warnings.append(f"Lockdown change failed in #{ch.name}: {e}")

    # ---------- role ordering (between @everyone and ADMIN only) ----------
    async def _reorder_roles_like_source(self, p: Plan) -> None:
        """
        Reorder roles bottom→top to match source, strictly between:
          @everyone (pos 0) < [SYMBOLIC ROLES] < ADMIN (not moved)
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
            for sr in src_sorted:
                tr = target.get_role(p.role_id_map.get(sr.id, 0))
                if not tr:
                    continue
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
                    except discord.HTTPException as e:
                        p.warnings.append(f"Failed moving role {role.name}: {e}")
                pos += 1
        except Exception as e:
            p.warnings.append(f"Role reordering issue: {e}")
