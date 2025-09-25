# brawlstarsmanager.py
# Red-DiscordBot cog: Brawl Stars self-service onboarding & club placement
# v0.7.2 — CamelCase, role-gated admin, richer lobby, themed embeds, channel.type fix, JSON import/export
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
from io import BytesIO

import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

log = logging.getLogger("red.brawlstars_manager")

API_BASE = "https://api.brawlstars.com/v1"
MAX_CLUB_MEMBERS = 50
THREAD_AUTO_ARCHIVE_M = 1440  # 24h
NICK_PREFIX_STRIP = re.compile(r"^(TLG\s+)", re.IGNORECASE)

# ---------- visual theme & helpers ----------
THEME = {
    "primary": 0x5865F2,  # blurple
    "success": 0x2ECC71,  # green
    "warning": 0xF1C40F,  # yellow
    "error":   0xE74C3C,  # red
    "neutral": 0x2B2D31,  # dark
    "accent":  0x00B8FF,  # cyan
}

EMOJI = {
    "spark": "✨",
    "ok": "✅",
    "warn": "⚠️",
    "err": "❌",
    "door": "🚪",
    "shield": "🛡️",
    "gear": "⚙️",
    "chat": "💬",
    "question": "❓",
    "club": "🏆",
}

def FmtNum(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)

def GuildIcon(guild: Optional[discord.Guild]) -> Optional[str]:
    try:
        return guild.icon.url if guild and guild.icon else None
    except Exception:
        return None

def Emb(
    title: str,
    desc: str | None = None,
    *,
    color: int | None = None,
    kind: str = "primary",
    guild: Optional[discord.Guild] = None,
    footer: str | None = None,
    thumbnail_url: str | None = None,
    image_url: str | None = None,
) -> discord.Embed:
    if color is None:
        color = THEME.get(kind, THEME["neutral"])
    e = discord.Embed(title=title, description=desc or discord.Embed.Empty, color=color)
    if guild:
        icon = GuildIcon(guild)
        if icon:
            e.set_author(name=guild.name, icon_url=icon)
        else:
            e.set_author(name=guild.name)
    footer_text = footer or "Brawl Stars Manager • v0.7.2"
    e.set_footer(text=footer_text)
    if thumbnail_url:
        e.set_thumbnail(url=thumbnail_url)
    if image_url:
        e.set_image(url=image_url)
    return e

# ----------------- helpers -----------------
def NormTag(tag: str) -> str:
    """Normalize a BS tag (strip #, uppercase, O->0)."""
    return tag.strip().lstrip("#").upper().replace("O", "0")

def EncTag(tag: str) -> str:
    return urllib.parse.quote(f"#{NormTag(tag)}", safe="")

