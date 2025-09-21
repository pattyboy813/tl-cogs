from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Callable, Awaitable, Deque
from collections import deque
import datetime

import discord
from redbot.core import commands, Config, checks

__red_end_user_data_statement__ = (
    "This cog stores server IDs and the user ID of the last operator in config. "
    "No message content is stored."
)

# ------------------------
# Data structures
# ------------------------

@dataclass
class RoleDiff:
    create: List[str]
    delete: List[str]
    update: List[str]


@dataclass
class ChannelPlan:
    create: List[str]
    delete: List[str]
    update: List[str]


@dataclass
class DiffSummary:
    roles: RoleDiff
    categories: ChannelPlan
    text: ChannelPlan
    voice: ChannelPlan

    @property
    def total_create(self) -> int:
        return (
            len(self.roles.create)
            + len(self.categories.create)
            + len(self.text.create)
            + len(self.voice.create)
        )

    @property
    def total_update(self) -> int:
        return (
            len(self.roles.update)
            + len(self.categories.update)
            + len(self.text.update)
            + len(self.voice.update)
        )

    @property
    def total_delete(self) -> int:
        return (
            len(self.roles.delete)
            + len(self.categories.delete)
            + len(self.text.delete)
            + len(self.voice.delete)
        )


# ------------------------
# Helpers
# ------------------------

def _role_key(r: discord.Role) -> str:
    return r.name.lower()


def _cat_key(c: discord.CategoryChannel) -> str:
    return c.name.lower()


def _chan_key(ch: discord.abc.GuildChannel) -> Tuple[str, str]:
    if isinstance(ch, discord.TextChannel):
        kind = "text"
    elif isinstance(ch, discord.VoiceChannel):
        kind = "voice"
    elif isinstance(ch, discord.CategoryChannel):
        kind = "category"
    else:
        t = getattr(ch, "type", None)
        kind = getattr(t, "name", str(t))
    return (ch.name.lower(), kind)

def _now() -> str:
    return datetime.datetime.utcnow().strftime("%H:%M:%S")


# ------------------------
# Status board (single rolling embed)
# ------------------------

class StatusBoard:
    """
    One message that we keep editing. Debounced to avoid rate limits.
    """
    def __init__(self, channel: discord.abc.Messageable, title: str, *, flush_interval: float = 1.2, max_lines: int = 20):
        self.channel = channel
        self.title = title
        self.flush_interval = flush_interval
        self.max_lines = max_lines
        self._lines: Deque[str] = deque(maxlen=max_lines)
        self._task: Optional[asyncio.Task] = None
        self._pending = asyncio.Event()
        self._running = False
        self.message: Optional[discord.Message] = None
        self._colour = discord.Colour.blurple()
        self._footer = "Live status • updates are batched"
        self._phase = "Preview"

    async def start(self):
        if self._running:
            return
        self._running = True
        emb = discord.Embed(title=self.title, colour=self._colour, description="*…*")
        emb.set_footer(text=self._footer)
        self.message = await self.channel.send(embed=emb)
        self._task = asyncio.create_task(self._worker())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def phase(self, phase: str, *, colour: discord.Colour):
        self._phase = phase
        self._colour = colour
        self._pending.set()

    def note(self, line: str):
        self._lines.append(f"`{_now()}` {line}")
        self._pending.set()

    async def finish(self, *, success: bool, extra: Optional[str] = None):
        self.phase("Complete ✅" if success else "Failed ❌", colour=(discord.Colour.green() if success else discord.Colour.red()))
        if extra:
            self.note(extra)
        # force one last flush
        self._pending.set()
        await asyncio.sleep(0.05)
        self.stop()
        # remove view if any
        try:
            if self.message:
                await self.message.edit(view=None)
        except Exception:
            pass

    async def _worker(self):
        try:
            while self._running:
                try:
                    await asyncio.wait_for(self._pending.wait(), timeout=self.flush_interval)
                except asyncio.TimeoutError:
                    pass
                self._pending.clear()
                if not self.message:
                    continue
                desc = "\n".join(self._lines) or "*…*"
                emb = discord.Embed(
                    title=f"{self.title} — {self._phase}",
                    colour=self._colour,
                    description=desc[:4000],
                )
                emb.set_footer(text=self._footer)
                try:
                    await self.message.edit(embed=emb)
                except Exception:
                    # swallow to avoid loops if perms change mid-run
                    pass
        except asyncio.CancelledError:
            return


# ------------------------
# UI — Confirm view
# ------------------------

class ConfirmView(discord.ui.View):
    def __init__(self, cog: "TheRevamper", ctx: commands.Context, plan: DiffSummary):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.plan = plan
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass

    @discord.ui.button(label="Proceed", style=discord.ButtonStyle.green, emoji="✅")
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message(
                "Only the command invoker can confirm this run.", ephemeral=True
            )
        await interaction.response.defer(thinking=True)
        await self.cog._apply_plan_with_transaction(interaction, self.plan)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="🛑")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message(
                "Only the command invoker can cancel.", ephemeral=True
            )
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


# ------------------------
# Cog
# ------------------------

class TheRevamper(commands.Cog):
    """
    Sync selected structure from a **revamp** (source) guild to your **main** (target) guild, with
    **permission overwrite** syncing and **transactional rollback** if anything fails.

    - Single rolling status embed (colour by phase) to avoid spam/rate limits.
    - Overwrite sync guard: skip if >100 entries (Discord hard limit).
    """

    default_guild = {
        "source_guild_id": None,  # revamp guild
        "target_guild_id": None,  # main guild
        "prune": False,
        "sync_overwrites": True,  # default True per request
        "last_operator": None,
        "transactional": True,
    }

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA18F0D55, force_registration=True)
        self.config.register_global()
        self.config.register_guild(**self.default_guild)

    # ------------------------
    # Utilities
    # ------------------------
    async def _get_guilds(self, ctx: commands.Context) -> Tuple[discord.Guild, discord.Guild]:
        data = await self.config.guild(ctx.guild).all()
        sid = data.get("source_guild_id")
        tid = data.get("target_guild_id")
        if not sid or not tid:
            raise commands.UserFeedbackCheckFailure(
                "Please set both source and target guild IDs with `[p]TheRevamper set guilds <source_id> <target_id>`."
            )
        src = self.bot.get_guild(int(sid))
        tgt = self.bot.get_guild(int(tid))
        if not src or not tgt:
            raise commands.UserFeedbackCheckFailure("Bot must be in both guilds.")
        return src, tgt

    # ------------------------
    # Diff logic
    # ------------------------
    def _diff_roles(self, src: discord.Guild, tgt: discord.Guild) -> RoleDiff:
        src_roles = { _role_key(r): r for r in src.roles if not r.is_default() }
        tgt_roles = { _role_key(r): r for r in tgt.roles if not r.is_default() }

        create = [r.name for k, r in src_roles.items() if k not in tgt_roles]
        delete = [r.name for k, r in tgt_roles.items() if k not in src_roles]
        update = []
        for k, sr in src_roles.items():
            tr = tgt_roles.get(k)
            if not tr:
                continue
            if (
                sr.colour != tr.colour
                or sr.hoist != tr.hoist
                or sr.mentionable != tr.mentionable
                or sr.permissions.value != tr.permissions.value
            ):
                update.append(sr.name)
        return RoleDiff(create=create, delete=delete, update=update)

    def _split_channels(self, g: discord.Guild):
        cats = [c for c in g.categories]
        texts = [c for c in g.text_channels]
        voices = [c for c in g.voice_channels]
        return cats, texts, voices

    def _plan_channels(self, src: discord.Guild, tgt: discord.Guild) -> Tuple[ChannelPlan, ChannelPlan, ChannelPlan]:
        scats, stxt, svoc = self._split_channels(src)
        tcats, ttxt, tvoc = self._split_channels(tgt)

        # Categories
        scmap = { _cat_key(c): c for c in scats }
        tcmap = { _cat_key(c): c for c in tcats }
        cat_create = [c.name for k, c in scmap.items() if k not in tcmap]
        cat_delete = [c.name for k, c in tcmap.items() if k not in scmap]
        cat_update = []
        for k, sc in scmap.items():
            if k in tcmap and sc.position != tcmap[k].position:
                cat_update.append(sc.name)
        cat_plan = ChannelPlan(cat_create, cat_delete, cat_update)

        def plan_for(ch_src: List[discord.abc.GuildChannel], ch_tgt: List[discord.abc.GuildChannel]):
            sdict = { _chan_key(c): c for c in ch_src }
            tdict = { _chan_key(c): c for c in ch_tgt }
            to_create = [c.name for k, c in sdict.items() if k not in tdict]
            to_delete = [c.name for k, c in tdict.items() if k not in sdict]
            to_update = []
            for k, sc in sdict.items():
                tc = tdict.get(k)
                if not tc:
                    continue
                differs = False
                if isinstance(sc, discord.TextChannel) and isinstance(tc, discord.TextChannel):
                    if (
                        sc.topic != tc.topic
                        or sc.is_nsfw() != tc.is_nsfw()
                        or sc.slowmode_delay != tc.slowmode_delay
                        or (sc.category and sc.category.name) != (tc.category.name if tc.category else None)
                        or sc.position != tc.position
                        or sc.overwrites != tc.overwrites
                    ):
                        differs = True
                elif isinstance(sc, discord.VoiceChannel) and isinstance(tc, discord.VoiceChannel):
                    if (
                        sc.bitrate != tc.bitrate
                        or sc.user_limit != tc.user_limit
                        or sc.is_nsfw() != tc.is_nsfw()
                        or (sc.category and sc.category.name) != (tc.category.name if tc.category else None)
                        or sc.position != tc.position
                        or sc.overwrites != tc.overwrites
                    ):
                        differs = True
                if differs:
                    to_update.append(sc.name)
            return ChannelPlan(to_create, to_delete, to_update)

        text_plan = plan_for(stxt, ttxt)
        voice_plan = plan_for(svoc, tvoc)
        return cat_plan, text_plan, voice_plan

    def _build_summary(self, src: discord.Guild, tgt: discord.Guild) -> DiffSummary:
        roles = self._diff_roles(src, tgt)
        cats, text, voice = self._plan_channels(src, tgt)
        return DiffSummary(roles=roles, categories=cats, text=text, voice=voice)

    # ------------------------
    # Transaction machinery
    # ------------------------
    class Txn:
        def __init__(self):
            self.undo: List[Callable[[], Awaitable[None]]] = []

        def add(self, fn: Callable[[], Awaitable[None]]):
            self.undo.append(fn)

        async def rollback(self, progress: Callable[[str], Awaitable[None]]):
            await progress("⚠️ Error occurred — rolling back…")
            for action in reversed(self.undo):
                try:
                    await action()
                except Exception:
                    pass
            await progress("↩️ Rollback complete.")

    # ------------------------
    # Overwrite guard
    # ------------------------
    @staticmethod
    def _safe_overwrites(ow: Dict) -> Optional[Dict]:
        """
        Discord refuses >100 permission_overwrites on a channel/category.
        Return None if safe to skip (too many), else return mapping.
        """
        try:
            if ow and len(ow) > 100:
                return None
        except Exception:
            pass
        return ow

    # ------------------------
    # Apply changes with rollback
    # ------------------------
    async def _apply_roles(self, src: discord.Guild, tgt: discord.Guild, *, prune: bool, txn: "TheRevamper.Txn", board: StatusBoard):
        src_roles = [r for r in src.roles if not r.is_default()]
        tgt_roles = [r for r in tgt.roles if not r.is_default()]
        sdict = { _role_key(r): r for r in src_roles }
        tdict = { _role_key(r): r for r in tgt_roles }

        # Create/update
        for srole in src_roles:
            tr = tdict.get(_role_key(srole))
            if not tr:
                board.note(f"Creating role **{srole.name}**…")
                newr = await tgt.create_role(
                    name=srole.name,
                    colour=srole.colour,
                    hoist=srole.hoist,
                    mentionable=srole.mentionable,
                    permissions=srole.permissions,
                    reason="TheRevamper: create role to match source",
                )
                txn.add(lambda r=newr: r.delete(reason="TheRevamper rollback: remove created role"))
            else:
                before = dict(
                    colour=tr.colour,
                    hoist=tr.hoist,
                    mentionable=tr.mentionable,
                    permissions=tr.permissions,
                )
                if (
                    srole.colour != tr.colour
                    or srole.hoist != tr.hoist
                    or srole.mentionable != tr.mentionable
                    or srole.permissions.value != tr.permissions.value
                ):
                    board.note(f"Updating role **{srole.name}**…")
                    await tr.edit(
                        colour=srole.colour,
                        hoist=srole.hoist,
                        mentionable=srole.mentionable,
                        permissions=srole.permissions,
                        reason="TheRevamper: update role to match source",
                    )
                    txn.add(lambda r=tr, b=before: r.edit(**b, reason="TheRevamper rollback: restore role"))

        # Reorder to match source order
        role_positions = { tdict.get(_role_key(r), None): idx for idx, r in enumerate(src_roles, start=1) if tdict.get(_role_key(r)) }
        if role_positions:
            board.note("Reordering roles…")
            snap_positions = {r: r.position for r in role_positions.keys() if r}
            await tgt.edit_role_positions({r: pos for r, pos in role_positions.items() if r})
            async def undo_positions():
                await tgt.edit_role_positions(snap_positions)
            txn.add(undo_positions)

        # Prune
        if prune:
            for tname, trole in list(tdict.items()):
                if tname not in sdict:
                    board.note(f"Deleting role **{trole.name}**…")
                    attrs = dict(
                        name=trole.name,
                        colour=trole.colour,
                        hoist=trole.hoist,
                        mentionable=trole.mentionable,
                        permissions=trole.permissions,
                        position=trole.position,
                    )
                    await trole.delete(reason="TheRevamper: prune missing role")
                    async def recreate(a=attrs, guild=tgt):
                        newr = await guild.create_role(
                            name=a["name"], colour=a["colour"], hoist=a["hoist"], mentionable=a["mentionable"], permissions=a["permissions"],
                            reason="TheRevamper rollback: recreate pruned role",
                        )
                        try:
                            await guild.edit_role_positions({newr: a["position"]})
                        except Exception:
                            pass
                    txn.add(recreate)

    async def _get_or_create_category(self, tgt: discord.Guild, name: Optional[str], *, txn: "TheRevamper.Txn", board: StatusBoard) -> Optional[discord.CategoryChannel]:
        if name is None:
            return None
        existing = discord.utils.get(tgt.categories, name=name)
        if existing:
            return existing
        board.note(f"Creating category **{name}**…")
        cat = await tgt.create_category(name=name, reason="TheRevamper: create missing category")
        txn.add(lambda c=cat: c.delete(reason="TheRevamper rollback: remove created category"))
        return cat

    async def _apply_channels(self, src: discord.Guild, tgt: discord.Guild, *, prune: bool, sync_overwrites: bool, txn: "TheRevamper.Txn", board: StatusBoard):
        # Categories first
        scats = src.categories
        tcats = tgt.categories
        for sc in scats:
            tc = discord.utils.get(tcats, name=sc.name)
            if not tc:
                board.note(f"Creating category **{sc.name}**…")
                tc = await tgt.create_category(sc.name, reason="TheRevamper: add category")
                txn.add(lambda c=tc: c.delete(reason="TheRevamper rollback: remove created category"))
            before = dict(position=tc.position, overwrites=tc.overwrites)
            edits: Dict = {}
            if tc.position != sc.position:
                edits["position"] = sc.position
            if sync_overwrites and tc.overwrites != sc.overwrites:
                safe = self._safe_overwrites(sc.overwrites)
                if safe is None:
                    board.note(f"Skipping overwrites for category **{sc.name}** (>{100} entries).")
                else:
                    edits["overwrites"] = safe
            if edits:
                board.note(f"Updating category **{sc.name}**…")
                await tc.edit(**edits, reason="TheRevamper: sync category")
                txn.add(lambda c=tc, b=before: c.edit(**b, reason="TheRevamper rollback: restore category"))

        if prune:
            for tc in list(tcats):
                if not discord.utils.get(scats, name=tc.name):
                    board.note(f"Deleting category **{tc.name}**…")
                    snap = dict(name=tc.name, position=tc.position, overwrites=tc.overwrites)
                    await tc.delete(reason="TheRevamper: prune category")
                    async def recreate_cat(g=tgt, s=snap):
                        newc = await g.create_category(s["name"], reason="TheRevamper rollback: recreate category")
                        try:
                            await newc.edit(position=s["position"], overwrites=self._safe_overwrites(s["overwrites"]) or {})
                        except Exception:
                            pass
                    txn.add(recreate_cat)

        # Text and voice
        async def sync_collection(src_channels: List[discord.abc.GuildChannel], is_text: bool):
            tgt_channels = tgt.text_channels if is_text else tgt.voice_channels
            for sc in src_channels:
                tc = discord.utils.get(tgt_channels, name=sc.name)
                tgt_cat = await self._get_or_create_category(tgt, sc.category.name if sc.category else None, txn=txn, board=board)
                if not tc:
                    board.note(f"Creating {'text' if is_text else 'voice'} channel **{sc.name}**…")
                    if is_text:
                        safe = self._safe_overwrites(sc.overwrites) if sync_overwrites else None
                        tc = await tgt.create_text_channel(
                            name=sc.name,
                            category=tgt_cat,
                            topic=sc.topic,
                            nsfw=sc.is_nsfw(),
                            slowmode_delay=sc.slowmode_delay,
                            overwrites=safe,
                            reason="TheRevamper: add text channel",
                        )
                    else:
                        safe = self._safe_overwrites(sc.overwrites) if sync_overwrites else None
                        tc = await tgt.create_voice_channel(
                            name=sc.name,
                            category=tgt_cat,
                            bitrate=sc.bitrate,
                            user_limit=sc.user_limit,
                            overwrites=safe,
                            reason="TheRevamper: add voice channel",
                        )
                    txn.add(lambda c=tc: c.delete(reason="TheRevamper rollback: remove created channel"))
                else:
                    # snapshot + edits
                    if is_text and isinstance(tc, discord.TextChannel) and isinstance(sc, discord.TextChannel):
                        before = dict(
                            category=tc.category,
                            topic=tc.topic,
                            nsfw=tc.is_nsfw(),
                            slowmode_delay=tc.slowmode_delay,
                            overwrites=tc.overwrites,
                            position=tc.position,
                        )
                        edits: Dict = {}
                        if (tc.category and tc.category.name) != (sc.category.name if sc.category else None):
                            edits["category"] = tgt_cat
                        if tc.topic != sc.topic:
                            edits["topic"] = sc.topic
                        if tc.is_nsfw() != sc.is_nsfw():
                            edits["nsfw"] = sc.is_nsfw()
                        if tc.slowmode_delay != sc.slowmode_delay:
                            edits["slowmode_delay"] = sc.slowmode_delay
                        if sync_overwrites and tc.overwrites != sc.overwrites:
                            safe = self._safe_overwrites(sc.overwrites)
                            if safe is None:
                                board.note(f"Skipping overwrites for **#{sc.name}** (>{100} entries).")
                            else:
                                edits["overwrites"] = safe
                        if edits:
                            board.note(f"Updating text channel **{sc.name}**…")
                            await tc.edit(**edits, reason="TheRevamper: sync text props")
                            txn.add(lambda c=tc, b=before: c.edit(**b, reason="TheRevamper rollback: restore text"))
                        if tc.position != sc.position:
                            snap_pos = tc.position
                            try:
                                await tc.edit(position=sc.position, reason="TheRevamper: reorder")
                                txn.add(lambda c=tc, p=snap_pos: c.edit(position=p, reason="TheRevamper rollback: restore order"))
                            except discord.HTTPException:
                                pass

                    if not is_text and isinstance(tc, discord.VoiceChannel) and isinstance(sc, discord.VoiceChannel):
                        before = dict(
                            category=tc.category,
                            bitrate=tc.bitrate,
                            user_limit=tc.user_limit,
                            nsfw=tc.is_nsfw(),
                            overwrites=tc.overwrites,
                            position=tc.position,
                        )
                        edits: Dict = {}
                        if (tc.category and tc.category.name) != (sc.category.name if sc.category else None):
                            edits["category"] = tgt_cat
                        if tc.bitrate != sc.bitrate:
                            edits["bitrate"] = sc.bitrate
                        if tc.user_limit != sc.user_limit:
                            edits["user_limit"] = sc.user_limit
                        if tc.is_nsfw() != sc.is_nsfw():
                            edits["nsfw"] = sc.is_nsfw()
                        if sync_overwrites and tc.overwrites != sc.overwrites:
                            safe = self._safe_overwrites(sc.overwrites)
                            if safe is None:
                                board.note(f"Skipping overwrites for **🔊 {sc.name}** (>{100} entries).")
                            else:
                                edits["overwrites"] = safe
                        if edits:
                            board.note(f"Updating voice channel **{sc.name}**…")
                            await tc.edit(**edits, reason="TheRevamper: sync voice props")
                            txn.add(lambda c=tc, b=before: c.edit(**b, reason="TheRevamper rollback: restore voice"))
                        if tc.position != sc.position:
                            snap_pos = tc.position
                            try:
                                await tc.edit(position=sc.position, reason="TheRevamper: reorder")
                                txn.add(lambda c=tc, p=snap_pos: c.edit(position=p, reason="TheRevamper rollback: restore order"))
                            except discord.HTTPException:
                                pass

            # Prune
            if prune:
                for tc in list(tgt_channels):
                    if not discord.utils.get(src_channels, name=tc.name):
                        board.note(f"Deleting channel **{tc.name}**…")
                        if isinstance(tc, discord.TextChannel):
                            snap = dict(
                                kind="text",
                                name=tc.name,
                                category=tc.category.name if tc.category else None,
                                topic=tc.topic,
                                nsfw=tc.is_nsfw(),
                                slowmode_delay=tc.slowmode_delay,
                                overwrites=tc.overwrites,
                                position=tc.position,
                            )
                        else:
                            snap = dict(
                                kind="voice",
                                name=tc.name,
                                category=tc.category.name if tc.category else None,
                                bitrate=getattr(tc, "bitrate", None),
                                user_limit=getattr(tc, "user_limit", None),
                                nsfw=tc.is_nsfw(),
                                overwrites=tc.overwrites,
                                position=tc.position,
                            )
                        await tc.delete(reason="TheRevamper: prune channel")
                        async def recreate_channel(g=tgt, s=snap):
                            cat = discord.utils.get(g.categories, name=s["category"]) if s["category"] else None
                            if s["kind"] == "text":
                                newc = await g.create_text_channel(
                                    name=s["name"], category=cat, topic=s["topic"], nsfw=s["nsfw"],
                                    slowmode_delay=s["slowmode_delay"],
                                    overwrites=self._safe_overwrites(s["overwrites"]) or {},
                                    reason="TheRevamper rollback: recreate pruned text",
                                )
                            else:
                                newc = await g.create_voice_channel(
                                    name=s["name"], category=cat, bitrate=s["bitrate"], user_limit=s["user_limit"],
                                    overwrites=self._safe_overwrites(s["overwrites"]) or {},
                                    reason="TheRevamper rollback: recreate pruned voice",
                                )
                            try:
                                await newc.edit(position=s["position"])
                            except Exception:
                                pass
                        txn.add(recreate_channel)

        await sync_collection(src.text_channels, True)
        await sync_collection(src.voice_channels, False)

    async def _apply_plan_with_transaction(self, interaction: discord.Interaction, plan: DiffSummary):
        assert interaction.guild
        data = await self.config.guild(interaction.guild).all()
        prune = bool(data.get("prune"))
        sync_overwrites = bool(data.get("sync_overwrites"))
        transactional = bool(data.get("transactional", True))
        sid = int(data.get("source_guild_id"))
        tid = int(data.get("target_guild_id"))
        src = self.bot.get_guild(sid)
        tgt = self.bot.get_guild(tid)
        if not src or not tgt:
            return await interaction.edit_original_response(content="Guilds not found; is the bot in both servers?", view=None)

        # Single rolling embed
        board = StatusBoard(interaction.channel, title="TheRevamper", flush_interval=1.2, max_lines=24)  # type: ignore
        await board.start()
        board.phase("Running", colour=discord.Colour.blurple())
        board.note(f"From **{src.name}** → **{tgt.name}**")
        board.note("Starting roles…")

        txn = self.Txn()
        ok = True
        try:
            await self._apply_roles(src, tgt, prune=prune, txn=txn, board=board)
            board.note("Roles complete. Starting categories/channels…")
            await self._apply_channels(src, tgt, prune=prune, sync_overwrites=sync_overwrites, txn=txn, board=board)
        except discord.Forbidden:
            ok = False
            if transactional:
                await txn.rollback(lambda s: (board.note(s)))
            board.note("❌ I don't have sufficient permissions. Ensure I have Manage Roles/Channels.")
        except Exception as e:
            ok = False
            if transactional:
                await txn.rollback(lambda s: (board.note(s)))
            board.note(f"❌ Error: {e!r}")

        if ok:
            board.note(f"✅ Sync complete. Created **{plan.total_create}**, updated **{plan.total_update}**, deleted **{plan.total_delete}**.")
            await board.finish(success=True)
        else:
            await board.finish(success=False)

        # Replace the original "thinking" response with a compact summary
        summary = discord.Embed(
            title="TheRevamper — Complete" if ok else "TheRevamper — Failed",
            colour=discord.Colour.green() if ok else discord.Colour.red(),
        )
        summary.add_field(name="Source", value=f"{src.name} ({src.id})", inline=True)
        summary.add_field(name="Target", value=f"{tgt.name} ({tgt.id})", inline=True)
        summary.add_field(name="Created", value=str(plan.total_create), inline=True)
        summary.add_field(name="Updated", value=str(plan.total_update), inline=True)
        summary.add_field(name="Deleted", value=str(plan.total_delete), inline=True)
        await interaction.edit_original_response(embed=summary, view=None)

    # ------------------------
    # Commands
    # ------------------------
    @commands.group(name="TheRevamper")
    @checks.admin_or_permissions(manage_guild=True)
    async def therevamper(self, ctx: commands.Context):
        """Configure and run the server revamp sync (source -> main)."""
        pass

    @therevamper.command(name="set")
    async def therevamper_set(self, ctx: commands.Context, sub: str, *args):
        """Settings:
        - guilds <source_id> <target_id>
        - prune <true|false>
        - overwrites <true|false>
        - transactional <true|false>
        """
        sub = sub.lower()
        if sub == "guilds":
            if len(args) != 2:
                return await ctx.send("Usage: [p]TheRevamper set guilds <source_id> <target_id>")
            sid, tid = args
            await self.config.guild(ctx.guild).source_guild_id.set(int(sid))
            await self.config.guild(ctx.guild).target_guild_id.set(int(tid))
            await self.config.guild(ctx.guild).last_operator.set(ctx.author.id)
            await ctx.send(f"Saved guilds. Source=`{sid}`, Target=`{tid}`")
        elif sub == "prune":
            if not args:
                return await ctx.send("Usage: [p]TheRevamper set prune <true|false>")
            val = args[0].lower() in {"1", "true", "t", "yes", "y"}
            await self.config.guild(ctx.guild).prune.set(val)
            await ctx.send(f"Prune missing items is now **{val}**")
        elif sub == "overwrites":
            if not args:
                return await ctx.send("Usage: [p]TheRevamper set overwrites <true|false>")
            val = args[0].lower() in {"1", "true", "t", "yes", "y"}
            await self.config.guild(ctx.guild).sync_overwrites.set(val)
            await ctx.send(f"Sync permission overwrites is now **{val}**")
        elif sub == "transactional":
            if not args:
                return await ctx.send("Usage: [p]TheRevamper set transactional <true|false>")
            val = args[0].lower() in {"1", "true", "t", "yes", "y"}
            await self.config.guild(ctx.guild).transactional.set(val)
            await ctx.send(f"Transactional rollback is now **{val}**")
        else:
            await ctx.send("Unknown setting. See `[p]help TheRevamper set`.")

    @therevamper.command(name="preview")
    async def TheRevamper_preview(self, ctx: commands.Context):
        """Show a live-updating embed with planned changes and a confirmation UI."""
        src, tgt = await self._get_guilds(ctx)
        plan = self._build_summary(src, tgt)

        emb = discord.Embed(title="TheRevamper — Preview", colour=discord.Colour.gold())
        emb.add_field(name="Source (revamp)", value=f"{src.name} ({src.id})", inline=True)
        emb.add_field(name="Target (main)", value=f"{tgt.name} ({tgt.id})", inline=True)
        emb.add_field(
            name="Roles",
            value=(f"Create: **{len(plan.roles.create)}**\nUpdate: **{len(plan.roles.update)}**\nDelete: **{len(plan.roles.delete)}**"),
            inline=True,
        )
        emb.add_field(
            name="Categories",
            value=(f"Create: **{len(plan.categories.create)}**\nUpdate: **{len(plan.categories.update)}**\nDelete: **{len(plan.categories.delete)}**"),
            inline=True,
        )
        emb.add_field(
            name="Text Channels",
            value=(f"Create: **{len(plan.text.create)}**\nUpdate: **{len(plan.text.update)}**\nDelete: **{len(plan.text.delete)}**"),
            inline=True,
        )
        emb.add_field(
            name="Voice Channels",
            value=(f"Create: **{len(plan.voice.create)}**\nUpdate: **{len(plan.voice.update)}**\nDelete: **{len(plan.voice.delete)}**"),
            inline=True,
        )
        emb.add_field(
            name="Permissions",
            value=("Overwrites **ON**" if (await self.config.guild(ctx.guild).sync_overwrites()) else "Overwrites **OFF**"),
            inline=False,
        )
        emb.set_footer(text="Proceed to run with rollback and a single rolling status embed.")

        view = ConfirmView(self, ctx, plan)
        msg = await ctx.send(embed=emb, view=view)
        view.message = msg

    @therevamper.command(name="run")
    async def TheRevamper_run(self, ctx: commands.Context):
        """Shortcut: generate preview and immediately show the Proceed/Cancel UI."""
        await self.TheRevamper_preview(ctx)