def ExtractId(value: Any) -> Optional[int]:
    """Accept raw int, '123', '<@&123>', '<#123>', 'role 123' etc."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"\d{5,}", value)
        if m:
            try:
                return int(m.group(0))
            except Exception:
                return None
    return None

class BSAPI:
    """Tiny BS API client with basic retry handling (CamelCase)."""

    def __init__(self, session: aiohttp.ClientSession, apiKeyGetter):
        self.session = session
        self._get_key = apiKeyGetter  # async callable

    async def _request(self, method: str, path: str) -> Dict[str, Any]:
        key = await self._get_key()
        if not key:
            raise RuntimeError("Brawl Stars API key not set. Use [p]bs apikey set <token>.")
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Red-Cog-BS-Manager/0.7.2",
        }
        url = f"{API_BASE}{path}"
        for attempt in range(3):
            async with self.session.request(method, url, headers=headers) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"message": text}
                if 200 <= resp.status < 300:
                    return data
                if resp.status == 429:
                    retry = float(resp.headers.get("retry-after", "1.5"))
                    await asyncio.sleep(retry)
                    continue
                if resp.status in (500, 502, 503, 504):
                    await asyncio.sleep(1 + attempt)
                    continue
                raise RuntimeError(f"BS API error {resp.status}: {data.get('message')}")
        raise RuntimeError("BS API request failed after retries.")

    async def Player(self, tag: str) -> Dict[str, Any]:
        return await self._request("GET", f"/players/{EncTag(tag)}")

    async def Club(self, tag: str) -> Dict[str, Any]:
        return await self._request("GET", f"/clubs/{EncTag(tag)}")

@dataclass
class ClubConfig:
    tag: str                 # "#ABCDEFG"
    role_id: int             # role to assign on success (club role)
    chat_channel_id: int     # where to ping them when done
    closed: bool = False     # admin override to exclude
    # legacy: retained so old configs load; not used for eligibility
    min_trophies: Optional[int] = None

    @classmethod
    def FromDict(cls, d: Dict[str, Any]) -> "ClubConfig":
        return cls(
            tag=d["tag"],
            role_id=int(d["role_id"]),
            chat_channel_id=int(d["chat_channel_id"]),
            closed=bool(d.get("closed", False)),
            min_trophies=(int(d["min_trophies"]) if "min_trophies" in d and d["min_trophies"] is not None else None),
        )

    def ToDict(self) -> Dict[str, Any]:
        out = {
            "tag": self.tag,
            "role_id": self.role_id,
            "chat_channel_id": self.chat_channel_id,
            "closed": self.closed,
        }
        if self.min_trophies is not None:
            out["min_trophies"] = self.min_trophies
        return out

# ----------------- UI components -----------------
class StartView(discord.ui.View):
    """Persistent 'Get Started' + 'How it works' in lobby."""

    def __init__(self, cog: "BrawlStarsManager"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Get Started", style=discord.ButtonStyle.primary, custom_id="bs:start")
    async def Start(self, interaction: discord.Interaction, button: discord.ui.Button):
        saved = await self.cog.GetUserTag(interaction.user)
        if saved:
            await self.cog.BeginFlowWithTag(interaction, saved)
        else:
            await interaction.response.send_modal(TagModal(self.cog))

    @discord.ui.button(label="How it works", style=discord.ButtonStyle.secondary, custom_id="bs:how")
    async def How(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message(embed=Emb("No guild", "Please use this in a server.", kind="warning"), ephemeral=True)
            return
        pages = await self.cog.config.guild(interaction.guild).lobby_guide_pages()
        if not pages:
            await interaction.response.send_message(
                embed=Emb("No guide yet", "Ask an admin to add guide pages with `[p]bs lobbycfg guide add <image_url>`.", kind="warning"),
                ephemeral=True,
            )
            return
        view = HowItWorksPager(pages)
        await interaction.response.send_message(embed=view.PageEmbed(), view=view, ephemeral=True)

class HowItWorksPager(discord.ui.View):
    def __init__(self, pages: List[Dict[str, str]]):
        super().__init__(timeout=300)
        self.pages = pages
        self.i = 0

    def PageEmbed(self) -> discord.Embed:
        p = self.pages[self.i]
        em = Emb(p.get("title") or f"Step {self.i+1}", p.get("desc") or "", kind="neutral")
        if p.get("image_url"):
            em.set_image(url=p["image_url"])
        return em

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def Prev(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.i = (self.i - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.PageEmbed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def Next(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.i = (self.i + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.PageEmbed(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def Close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Guide closed.", embed=None, view=None)

class TagModal(discord.ui.Modal, title="Enter your Brawl Stars Tag"):
    tag_input = discord.ui.TextInput(
        label="Your tag (with or without #)",
        placeholder="#ABC123",
        max_length=15,
        required=True,
    )

    def __init__(self, cog: "BrawlStarsManager"):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.BeginFlowWithTag(interaction, str(self.tag_input.value))

class ClubSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], cog: "BrawlStarsManager", thread_id: int, applicant_id: int):
        super().__init__(placeholder="Choose a club", min_values=1, max_values=1, options=options, custom_id="bs:clubselect")
        self.cog = cog
        self.thread_id = thread_id
        self.applicant_id = applicant_id

    async def callback(self, interaction: discord.Interaction):  # must be named `callback`
        if interaction.user.id != self.applicant_id:
            await interaction.response.send_message(embed=Emb("Not for you", "Only the applicant can select a club.", kind="warning"), ephemeral=True)
            return
        chosen_tag = self.values[0]
        await self.cog.OnClubSelected(interaction, self.thread_id, self.applicant_id, chosen_tag)

class ClubSelectView(discord.ui.View):
    def __init__(self, cog: "BrawlStarsManager", options: List[discord.SelectOption], thread_id: int, applicant_id: int):
        super().__init__(timeout=600)
        self.add_item(ClubSelect(options, cog, thread_id, applicant_id))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="bs:cancel")
    async def Cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=Emb("Cancelled", "You can close this thread.", kind="neutral"), ephemeral=True)

class ProvideLinkButton(discord.ui.View):
    def __init__(self, cog: "BrawlStarsManager", thread_id: int, member_id: int, club_tag: str):
        super().__init__(timeout=1800)
        self.cog = cog
        self.thread_id = thread_id
        self.member_id = member_id
        self.club_tag = club_tag

    @discord.ui.button(label="Provide Club Invite Link", style=discord.ButtonStyle.primary, custom_id="bs:lead:link")
    async def Provide(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LinkModal(self.cog, self.thread_id, self.member_id, self.club_tag))

class LinkModal(discord.ui.Modal, title="Paste the club invite link"):
    link = discord.ui.TextInput(
        label="Invite Link",
        placeholder="https://link.brawlstars.com/invite/....",
        required=True,
        max_length=200,
    )

    def __init__(self, cog: "BrawlStarsManager", thread_id: int, member_id: int, club_tag: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.thread_id = thread_id
        self.member_id = member_id
        self.club_tag = club_tag

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.HandleLeaderLink(interaction, self.thread_id, self.member_id, self.club_tag, str(self.link.value))

class JoinedView(discord.ui.View):
    def __init__(self, cog: "BrawlStarsManager", club_tag: str, member_id: int):
        super().__init__(timeout=3600)
        self.cog = cog
        self.club_tag = club_tag
        self.member_id = member_id

    @discord.ui.button(label="I've joined the club", style=discord.ButtonStyle.success, custom_id="bs:joined")
    async def Joined(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.HandleJoined(interaction, self.club_tag, self.member_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="bs:joined_cancel")
    async def Cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=Emb("Cancelled", "You can close this thread.", kind="neutral"), ephemeral=True)

# -------- Interactive "club add" wizard components --------
class ClubAddTagModal(discord.ui.Modal, title="Club Tag"):
    tag_input = discord.ui.TextInput(
        label="Club Tag",
        placeholder="#ABC123",
        max_length=15,
        required=True,
    )
    def __init__(self, wizard: "ClubAddWizard"):
        super().__init__(timeout=300)
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.wizard.SetTag(interaction, str(self.tag_input.value))

class RolePicker(discord.ui.RoleSelect):
    def __init__(self, wizard: "ClubAddWizard"):
        super().__init__(placeholder="Pick club role", min_values=1, max_values=1)
        self.wizard = wizard

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        await self.wizard.SetRole(interaction, role)

class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, wizard: "ClubAddWizard"):
        super().__init__(
            placeholder="Pick club chat channel",
            min_values=1,
            max_values=1,
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.forum,
            ],
        )
        self.wizard = wizard

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await self.wizard.SetChannel(interaction, channel)

class ClubAddWizard(discord.ui.View):
    """Interactive stateful wizard for adding a club config."""

    def __init__(self, cog: "BrawlStarsManager", invoker: discord.Member):
        super().__init__(timeout=600)
        self.cog = cog
        self.invoker = invoker
        self.guild = invoker.guild

        self.club_tag: Optional[str] = None
        self.club_name: Optional[str] = None
        self.api_req_trophies: Optional[int] = None
        self.role_id: Optional[int] = None
        self.chat_channel_id: Optional[int] = None

        self.role_picker = RolePicker(self)
        self.channel_picker = ChannelPicker(self)
        self.add_item(self.role_picker)
        self.add_item(self.channel_picker)

    @discord.ui.button(label="Set Club Tag", style=discord.ButtonStyle.primary, row=2)
    async def BtnTag(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._IsInvoker(interaction):
            return
        await interaction.response.send_modal(ClubAddTagModal(self))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def BtnSave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._IsInvoker(interaction):
            return
        if not (self.club_tag and self.role_id and self.chat_channel_id):
            await interaction.response.send_message(
                embed=Emb("Missing info", "Please set **Club Tag**, **Role**, and **Chat Channel**.", kind="warning"),
                ephemeral=True,
            )
            return
        tag_key = self.club_tag.upper()
        c = ClubConfig(
            tag=self.club_tag,
            role_id=self.role_id,
            chat_channel_id=self.chat_channel_id,
            closed=False,
            min_trophies=None,
        )
        async with self.cog.config.guild(self.guild).clubs() as clubs:
            clubs[tag_key] = c.ToDict()

        role = self.guild.get_role(self.role_id)
        ch = self.guild.get_channel(self.chat_channel_id)
        title = self.club_name or self.club_tag
        req = f"\nAPI req trophies: **{self.api_req_trophies}**" if self.api_req_trophies is not None else ""
        await interaction.response.send_message(
            embed=Emb("Club Saved", f"**{title}** ({self.club_tag}){req}\nRole: {role.mention if role else self.role_id}\nChat: {ch.mention if ch else self.chat_channel_id}", kind="success", guild=self.guild),
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def BtnCancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._IsInvoker(interaction):
            return
        await interaction.response.send_message(embed=Emb("Cancelled", "No changes saved.", kind="neutral"), ephemeral=True)
        self.stop()

    async def SetTag(self, interaction: discord.Interaction, raw_tag: str):
        if not self._IsInvoker(interaction):
            return
        tag = f"#{NormTag(raw_tag)}"
        api = await self.cog.Api(interaction.guild)
        try:
            info = await api.Club(tag)
        except Exception as ex:
            await interaction.response.send_message(embed=Emb("Invalid Club Tag", f"Could not fetch club: {ex}", kind="error"), ephemeral=True)
            return
        self.club_tag = tag
        self.club_name = info.get("name") or tag
        self.api_req_trophies = int(info.get("requiredTrophies") or 0)
        await interaction.response.send_message(
            embed=Emb("Tag Set", f"Club: **{self.club_name}** ({tag})\nAPI req trophies: **{self.api_req_trophies}**", kind="primary", guild=self.guild),
            ephemeral=True,
        )

    async def SetRole(self, interaction: discord.Interaction, role: discord.Role):
        if not self._IsInvoker(interaction):
            return
        self.role_id = role.id
        await interaction.response.send_message(embed=Emb("Role Selected", f"{role.mention}", kind="primary", guild=self.guild), ephemeral=True)

    async def SetChannel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel):
        if not self._IsInvoker(interaction):
            return

        allowed_types = {
            discord.ChannelType.text,
            discord.ChannelType.news,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
            discord.ChannelType.forum,
        }
        ch_type = getattr(channel, "type", None)

        if ch_type not in allowed_types:
            await interaction.response.send_message(
                embed=Emb("Unsupported channel", "Please pick a text channel, announcement channel, thread, or forum.", kind="warning"),
                ephemeral=True,
            )
            return

        self.chat_channel_id = channel.id
        mention = getattr(channel, "mention", str(channel.id))
        await interaction.response.send_message(
            embed=Emb("Chat Channel Selected", f"{mention}", kind="primary", guild=self.guild),
            ephemeral=True,
        )

    def _IsInvoker(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            asyncio.create_task(
                interaction.response.send_message(
                    embed=Emb("Not for you", "Only the command invoker can use this wizard.", kind="warning"),
                    ephemeral=True,
                )
            )
            return False
        return True

# ----------------- Cog -----------------
class BrawlStarsManager(commands.Cog):
    """Self-service Brawl Stars club placement."""

    __author__ = "Pat+Chat"
    __version__ = "0.7.2"

    def __init__(self, bot: Red):
        self.bot: Red = bot
        self.config = Config.get_conf(self, identifier=0xB5A71C09, force_registration=True)
        self.config.register_guild(
            api_key=None,
            lobby_channel_id=None,
            leadership_channel_id=None,
            family_role_id=None,
            clubs={},
            threads={},
            lobby_embed={
                "title": "Club Placement",
                "description": (
                    f"{EMOJI['spark']} Click **Get Started** to begin the self-service flow.\n"
                    "I’ll create a private thread, check your eligibility via the Brawl Stars API, "
                    "and guide you through joining your club.\n\n"
                    "Use **How it works** for a quick photo walkthrough."
                ),
                "image_url": None,
                "thumbnail_url": None,
            },
            lobby_guide_pages=[],
            admin_role_ids=[],
        )
        self.config.register_user(tag=None)
        self._session: Optional[aiohttp.ClientSession] = None

    # -------- lifecycle --------
    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        self.bot.add_view(StartView(self))

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    # -------- admin role guard --------
    async def HasAdmin(self, ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        if ctx.guild is None:
            return False
        if ctx.author == ctx.guild.owner:
            return True
        if getattr(ctx.author.guild_permissions, "manage_guild", False):
            return True
        role_ids: List[int] = await self.config.guild(ctx.guild).admin_role_ids()
        if role_ids:
            user_role_ids = {r.id for r in getattr(ctx.author, "roles", [])}
            if user_role_ids.intersection(set(role_ids)):
                return True
        return False

    async def GuardAdmin(self, ctx: commands.Context) -> bool:
        ok = await self.HasAdmin(ctx)
        if not ok:
            await ctx.send(embed=Emb("Nope", "You don’t have permission to use this command.", kind="error", guild=ctx.guild))
        return ok

    # -------- API helper --------
    async def Api(self, guild: discord.Guild) -> BSAPI:
        async def get_key():
            key = await self.config.guild(guild).api_key()
            if not key:
                key = os.getenv("BRAWLSTARS_API_KEY")
            return key
        return BSAPI(self._session, get_key)

    # -------- lobby embed builder --------
    async def BuildLobbyEmbed(self, guild: discord.Guild) -> discord.Embed:
        cfg = await self.config.guild(guild).lobby_embed()
        clubs_cfg = await self.config.guild(guild).clubs()
        total_clubs = len(clubs_cfg)

        title = cfg.get("title") or "Club Placement"
        description = cfg.get("description") or (
            f"{EMOJI['spark']} Click **Get Started** to begin the self-service flow.\n"
            "I’ll create a private thread, verify your player via the Brawl Stars API, "
            "and guide you through joining the right club."
        )

        e = Emb(title, description, kind="primary", guild=guild)
        thumb = cfg.get("thumbnail_url")
        img = cfg.get("image_url")
        if thumb:
            e.set_thumbnail(url=thumb)
        if img:
            e.set_image(url=img)

        e.add_field(
            name=f"{EMOJI['gear']} What you’ll need",
            value=(
                "• Your **Brawl Stars player tag** (e.g. `#ABC123`)\n"
                "• About **1–2 minutes** to pick a club and join\n"
                "• Ability to paste an **invite link** (leaders will provide it)"
            ),
            inline=False,
        )
        e.add_field(
            name=f"{EMOJI['door']} How it works",
            value=(
                "1) Press **Get Started** → I’ll open a private thread with you\n"
                "2) I check your profile (IGN/trophies) via the official API\n"
                "3) You pick an open, eligible club from a list\n"
                "4) A leader drops an invite → you join\n"
                "5) I **verify** you’re in the club, set your **nickname**, and give you the **roles**"
            ),
            inline=False,
        )
        if total_clubs:
            e.add_field(
                name=f"{EMOJI['club']} Clubs available",
                value=(
                    f"• Configured clubs: **{total_clubs}**\n"
                    "• I only show clubs with spots open and where you meet the API trophy requirements."
                ),
                inline=False,
            )
        e.add_field(
            name=f"{EMOJI['shield']} Privacy & safety",
            value=(
                "• I only store your **saved tag** for quicker onboarding next time\n"
                "• I query **read-only** endpoints from the official Brawl Stars API\n"
                "• Leaders only see what’s needed to get you in fast"
            ),
            inline=False,
        )
        e.add_field(
            name=f"{EMOJI['question']} Need help?",
            value=(
                "• Tap **How it works** for a short photo guide\n"
                f"• Or ping staff in the leadership channel if you get stuck"
            ),
            inline=False,
        )
        return e

    # -------- user tag helpers --------
    async def GetUserTag(self, user: discord.abc.User) -> Optional[str]:
        return await self.config.user(user).tag()

    # -------- flow entry --------
    async def BeginFlowWithTag(self, interaction: discord.Interaction, raw_tag: str):
        if not interaction.guild:
            await interaction.response.send_message(embed=Emb("Use in a server", "Please run this in a server.", kind="warning"), ephemeral=True)
            return

        gconf = self.config.guild(interaction.guild)
        lobby_id = await gconf.lobby_channel_id()
        if lobby_id and interaction.channel_id != lobby_id:
            await interaction.response.send_message(embed=Emb("Wrong channel", "Please use the configured lobby channel.", kind="warning"), ephemeral=True)
            return

        tag = NormTag(raw_tag)
        api = await self.Api(interaction.guild)
        try:
            pdata = await api.Player(tag)
        except Exception as e:
            await interaction.response.send_message(embed=Emb("Couldn’t fetch player", f"Check your tag. {e}", kind="error"), ephemeral=True)
            return

        ign = pdata.get("name", "Unknown")
        trophies = pdata.get("trophies", 0)
        await self.config.user(interaction.user).tag.set(f"#{tag}")

        parent_ch = interaction.channel
        if isinstance(parent_ch, discord.Thread):
            parent_ch = parent_ch.parent
        if not isinstance(parent_ch, discord.TextChannel):
            await interaction.response.send_message(embed=Emb("Channel issue", "Please start from a text channel.", kind="warning"), ephemeral=True)
            return

        thread_name = f"{ign} · #{tag}"
        thread = await parent_ch.create_thread(
            name=thread_name[:90],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=THREAD_AUTO_ARCHIVE_M,
        )
        await thread.add_user(interaction.user)

        async with gconf.threads() as threads:
            threads[str(thread.id)] = {"member_id": interaction.user.id, "tag": tag, "chosen": None}

        await interaction.response.send_message(
            embed=Emb("Thread created", f"I made a private thread for you: {thread.mention}", kind="success", guild=interaction.guild),
            ephemeral=True,
        )

        intro = Emb(
            "Welcome!",
            f"**IGN:** `{ign}` • **Tag:** `#{tag}` • **Trophies:** **{FmtNum(trophies)}**\n\n"
            "I’ll now check which clubs you’re eligible for…",
            kind="neutral",
            guild=interaction.guild,
        )
        await thread.send(content=interaction.user.mention, embed=intro)

        await self.ShowEligibleClubs(thread, interaction.user, tag, trophies)

    # -------- eligibility & selection --------
    async def ShowEligibleClubs(self, thread: discord.Thread, member: discord.Member, tag: str, trophies: int):
        gconf = self.config.guild(thread.guild)
        clubs_raw = await gconf.clubs()
        clubs = [ClubConfig.FromDict(v) for v in clubs_raw.values()]
        api = await self.Api(thread.guild)

        if not clubs:
            await thread.send(embed=Emb("No clubs configured", "An admin can add clubs with `[p]bs club add`", kind="warning", guild=thread.guild))
            return

        candidates: List[Tuple[ClubConfig, Dict[str, Any]]] = []
        for c in clubs:
            if c.closed:
                continue
            try:
                cinfo = await api.Club(c.tag)
            except Exception as e:
                await thread.send(embed=Emb("API error", f"Couldn’t fetch club {c.tag}: {e}", kind="error", guild=thread.guild))
                continue
            req = int(cinfo.get("requiredTrophies") or 0)
            if trophies < req:
                continue
            candidates.append((c, cinfo))

        if not candidates:
            await thread.send(embed=Emb("Not eligible yet", "You don’t meet the trophy requirements for any configured club yet.", kind="warning", guild=thread.guild))
            return

        eligible: List[Tuple[ClubConfig, Dict[str, Any]]] = []
        for c, info in candidates:
            ctype = (info.get("type") or "").lower()
            members = int(info.get("members", 0))
            if ctype == "closed":
                continue
            if members >= MAX_CLUB_MEMBERS:
                continue
            eligible.append((c, info))

        if not eligible:
            await thread.send(embed=Emb("No open spots", "All suitable clubs are currently full or closed. Please check back later.", kind="warning", guild=thread.guild))
            return

        options: List[discord.SelectOption] = []
        for c, info in eligible:
            name = info.get("name", c.tag)
            m = int(info.get("members", 0))
            label = name[:100]
            desc = f"{FmtNum(m)}/{MAX_CLUB_MEMBERS} • req {info.get('requiredTrophies', 0)}"
            options.append(discord.SelectOption(label=label, description=desc, value=c.tag))

        view = ClubSelectView(self, options, thread.id, member.id)
        await thread.send(
            embed=Emb("Eligible Clubs", "Select a club from the dropdown below to proceed.", kind="primary", guild=thread.guild),
            view=view,
        )
        for c, info in eligible:
            await thread.send(embed=self.ClubEmbed(info, thread.guild))

    def ClubEmbed(self, info: Dict[str, Any], guild: Optional[discord.Guild] = None) -> discord.Embed:
        name = info.get("name", "?")
        tag = info.get("tag", "?")
        desc = info.get("description", "") or "No description provided."
        req = int(info.get("requiredTrophies", 0))
        members = int(info.get("members", 0))
        ctype = str(info.get("type", "unknown")).title()
        trophies = int(info.get("trophies", 0))

        em = Emb(f"{name} ({tag})", desc, kind="accent", guild=guild)
        em.add_field(name="Type", value=ctype)
        em.add_field(name="Members", value=f"{FmtNum(members)}/{MAX_CLUB_MEMBERS}")
        em.add_field(name="Req. Trophies", value=FmtNum(req))
        em.add_field(name="Club Trophies", value=FmtNum(trophies), inline=False)
        return em

    async def OnClubSelected(self, interaction: discord.Interaction, thread_id: int, applicant_id: int, chosen_tag: str):
        if not interaction.guild:
            await interaction.response.send_message(embed=Emb("Guild missing", kind="warning"), ephemeral=True)
            return
        if interaction.user.id != applicant_id:
            await interaction.response.send_message(embed=Emb("Not for you", "Only the applicant can select a club.", kind="warning"), ephemeral=True)
            return

        gconf = self.config.guild(interaction.guild)
        async with gconf.threads() as threads:
            t = threads.get(str(thread_id))
            if not t:
                await interaction.response.send_message(embed=Emb("Thread state missing", kind="error"), ephemeral=True)
                return
            t["chosen"] = chosen_tag
            threads[str(thread_id)] = t

        await interaction.response.send_message(embed=Emb("Selected", f"Great—selected **{chosen_tag}**. I’ll notify leadership now.", kind="success", guild=interaction.guild), ephemeral=True)
        await self.NotifyLeadership(interaction.guild, thread_id, applicant_id, chosen_tag)

    # -------- leadership handoff --------
    async def NotifyLeadership(self, guild: discord.Guild, thread_id: int, member_id: int, club_tag: str):
        gconf = self.config.guild(guild)
        lead_id = await gconf.leadership_channel_id()
        if not lead_id:
            return
        channel = guild.get_channel(lead_id)
        if not channel or not isinstance(channel, (discord.TextChannel, getattr(discord, "AnnouncementChannel", discord.TextChannel), discord.Thread)):
            return
        member = guild.get_member(member_id)
        thread = guild.get_thread(thread_id)
        if not member or not thread:
            return

        await channel.send(
            embed=Emb(
                "New applicant needs invite",
                f"Applicant: {member.mention}\nThread: {thread.mention}\nClub: **{club_tag}**\n\n"
                "Click the button below to provide the invite link.",
                kind="primary",
                guild=guild,
            ),
            view=ProvideLinkButton(self, thread.id, member.id, club_tag),
        )

    async def HandleLeaderLink(self, interaction: discord.Interaction, thread_id: int, member_id: int, club_tag: str, url: str):
        if not interaction.guild:
            await interaction.response.send_message(embed=Emb("Guild missing", kind="warning"), ephemeral=True)
            return
        thread = interaction.guild.get_thread(thread_id)
        member = interaction.guild.get_member(member_id)
        if not thread or not member:
            await interaction.response.send_message(embed=Emb("Thread/member not found", kind="error"), ephemeral=True)
            return
        if not url.startswith("http"):
            await interaction.response.send_message(embed=Emb("Invalid link", "Please provide a valid URL.", kind="error"), ephemeral=True)
            return

        await interaction.response.send_message(embed=Emb("Thanks!", "I’ve sent the link to the applicant.", kind="success"), ephemeral=True)

        await thread.send(
            embed=Emb(
                "Your club invite",
                f"{member.mention}, here’s your invite link for **{club_tag}**:\n{url}\n\n"
                "Once you’ve joined, press the button below and I’ll verify and finish up.",
                kind="primary",
                guild=interaction.guild,
            ),
            view=JoinedView(self, club_tag, member.id),
        )

    # -------- verification & finish --------
    async def HandleJoined(self, interaction: discord.Interaction, club_tag: str, member_id: int):
        if not interaction.guild:
            await interaction.response.send_message(embed=Emb("Guild missing", kind="warning"), ephemeral=True)
            return
        if interaction.user.id != member_id:
            await interaction.response.send_message(embed=Emb("Not for you", "Only the applicant can confirm joining.", kind="warning"), ephemeral=True)
            return

        gconf = self.config.guild(interaction.guild)
        async with gconf.threads() as threads:
            t = threads.get(str(interaction.channel_id), {})

        tag = t.get("tag")
        if not tag:
            await interaction.response.send_message(embed=Emb("No tag on file", "Please restart the flow.", kind="error"), ephemeral=True)
            return

        api = await self.Api(interaction.guild)
        try:
            pdata = await api.Player(tag)
        except Exception as e:
            await interaction.response.send_message(embed=Emb("API error", f"Couldn’t verify via API: {e}", kind="error"), ephemeral=True)
            return

        club_now = (pdata.get("club") or {}).get("tag")
        ign = pdata.get("name", "Unknown")
        if not club_now or NormTag(club_now) != NormTag(club_tag):
            await interaction.response.send_message(embed=Emb("Not yet", "I don’t see you in that club yet. Try again in a minute.", kind="warning"), ephemeral=True)
            return

        clubs_raw = await gconf.clubs()
        cconf_raw = clubs_raw.get(club_tag.upper())
        if not cconf_raw:
            await interaction.response.send_message(embed=Emb("Config missing", "Club config missing; contact staff.", kind="error"), ephemeral=True)
            return
        cconf = ClubConfig.FromDict(cconf_raw)

        family_role_id = await gconf.family_role_id()
        family_role = interaction.guild.get_role(family_role_id) if family_role_id else None
        club_role = interaction.guild.get_role(cconf.role_id)
        chat_chan = interaction.guild.get_channel(cconf.chat_channel_id)

        member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)

        try:
            cinfo = await api.Club(club_tag)
            club_name = cinfo.get("name", club_tag)
        except Exception:
            club_name = club_tag
        club_suffix = NICK_PREFIX_STRIP.sub("", club_name).strip()
        new_nick = f"{ign} | {club_suffix}"

        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await member.edit(nick=new_nick)

        roles_to_add = [r for r in [family_role, club_role] if r]
        if roles_to_add:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await member.add_roles(*roles_to_add, reason="Brawl Stars club join verified")

        await interaction.response.send_message(embed=Emb("Verified!", f"Assigned your roles and set nickname to **{new_nick}**.", kind="success"), ephemeral=True)

        if chat_chan:
            try:
                welcome_embed = Emb("Welcome!", f"Welcome {member.mention} to **{club_suffix}**! 🎉", kind="primary", guild=interaction.guild)
                if isinstance(chat_chan, (discord.TextChannel, getattr(discord, "AnnouncementChannel", discord.TextChannel), discord.Thread)):
                    await chat_chan.send(embed=welcome_embed)
                elif isinstance(chat_chan, discord.ForumChannel):
                    await chat_chan.create_thread(
                        name=f"Welcome {member.display_name}",
                        content=f"Welcome {member.mention} to **{club_suffix}**! 🎉",
                        embed=welcome_embed,
                    )
            except Exception:
                pass

        if isinstance(interaction.channel, discord.Thread):
            with contextlib.suppress(Exception):
                await interaction.channel.edit(locked=True, archived=True)

    # ----------------- Commands (admin & user) -----------------
    def HelpEmbed(self, ctx: commands.Context) -> discord.Embed:
        p = ctx.prefix or "[p]"
        em = Emb(
            "Brawl Stars Manager — Help",
            "Self-service onboarding to clubs, leadership handoff, and role assignment.\n"
            "Below are the key commands. Replace `[p]` with your bot prefix.",
            kind="primary",
            guild=ctx.guild,
        )
        em.add_field(
            name="User",
            value=box(
                f"{p}bstag set <tag>      -> Save your Brawl Stars tag (validates via API)\n"
                f"{p}bstag show [user]    -> Show saved tag (self or another member)\n"
                f"{p}bstag clear          -> Clear your saved tag",
                lang="ini"
            ),
            inline=False
        )
        em.add_field(
            name="Admin – Setup",
            value=box(
                f"{p}bs apikey set <token>      -> Set API key (guild)\n"
                f"{p}bs setfamilyrole @Role     -> Family role given to all members\n"
                f"{p}bs setlobby #channel       -> Lobby where Start/How-It-Works is posted\n"
                f"{p}bs setleadership #channel  -> Where leaders receive invite tasks\n"
                f"{p}bs lobby postbutton        -> Post Start & How-It-Works buttons",
                lang="ini"
            ),
            inline=False
        )
        em.add_field(
            name="Admin – Clubs",
            value=box(
                f"{p}bs club add                -> Interactive wizard (tag -> role -> chat)\n"
                f"{p}bs club list               -> List clubs (live API req & capacity)\n"
                f"{p}bs club close <tag> <bool> -> Override closed flag\n"
                f"{p}bs club remove <tag>       -> Remove a club\n"
                f"{p}bs club import [merge|replace] <json or attach> -> Import clubs from JSON\n"
                f"{p}bs club export             -> Export clubs to JSON",
                lang="ini"
            ),
            inline=False
        )
        em.add_field(
            name="Admin – Lobby visuals",
            value=box(
                f"{p}bs lobbycfg settitle <text>\n"
                f"{p}bs lobbycfg setdesc <text>\n"
                f"{p}bs lobbycfg setimage <url>\n"
                f"{p}bs lobbycfg setthumb <url>\n"
                f"{p}bs lobbycfg guide add <url> [Title | Description]\n"
                f"{p}bs lobbycfg guide list / remove <index> / clear",
                lang="ini"
            ),
            inline=False
        )
        em.add_field(
            name="Admin – Role gate",
            value=box(
                f"{p}bs adminroles add @Role    -> Allow this role to use admin commands\n"
                f"{p}bs adminroles remove @Role -> Remove from allowlist\n"
                f"{p}bs adminroles list         -> Show allowed roles\n"
                f"{p}bs adminroles clear        -> Reset allowlist",
                lang="ini"
            ),
            inline=False
        )
        return em

    async def format_help_for_context(self, ctx: commands.Context) -> str:
        return "Brawl Stars Manager: use `[p]bs help` for full, embedded help."

    @commands.group(name="bs", invoke_without_command=True)
    async def Bs(self, ctx: commands.Context):
        await ctx.send(embed=self.HelpEmbed(ctx))

    @Bs.command(name="help")
    async def BsHelp(self, ctx: commands.Context):
        await ctx.send(embed=self.HelpEmbed(ctx))

    # ---- Admin role allowlist
    @Bs.group(name="adminroles", invoke_without_command=True)
    async def BsAdminRoles(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        await ctx.send_help()

    @BsAdminRoles.command(name="add")
    async def BsAdminRolesAdd(self, ctx: commands.Context, role: discord.Role):
        if not await self.GuardAdmin(ctx):
            return
        ids = await self.config.guild(ctx.guild).admin_role_ids()
        if role.id in ids:
            await ctx.send(embed=Emb("Already allowed", f"{role.mention} is already on the list.", kind="warning", guild=ctx.guild))
            return
        ids.append(role.id)
        await self.config.guild(ctx.guild).admin_role_ids.set(ids)
        await ctx.send(embed=Emb("Role allowed", f"{role.mention} can now use admin commands for this cog.", kind="success", guild=ctx.guild))

    @BsAdminRoles.command(name="remove")
    async def BsAdminRolesRemove(self, ctx: commands.Context, role: discord.Role):
        if not await self.GuardAdmin(ctx):
            return
        ids = await self.config.guild(ctx.guild).admin_role_ids()
        if role.id not in ids:
            await ctx.send(embed=Emb("Not on list", f"{role.mention} wasn't on the allowlist.", kind="warning", guild=ctx.guild))
            return
        ids = [i for i in ids if i != role.id]
        await self.config.guild(ctx.guild).admin_role_ids.set(ids)
        await ctx.send(embed=Emb("Role removed", f"{role.mention} removed from allowlist.", kind="success", guild=ctx.guild))

    @BsAdminRoles.command(name="list")
    async def BsAdminRolesList(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        ids = await self.config.guild(ctx.guild).admin_role_ids()
        if not ids:
            await ctx.send(embed=Emb("No roles yet", "Use `bs adminroles add @Role` to allow a role.", kind="neutral", guild=ctx.guild))
            return
        names = []
        for i in ids:
            r = ctx.guild.get_role(i)
            names.append(r.mention if r else f"`{i}`")
        await ctx.send(embed=Emb("Allowed roles", "\n".join(names), kind="primary", guild=ctx.guild))

    @BsAdminRoles.command(name="clear")
    async def BsAdminRolesClear(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.guild(ctx.guild).admin_role_ids.set([])
        await ctx.send(embed=Emb("Cleared", "Admin role allowlist reset.", kind="success", guild=ctx.guild))

    # ---- API key
    @Bs.group(name="apikey", invoke_without_command=True)
    async def BsApiKey(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        key = await self.config.guild(ctx.guild).api_key()
        status = "configured" if key else ("using env var" if os.getenv("BRAWLSTARS_API_KEY") else "not set")
        await ctx.send(embed=Emb("API Key", f"Status: **{status}**", kind="primary", guild=ctx.guild))

    @BsApiKey.command(name="set")
    async def BsApiKeySet(self, ctx: commands.Context, *, token: str):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.guild(ctx.guild).api_key.set(token.strip())
        await ctx.send(embed=Emb("API Key Saved", "Key stored for this guild.", kind="success", guild=ctx.guild))

    @BsApiKey.command(name="clear")
    async def BsApiKeyClear(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.guild(ctx.guild).api_key.clear()
        await ctx.send(embed=Emb("API Key Cleared", "I will try the BRAWLSTARS_API_KEY env var if present.", kind="neutral", guild=ctx.guild))

    # ---- Lobby / leadership / family
    @Bs.command(name="setlobby")
    async def BsSetLobby(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.guild(ctx.guild).lobby_channel_id.set(channel.id)
        await ctx.send(embed=Emb("Lobby Set", f"Lobby channel is now {channel.mention}", kind="success", guild=ctx.guild))

    @Bs.command(name="setleadership")
    async def BsSetLeadership(self, ctx: commands.Context, channel: discord.TextChannel | discord.Thread):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.guild(ctx.guild).leadership_channel_id.set(channel.id)
        await ctx.send(embed=Emb("Leadership Set", f"Leadership channel is now {channel.mention}", kind="success", guild=ctx.guild))

    @Bs.command(name="setfamilyrole")
    async def BsSetFamilyRole(self, ctx: commands.Context, role: discord.Role):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.guild(ctx.guild).family_role_id.set(role.id)
        await ctx.send(embed=Emb("Family Role Set", f"Family role is now {role.mention}", kind="success", guild=ctx.guild))

    # ---- Lobby visuals
    @Bs.group(name="lobbycfg", invoke_without_command=True)
    async def BsLobbyCfg(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        await ctx.send_help()

    @BsLobbyCfg.command(name="settitle")
    async def BsLobbySetTitle(self, ctx: commands.Context, *, title: str):
        if not await self.GuardAdmin(ctx):
            return
        async with self.config.guild(ctx.guild).lobby_embed() as e:
            e["title"] = title
        await ctx.send(embed=Emb("Lobby title set", title, kind="success", guild=ctx.guild))

    @BsLobbyCfg.command(name="setdesc")
    async def BsLobbySetDesc(self, ctx: commands.Context, *, description: str):
        if not await self.GuardAdmin(ctx):
            return
        async with self.config.guild(ctx.guild).lobby_embed() as e:
            e["description"] = description
        await ctx.send(embed=Emb("Lobby description set", description[:2000], kind="success", guild=ctx.guild))

    @BsLobbyCfg.command(name="setimage")
    async def BsLobbySetImage(self, ctx: commands.Context, image_url: str):
        if not await self.GuardAdmin(ctx):
            return
        async with self.config.guild(ctx.guild).lobby_embed() as e:
            e["image_url"] = image_url
        em2 = Emb("Lobby banner set", image_url, kind="success", guild=ctx.guild, image_url=image_url)
        await ctx.send(embed=em2)

    @BsLobbyCfg.command(name="setthumb")
    async def BsLobbySetThumb(self, ctx: commands.Context, image_url: str):
        if not await self.GuardAdmin(ctx):
            return
        async with self.config.guild(ctx.guild).lobby_embed() as e:
            e["thumbnail_url"] = image_url
        em2 = Emb("Lobby thumbnail set", image_url, kind="success", guild=ctx.guild, thumbnail_url=image_url)
        await ctx.send(embed=em2)

    @BsLobbyCfg.group(name="guide", invoke_without_command=True)
    async def BsLobbyGuide(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        await ctx.send_help()

    @BsLobbyCfg.command(name="clear")
    async def BsLobbyGuideClear(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.guild(ctx.guild).lobby_guide_pages.set([])
        await ctx.send(embed=Emb("Guide cleared", "All pages removed.", kind="success", guild=ctx.guild))

    @BsLobbyCfg.command(name="list")
    async def BsLobbyGuideList(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        pages = await self.config.guild(ctx.guild).lobby_guide_pages()
        if not pages:
            await ctx.send(embed=Emb("No pages", "Use `bs lobbycfg guide add <image_url> [title] | [desc]`", kind="neutral", guild=ctx.guild))
            return
        lines = []
        for i, p in enumerate(pages, 1):
            t = p.get("title") or f"Step {i}"
            u = p.get("image_url") or "no image"
            lines.append(f"{i}. {t} — {u}")
        await ctx.send(embed=Emb("Guide pages", "\n".join(lines), kind="primary", guild=ctx.guild))

    @BsLobbyCfg.command(name="add")
    async def BsLobbyGuideAdd(self, ctx: commands.Context, image_url: str, *, text: str = ""):
        if not await self.GuardAdmin(ctx):
            return
        title, desc = None, None
        if "|" in text:
            title, desc = [s.strip() for s in text.split("|", 1)]
        elif text.strip():
            title = text.strip()
        page = {"image_url": image_url, "title": title, "desc": desc}
        pages = await self.config.guild(ctx.guild).lobby_guide_pages()
        pages.append(page)
        await self.config.guild(ctx.guild).lobby_guide_pages.set(pages)
        em2 = Emb(f"Added page {len(pages)}", (title or "") + ("\n" + desc if desc else ""), kind="success", guild=ctx.guild, image_url=image_url)
        await ctx.send(embed=em2)

    @BsLobbyCfg.command(name="remove")
    async def BsLobbyGuideRemove(self, ctx: commands.Context, index: int):
        if not await self.GuardAdmin(ctx):
            return
        pages = await self.config.guild(ctx.guild).lobby_guide_pages()
        if not 1 <= index <= len(pages):
            await ctx.send(embed=Emb("Out of range", f"Valid: 1..{len(pages)}", kind="warning", guild=ctx.guild))
            return
        removed = pages.pop(index - 1)
        await self.config.guild(ctx.guild).lobby_guide_pages.set(pages)
        await ctx.send(embed=Emb("Removed page", removed.get("title") or f"Step {index}", kind="success", guild=ctx.guild))

    # ---- Clubs management
    @Bs.group(name="club", invoke_without_command=True)
    async def BsClub(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        await ctx.send_help()

    @BsClub.command(name="add")
    async def BsClubAdd(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        if not ctx.guild:
            await ctx.send(embed=Emb("Use in a server", kind="warning"))
            return
        view = ClubAddWizard(self, ctx.author)
        await ctx.send(
            embed=Emb(
                "Add a Club (wizard)",
                "Steps:\n1) **Set Club Tag** (validated via API; shows API min trophies)\n2) **Pick Role**\n3) **Pick Chat Channel**\nThen press **Save**.",
                kind="primary",
                guild=ctx.guild,
            ),
            view=view,
        )

    @BsClub.command(name="close")
    async def BsClubClose(self, ctx: commands.Context, tag: str, closed: bool):
        if not await self.GuardAdmin(ctx):
            return
        key = f"#{NormTag(tag)}".upper()
        async with self.config.guild(ctx.guild).clubs() as clubs:
            if key not in clubs:
                await ctx.send(embed=Emb("Unknown club", f"{key} not configured.", kind="warning", guild=ctx.guild))
                return
            clubs[key]["closed"] = closed
        await ctx.send(embed=Emb("Club Updated", f"{key} closed = **{closed}**", kind="success", guild=ctx.guild))

    @BsClub.command(name="remove", aliases=["del", "delete", "rm"])
    async def BsClubRemove(self, ctx: commands.Context, tag: str):
        if not await self.GuardAdmin(ctx):
            return
        key = f"#{NormTag(tag)}".upper()
        async with self.config.guild(ctx.guild).clubs() as clubs:
            if key not in clubs:
                await ctx.send(embed=Emb("Unknown club", f"{key} is not configured.", kind="warning", guild=ctx.guild))
                return
            removed = clubs.pop(key, None)
        if removed:
            role_id = removed.get("role_id")
            chat_id = removed.get("chat_channel_id")
            role = ctx.guild.get_role(role_id) if role_id else None
            ch = ctx.guild.get_channel(chat_id) if chat_id else None
            details = []
            if role: details.append(f"role {role.mention}")
            if ch:   details.append(f"chat {ch.mention}")
            extra = f" ({', '.join(details)})" if details else ""
            await ctx.send(embed=Emb("Club Removed", f"**{key}** has been removed{extra}.", kind="success", guild=ctx.guild))
        else:
            await ctx.send(embed=Emb("Club Removed", f"**{key}** has been removed.", kind="success", guild=ctx.guild))

    @BsClub.command(name="list")
    async def BsClubList(self, ctx: commands.Context):
        if not await self.GuardAdmin(ctx):
            return
        clubs = await self.config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send(embed=Emb("No clubs", "Use `[p]bs club add` to configure some.", kind="neutral", guild=ctx.guild))
            return
        api = await self.Api(ctx.guild)
        lines = []
        for k, v in clubs.items():
            tag = v["tag"]
            try:
                info = await api.Club(tag)
                req = int(info.get("requiredTrophies") or 0)
                members = int(info.get("members") or 0)
                sfx = f" • API req {req} • {members}/{MAX_CLUB_MEMBERS}"
            except Exception:
                sfx = " • (API unreachable)"
            lines.append(f"{k}: role <@&{v['role_id']}> • chat <#{v['chat_channel_id']}> • closed={v.get('closed', False)}{sfx}")
        await ctx.send(embed=Emb("Clubs", box("\n".join(lines), lang="ini"), kind="primary", guild=ctx.guild))

    # ---- Import / Export
    @BsClub.command(name="export")
    async def BsClubExport(self, ctx: commands.Context):
        """Export current clubs to a JSON file."""
        if not await self.GuardAdmin(ctx):
            return
        clubs = await self.config.guild(ctx.guild).clubs()
        pretty = json.dumps(clubs, indent=2, ensure_ascii=False)
        data = pretty.encode("utf-8")
        buf = BytesIO(data)
        buf.seek(0)
        await ctx.send(
            embed=Emb("Exported", "Here is your **clubs_export.json**.", kind="success", guild=ctx.guild),
            file=discord.File(buf, filename="clubs_export.json"),
        )

    @BsClub.command(name="import")
    async def BsClubImport(self, ctx: commands.Context, mode: str = "merge", *, json_text: str = ""):
        """Import clubs from JSON.
        Usage:
          [p]bs club import [merge|replace] <json>
          (or attach a .json file to the command message)

        Accepts:
          - dict keyed by tag:  { "#TAG": { ... } }
          - list of objects:    [ { "tag": "#TAG", ... }, ... ]

        Fields per club:
          tag, role_id|role, chat_channel_id|chat, optional closed, optional min_trophies (legacy)
        Role/Channel can be IDs or mention strings.
        """
        if not await self.GuardAdmin(ctx):
            return
        # Prefer attachment if present
        raw = None
        if ctx.message.attachments:
            att = ctx.message.attachments[0]
            try:
                raw_bytes = await att.read()
                raw = raw_bytes.decode("utf-8", errors="replace")
            except Exception as e:
                await ctx.send(embed=Emb("Attachment error", f"Could not read attachment: {e}", kind="error", guild=ctx.guild))
                return
        else:
            raw = json_text.strip()
        if not raw:
            await ctx.send(embed=Emb("No JSON", "Provide JSON inline or attach a `.json` file.", kind="warning", guild=ctx.guild))
            return

        try:
            payload = json.loads(raw)
        except Exception as e:
            await ctx.send(embed=Emb("Invalid JSON", f"{e}", kind="error", guild=ctx.guild))
            return

        # Normalize to dict[tag] = ClubConfig.ToDict()
        imported: Dict[str, Dict[str, Any]] = {}
        def _push(club_obj: Dict[str, Any]):
            tag_in = club_obj.get("tag") or club_obj.get("Tag")
            if not tag_in:
                return "Missing 'tag'"
            tag_norm = f"#{NormTag(tag_in)}"
            role_val = club_obj.get("role_id", club_obj.get("role"))
            chat_val = club_obj.get("chat_channel_id", club_obj.get("chat"))
            role_id = ExtractId(role_val)
            chat_id = ExtractId(chat_val)
            if not role_id or not ctx.guild.get_role(role_id):
                return f"Invalid role for {tag_norm}"
            if not chat_id or not ctx.guild.get_channel(chat_id):
                return f"Invalid channel for {tag_norm}"
            closed = bool(club_obj.get("closed", False))
            min_trophies = club_obj.get("min_trophies", None)
            try:
                if min_trophies is not None:
                    min_trophies = int(min_trophies)
            except Exception:
                min_trophies = None
            c = ClubConfig(
                tag=tag_norm,
                role_id=int(role_id),
                chat_channel_id=int(chat_id),
                closed=closed,
                min_trophies=min_trophies,
            )
            imported[tag_norm.upper()] = c.ToDict()
            return None

        errors: List[str] = []
        if isinstance(payload, dict):
            # Either dict keyed by tag or wrapper {"clubs": ...}
            if "clubs" in payload and isinstance(payload["clubs"], (list, dict)):
                payload = payload["clubs"]
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if isinstance(v, dict) and "tag" not in v:
                        v = {**v, "tag": k}
                    err = _push(v if isinstance(v, dict) else {})
                    if err:
                        errors.append(err)
            else:
                await ctx.send(embed=Emb("Invalid JSON shape", "Expected dict or list.", kind="error", guild=ctx.guild))
                return
        elif isinstance(payload, list):
            for item in payload:
                err = _push(item if isinstance(item, dict) else {})
                if err:
                    errors.append(err)
        else:
            await ctx.send(embed=Emb("Invalid JSON shape", "Expected dict or list.", kind="error", guild=ctx.guild))
            return

        if errors:
            preview = "\n".join(f"• {e}" for e in errors[:10])
            await ctx.send(embed=Emb("Some entries were skipped", preview, kind="warning", guild=ctx.guild))

        if not imported:
            await ctx.send(embed=Emb("Nothing imported", "No valid club entries found.", kind="warning", guild=ctx.guild))
            return

        mode = (mode or "merge").lower()
        if mode not in {"merge", "replace"}:
            mode = "merge"

        if mode == "replace":
            await self.config.guild(ctx.guild).clubs.set(imported)
            action_msg = "Replaced all clubs."
        else:
            async with self.config.guild(ctx.guild).clubs() as clubs:
                clubs.update(imported)
            action_msg = "Merged clubs into existing set."

        await ctx.send(embed=Emb("Import complete", f"{action_msg} Imported: **{len(imported)}** entries.", kind="success", guild=ctx.guild))

    # ---- Lobby posting
    @Bs.command(name="lobby")
    async def BsLobby(self, ctx: commands.Context, sub: str):
        if not await self.GuardAdmin(ctx):
            return
        if sub.lower() != "postbutton":
            await ctx.send_help()
            return
        lobby_id = await self.config.guild(ctx.guild).lobby_channel_id()
        if not lobby_id:
            await ctx.send(embed=Emb("Set lobby first", "Use `[p]bs setlobby #channel`", kind="warning", guild=ctx.guild))
            return
        ch = ctx.guild.get_channel(lobby_id)
        if not ch:
            await ctx.send(embed=Emb("Lobby not found", kind="error", guild=ctx.guild))
            return
        lobby_embed = await self.BuildLobbyEmbed(ctx.guild)
        await ch.send(embed=lobby_embed, view=StartView(self))
        await ctx.send(embed=Emb("Posted", f"Start & How-it-works posted in {ch.mention}", kind="success", guild=ctx.guild))

    # ---- User tag management
    @commands.group(name="bstag", invoke_without_command=True)
    async def BsTag(self, ctx: commands.Context):
        await ctx.send_help()

    @BsTag.command(name="show")
    async def BsTagShow(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        target = member or ctx.author
        tag = await self.config.user(target).tag()
        if tag:
            await ctx.send(embed=Emb("Saved Tag", f"{target.mention}: `{tag}`", kind="primary", guild=ctx.guild))
        else:
            await ctx.send(embed=Emb("Saved Tag", f"{target.mention} has no saved tag.", kind="neutral", guild=ctx.guild))

    @BsTag.command(name="set")
    async def BsTagSet(self, ctx: commands.Context, tag: str):
        if not ctx.guild:
            await ctx.send(embed=Emb("Use in a server", kind="warning"))
            return
        n = NormTag(tag)
        api = await self.Api(ctx.guild)
        try:
            await api.Player(n)
        except Exception as ex:
            await ctx.send(embed=Emb("Invalid Tag", f"Could not validate: {ex}", kind="error", guild=ctx.guild))
            return
        await self.config.user(ctx.author).tag.set(f"#{n}")
        await ctx.send(embed=Emb("Saved", f"Your tag is now `#{n}`", kind="success", guild=ctx.guild))

    @BsTag.command(name="clear")
    async def BsTagClear(self, ctx: commands.Context):
        await self.config.user(ctx.author).tag.clear()
        await ctx.send(embed=Emb("Cleared", "Your saved tag has been removed.", kind="success", guild=ctx.guild))

    @BsTag.command(name="setfor")
    async def BsTagSetFor(self, ctx: commands.Context, member: discord.Member, tag: str):
        if not await self.GuardAdmin(ctx):
            return
        if not ctx.guild:
            await ctx.send(embed=Emb("Use in a server", kind="warning"))
            return
        n = NormTag(tag)
        api = await self.Api(ctx.guild)
        try:
            await api.Player(n)
        except Exception as ex:
            await ctx.send(embed=Emb("Invalid Tag", f"Could not validate: {ex}", kind="error", guild=ctx.guild))
            return
        await self.config.user(member).tag.set(f"#{n}")
        await ctx.send(embed=Emb("Saved", f"{member.mention} tag set to `#{n}`", kind="success", guild=ctx.guild))

    @BsTag.command(name="clearfor")
    async def BsTagClearFor(self, ctx: commands.Context, member: discord.Member):
        if not await self.GuardAdmin(ctx):
            return
        await self.config.user(member).tag.clear()
        await ctx.send(embed=Emb("Cleared", f"Removed saved tag for {member.mention}", kind="success", guild=ctx.guild))

    @Bs.command(name="diag")
    async def BsDiag(self, ctx: commands.Context, *, tag_or_club: str = "#0000000"):
        """Ping BS API and show raw status/info."""
        if not await self.GuardAdmin(ctx):
            return
        api = await self.Api(ctx.guild)
        probe = NormTag(tag_or_club)
        what = "player"
        try:
            if probe.startswith("2") or probe.startswith("P"):  # rough heuristic
                data = await api.Club(probe)
                what = "club"
            else:
                data = await api.Player(probe)
                what = "player"
            pretty = json.dumps({k: data.get(k) for k in list(data)[:8]}, indent=2, ensure_ascii=False)
            await ctx.send(embed=Emb("API OK", f"Fetched {what} `{probe}`.\n\n```json\n{pretty}\n```", kind="success", guild=ctx.guild))
        except Exception as ex:
            await ctx.send(embed=Emb("API error", f"`{type(ex).__name__}`\n{ex}", kind="error", guild=ctx.guild))

    @Bs.command(name="myip")
    async def BsMyIp(self, ctx: commands.Context):
        """Show the bot's current public IP (for allowlisting)."""
        if not await self.GuardAdmin(ctx):
            return
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.ipify.org?format=json", timeout=8) as r:
                    data = await r.json()
            await ctx.send(embed=Emb("Current public IP", f"`{data.get('ip','?')}`\nAdd this to your Supercell key allowlist.", kind="primary", guild=ctx.guild))
        except Exception as ex:
            await ctx.send(embed=Emb("Couldn’t fetch IP", f"{ex}", kind="error", guild=ctx.guild))

    @BsApiKey.command(name="test")
    async def BsApiKeyTest(self, ctx: commands.Context, tag: str):
        """Quick auth test against /players — 401/403 will be explicit."""
        if not await self.GuardAdmin(ctx):
            return
        api = await self.Api(ctx.guild)
        try:
            # Any valid-looking tag works; even a 404 proves auth worked.
            await api.Player(NormTag(tag))
            await ctx.send(embed=Emb("Key looks valid", "API returned data for that tag.", kind="success", guild=ctx.guild))
        except Exception as ex:
            await ctx.send(embed=Emb("Auth/network problem", f"{ex}", kind="error", guild=ctx.guild))

    @BsClub.command(name="probe")
    async def BsClubProbe(self, ctx: commands.Context, club_tag: str):
        """Fetch a single club via the BS API to debug connectivity/auth."""
        if not await self.GuardAdmin(ctx):
            return
        api = await self.Api(ctx.guild)
        try:
            data = await api.Club(f"#{club_tag.strip().lstrip('#').upper()}")
            pretty = json.dumps(
                {k: data.get(k) for k in ("tag", "name", "type", "requiredTrophies", "members", "trophies")},
                indent=2,
                ensure_ascii=False,
            )
            await ctx.send(embed=Emb("Club OK", f"```json\n{pretty}\n```", kind="success", guild=ctx.guild))
        except Exception as ex:
            await ctx.send(embed=Emb("Club error", f"{ex}", kind="error", guild=ctx.guild))

