# brawlstars_manager.py
# Red-DiscordBot cog: Brawl Stars self-service onboarding & club placement
# v0.5.0 — Interactive club add wizard + remove "Club Trophies" field
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

import aiohttp
import discord
from discord.ext import commands
from redbot.core import Config, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

log = logging.getLogger("red.brawlstars_manager")

API_BASE = "https://api.brawlstars.com/v1"
MAX_CLUB_MEMBERS = 50
THREAD_AUTO_ARCHIVE_M = 1440  # 24h
# Strip a leading "TLG " (case-insensitive) from the club name when formatting nickname suffix
NICK_PREFIX_STRIP = re.compile(r"^(TLG\s+)", re.IGNORECASE)


# ----------------- helpers -----------------
def norm_tag(tag: str) -> str:
    """Normalize a BS tag (strip #, uppercase, O->0)."""
    return tag.strip().lstrip("#").upper().replace("O", "0")


def enc_tag(tag: str) -> str:
    return urllib.parse.quote(f"#{norm_tag(tag)}", safe="")


def emb(title: str, desc: str | None = None, *, color: int = 0x2B2D31) -> discord.Embed:
    return discord.Embed(title=title, description=desc or discord.Embed.Empty, color=color)


class BSAPI:
    """Tiny BS API client with basic retry handling."""

    def __init__(self, session: aiohttp.ClientSession, api_key_getter):
        self.session = session
        self._get_key = api_key_getter  # async callable

    async def _request(self, method: str, path: str) -> Dict[str, Any]:
        key = await self._get_key()
        if not key:
            raise RuntimeError("Brawl Stars API key not set. Use [p]bs apikey set <token>.")
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Red-Cog-BS-Manager/0.5",
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

    async def player(self, tag: str) -> Dict[str, Any]:
        return await self._request("GET", f"/players/{enc_tag(tag)}")

    async def club(self, tag: str) -> Dict[str, Any]:
        return await self._request("GET", f"/clubs/{enc_tag(tag)}")


@dataclass
class ClubConfig:
    tag: str                 # "#ABCDEFG"
    min_trophies: int        # e.g. 20000
    role_id: int             # role to assign on success (club role)
    chat_channel_id: int     # where to ping them when done
    closed: bool = False     # admin override to exclude

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ClubConfig":
        return cls(
            tag=d["tag"],
            min_trophies=int(d["min_trophies"]),
            role_id=int(d["role_id"]),
            chat_channel_id=int(d["chat_channel_id"]),
            closed=bool(d.get("closed", False)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tag": self.tag,
            "min_trophies": self.min_trophies,
            "role_id": self.role_id,
            "chat_channel_id": self.chat_channel_id,
            "closed": self.closed,
        }


# ----------------- UI components -----------------
class StartView(discord.ui.View):
    """Persistent 'Get Started' button in lobby."""

    def __init__(self, cog: "BrawlStarsManager"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Get Started", style=discord.ButtonStyle.primary, custom_id="bs:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        # If user has a saved tag, use it; otherwise open modal
        saved = await self.cog.get_user_tag(interaction.user)
        if saved:
            await self.cog.begin_flow_with_tag(interaction, saved)
        else:
            await interaction.response.send_modal(TagModal(self.cog))


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
        await self.cog.begin_flow_with_tag(interaction, str(self.tag_input.value))


class ClubSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], cog: "BrawlStarsManager", thread_id: int, applicant_id: int):
        super().__init__(placeholder="Choose a club", min_values=1, max_values=1, options=options, custom_id="bs:clubselect")
        self.cog = cog
        self.thread_id = thread_id
        self.applicant_id = applicant_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.applicant_id:
            await interaction.response.send_message(embed=emb("Not for you", "Only the applicant can select a club."), ephemeral=True)
            return
        chosen_tag = self.values[0]
        await self.cog.on_club_selected(interaction, self.thread_id, self.applicant_id, chosen_tag)


class ClubSelectView(discord.ui.View):
    def __init__(self, cog: "BrawlStarsManager", options: List[discord.SelectOption], thread_id: int, applicant_id: int):
        super().__init__(timeout=600)
        self.add_item(ClubSelect(options, cog, thread_id, applicant_id))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="bs:cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=emb("Cancelled", "You can close this thread."), ephemeral=True)


class ProvideLinkButton(discord.ui.View):
    def __init__(self, cog: "BrawlStarsManager", thread_id: int, member_id: int, club_tag: str):
        super().__init__(timeout=1800)
        self.cog = cog
        self.thread_id = thread_id
        self.member_id = member_id
        self.club_tag = club_tag

    @discord.ui.button(label="Provide Club Invite Link", style=discord.ButtonStyle.primary, custom_id="bs:lead:link")
    async def provide(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await self.cog.handle_leader_link(interaction, self.thread_id, self.member_id, self.club_tag, str(self.link.value))


class JoinedView(discord.ui.View):
    def __init__(self, cog: "BrawlStarsManager", club_tag: str, member_id: int):
        super().__init__(timeout=3600)
        self.cog = cog
        self.club_tag = club_tag
        self.member_id = member_id

    @discord.ui.button(label="I've joined the club", style=discord.ButtonStyle.success, custom_id="bs:joined")
    async def joined(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_joined(interaction, self.club_tag, self.member_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="bs:joined_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=emb("Cancelled", "You can close this thread."), ephemeral=True)


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
        await self.wizard.set_tag(interaction, str(self.tag_input.value))


class ClubAddMinTrophiesModal(discord.ui.Modal, title="Minimum Trophies"):
    min_input = discord.ui.TextInput(
        label="Required Min Trophies",
        placeholder="e.g. 20000",
        max_length=6,
        required=True,
    )
    def __init__(self, wizard: "ClubAddWizard"):
        super().__init__(timeout=300)
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.wizard.set_min_trophies(interaction, str(self.min_input.value))


class RolePicker(discord.ui.RoleSelect):
    def __init__(self, wizard: "ClubAddWizard"):
        super().__init__(placeholder="Pick club role", min_values=1, max_values=1)
        self.wizard = wizard

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        await self.wizard.set_role(interaction, role)


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, wizard: "ClubAddWizard"):
        # Limit to text channels or threads
        super().__init__(placeholder="Pick club chat channel", min_values=1, max_values=1, channel_types=[discord.ChannelType.text, discord.ChannelType.public_thread, discord.ChannelType.private_thread])
        self.wizard = wizard

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await self.wizard.set_channel(interaction, channel)


class ClubAddWizard(discord.ui.View):
    """Interactive stateful wizard for adding a club config."""

    def __init__().subclass__(cls, **kwargs):  # keep mypy quiet for View's metaclass
        return super().__init_subclass__(**kwargs)

    def __init__(self, cog: "BrawlStarsManager", invoker: discord.Member):
        super().__init__(timeout=600)
        self.cog = cog
        self.invoker = invoker
        self.guild = invoker.guild

        self.club_tag: Optional[str] = None      # normalized "#TAG"
        self.club_name: Optional[str] = None     # fetched from API
        self.min_trophies: Optional[int] = None
        self.role_id: Optional[int] = None
        self.chat_channel_id: Optional[int] = None

        # Dynamic pickers
        self.role_picker = RolePicker(self)
        self.channel_picker = ChannelPicker(self)
        self.add_item(self.role_picker)
        self.add_item(self.channel_picker)

    # --- Buttons ---
    @discord.ui.button(label="Set Club Tag", style=discord.ButtonStyle.primary, row=2)
    async def btn_tag(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_invoker(interaction):
            return
        await interaction.response.send_modal(ClubAddTagModal(self))

    @discord.ui.button(label="Set Min Trophies", style=discord.ButtonStyle.primary, row=2)
    async def btn_min(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_invoker(interaction):
            return
        await interaction.response.send_modal(ClubAddMinTrophiesModal(self))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def btn_save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_invoker(interaction):
            return
        if not (self.club_tag and self.min_trophies is not None and self.role_id and self.chat_channel_id):
            await interaction.response.send_message(embed=emb("Missing info", "Please set **Club Tag**, **Min Trophies**, **Role**, and **Chat Channel**."), ephemeral=True)
            return
        # Persist
        tag_key = self.club_tag.upper()
        c = ClubConfig(tag=self.club_tag, min_trophies=self.min_trophies, role_id=self.role_id, chat_channel_id=self.chat_channel_id)
        async with self.cog.config.guild(self.guild).clubs() as clubs:
            clubs[tag_key] = c.to_dict()
        # Ack
        role = self.guild.get_role(self.role_id)
        ch = self.guild.get_channel(self.chat_channel_id)
        title = self.club_name or self.club_tag
        await interaction.response.send_message(
            embed=emb("Club Saved", f"**{title}** ({self.club_tag})\nMin trophies: **{self.min_trophies}**\nRole: {role.mention if role else self.role_id}\nChat: {ch.mention if ch else self.chat_channel_id}"),
            ephemeral=True
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_invoker(interaction):
            return
        await interaction.response.send_message(embed=emb("Cancelled", "No changes saved."), ephemeral=True)
        self.stop()

    # --- Setters (called from modals/selects) ---
    async def set_tag(self, interaction: discord.Interaction, raw_tag: str):
        if not self._is_invoker(interaction):
            return
        tag = f"#{norm_tag(raw_tag)}"
        api = await self.cog._api(self.guild)
        try:
            info = await api.club(tag)
        except Exception as ex:
            await interaction.response.send_message(embed=emb("Invalid Club Tag", f"Could not fetch club: {ex}"), ephemeral=True)
            return
        self.club_tag = tag
        self.club_name = info.get("name") or tag
        await interaction.response.send_message(embed=emb("Tag Set", f"Club: **{self.club_name}** ({tag})"), ephemeral=True)

    async def set_min_trophies(self, interaction: discord.Interaction, raw: str):
        if not self._is_invoker(interaction):
            return
        try:
            val = int(raw.replace(",", "").strip())
            if val < 0:
                raise ValueError
        except Exception:
            await interaction.response.send_message(embed=emb("Invalid number", "Please enter a positive integer."), ephemeral=True)
            return
        self.min_trophies = val
        await interaction.response.send_message(embed=emb("Min Trophies Set", f"**{val}**"), ephemeral=True)

    async def set_role(self, interaction: discord.Interaction, role: discord.Role):
        if not self._is_invoker(interaction):
            return
        self.role_id = role.id
        await interaction.response.send_message(embed=emb("Role Selected", f"{role.mention}"), ephemeral=True)

    async def set_channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel):
        if not self._is_invoker(interaction):
            return
        # Accept text channels or threads
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(embed=emb("Unsupported channel", "Please pick a text channel or thread."), ephemeral=True)
            return
        self.chat_channel_id = channel.id
        await interaction.response.send_message(embed=emb("Chat Channel Selected", f"{getattr(channel, 'mention', channel.id)}"), ephemeral=True)

    # --- helpers ---
    def _is_invoker(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            asyncio.create_task(interaction.response.send_message(embed=emb("Not for you", "Only the command invoker can use this wizard."), ephemeral=True))
            return False
        return True


# ----------------- Cog -----------------
class BrawlStarsManager(commands.Cog):
    """Self-service Brawl Stars club placement."""

    __author__ = "Pat+Chat"
    __version__ = "0.5.0"

    def __init__(self, bot: Red):
        self.bot: Red = bot
        self.config = Config.get_conf(self, identifier=0xB5A71C09, force_registration=True)
        self.config.register_guild(
            api_key=None,                # guild-scoped API key (fallback to env if None)
            lobby_channel_id=None,       # where Start button lives
            leadership_channel_id=None,  # where leaders get notified
            family_role_id=None,         # global family role (e.g. TLG Family)
            clubs={},                    # { "#TAG": ClubConfigDict }
            threads={},                  # { thread_id: {member_id, tag, chosen} }
        )
        self.config.register_user(
            tag=None,                    # saved user tag (e.g. "#ABC123")
        )
        self._session: Optional[aiohttp.ClientSession] = None

    # -------- lifecycle --------
    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        # Persistent view so the lobby button survives restarts
        self.bot.add_view(StartView(self))

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    # -------- API helper --------
    async def _api(self, guild: discord.Guild) -> BSAPI:
        async def get_key():
            # Prefer configured key; otherwise env var fallback
            key = await self.config.guild(guild).api_key()
            if not key:
                key = os.getenv("BRAWLSTARS_API_KEY")
            return key
        return BSAPI(self._session, get_key)

    # -------- user tag helpers --------
    async def get_user_tag(self, user: discord.abc.User) -> Optional[str]:
        return await self.config.user(user).tag()

    # -------- flow entry --------
    async def begin_flow_with_tag(self, interaction: discord.Interaction, raw_tag: str):
        """Entry point after modal or saved-tag shortcut."""
        if not interaction.guild:
            await interaction.response.send_message(embed=emb("Use in a server", "Please run this in a server."), ephemeral=True)
            return

        # Enforce lobby if set
        gconf = self.config.guild(interaction.guild)
        lobby_id = await gconf.lobby_channel_id()
        if lobby_id and interaction.channel_id != lobby_id:
            await interaction.response.send_message(embed=emb("Wrong channel", "Please use the configured lobby channel."), ephemeral=True)
            return

        tag = norm_tag(raw_tag)
        api = await self._api(interaction.guild)
        try:
            pdata = await api.player(tag)
        except Exception as e:
            await interaction.response.send_message(embed=emb("Couldn’t fetch player", f"Check your tag. {e}"), ephemeral=True)
            return

        ign = pdata.get("name", "Unknown")
        trophies = pdata.get("trophies", 0)

        # Save user tag for reuse
        await self.config.user(interaction.user).tag.set(f"#{tag}")

        # Create a private thread off the lobby (or current text channel)
        parent_ch = interaction.channel
        if isinstance(parent_ch, discord.Thread):
            parent_ch = parent_ch.parent
        if not isinstance(parent_ch, discord.TextChannel):
            await interaction.response.send_message(embed=emb("Channel issue", "Please start from a text channel."), ephemeral=True)
            return

        thread_name = f"{ign} · #{tag}"
        thread = await parent_ch.create_thread(
            name=thread_name[:90],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=THREAD_AUTO_ARCHIVE_M,
        )
        await thread.add_user(interaction.user)

        # Save per-thread state
        async with gconf.threads() as threads:
            threads[str(thread.id)] = {"member_id": interaction.user.id, "tag": tag, "chosen": None}

        # Acknowledge in lobby
        await interaction.response.send_message(
            embed=emb("Thread created", f"I made a private thread for you: {thread.mention}"),
            ephemeral=True,
        )

        # Intro in thread
        intro = emb(
            "Welcome!",
            f"**IGN:** `{ign}` • **Tag:** `#{tag}` • **Trophies:** **{trophies}**\n\n"
            "I’ll now check which clubs you’re eligible for…"
        )
        await thread.send(content=interaction.user.mention, embed=intro)

        await self.show_eligible_clubs(thread, interaction.user, tag, trophies)

    # -------- eligibility & selection --------
    async def show_eligible_clubs(self, thread: discord.Thread, member: discord.Member, tag: str, trophies: int):
        gconf = self.config.guild(thread.guild)
        clubs_raw = await gconf.clubs()
        clubs = [ClubConfig.from_dict(v) for v in clubs_raw.values()]
        api = await self._api(thread.guild)

        if not clubs:
            await thread.send(embed=emb("No clubs configured", "An admin can add clubs with `[p]bs club add`"))
            return

        # Step 1) min trophies + fetch live club info
        candidates: List[Tuple[ClubConfig, Dict[str, Any]]] = []
        for c in clubs:
            if c.closed:
                continue
            if trophies < c.min_trophies:
                continue
            try:
                cinfo = await api.club(c.tag)
            except Exception as e:
                await thread.send(embed=emb("API error", f"Couldn’t fetch club {c.tag}: {e}"))
                continue
            candidates.append((c, cinfo))

        if not candidates:
            await thread.send(embed=emb("Not eligible yet", "You don’t meet the trophy requirements for any configured club yet."))
            return

        # Step 2) remove full or closed (API)
        eligible: List[Tuple[ClubConfig, Dict[str, Any]]] = []
        for c, info in candidates:
            ctype = (info.get("type") or "").lower()  # open/inviteOnly/closed
            members = int(info.get("members", 0))
            if ctype == "closed":
                continue
            if members >= MAX_CLUB_MEMBERS:
                continue
            eligible.append((c, info))

        if not eligible:
            await thread.send(embed=emb("No open spots", "All suitable clubs are currently full or closed. Please check back later."))
            return

        # Step 3) Show list & detailed embeds
        options: List[discord.SelectOption] = []
        for c, info in eligible:
            name = info.get("name", c.tag)
            m = int(info.get("members", 0))
            options.append(discord.SelectOption(label=name[:100], description=f"{m}/{MAX_CLUB_MEMBERS}", value=c.tag))

        view = ClubSelectView(self, options, thread.id, member.id)
        await thread.send(
            embed=emb("Eligible Clubs", "Select a club from the dropdown below to proceed."),
            view=view,
        )

        # Detailed embeds (live info, not hardcoded) — removed "Club Trophies"
        for c, info in eligible:
            await thread.send(embed=self._club_embed(info))

    def _club_embed(self, info: Dict[str, Any]) -> discord.Embed:
        name = info.get("name", "?")
        tag = info.get("tag", "?")
        desc = info.get("description", "") or "No description."
        req = info.get("requiredTrophies", 0)
        members = info.get("members", 0)
        ctype = str(info.get("type", "unknown")).title()
        # Removed: trophies = info.get("trophies", 0)
        em = discord.Embed(title=f"{name} ({tag})", description=desc, color=0x2B2D31)
        em.add_field(name="Type", value=str(ctype))
        em.add_field(name="Members", value=f"{members}/{MAX_CLUB_MEMBERS}")
        em.add_field(name="Req. Trophies", value=str(req))
        return em

    async def on_club_selected(self, interaction: discord.Interaction, thread_id: int, applicant_id: int, chosen_tag: str):
        if not interaction.guild:
            await interaction.response.send_message(embed=emb("Guild missing"), ephemeral=True)
            return
        if interaction.user.id != applicant_id:
            await interaction.response.send_message(embed=emb("Not for you", "Only the applicant can select a club."), ephemeral=True)
            return

        gconf = self.config.guild(interaction.guild)
        async with gconf.threads() as threads:
            t = threads.get(str(thread_id))
            if not t:
                await interaction.response.send_message(embed=emb("Thread state missing"), ephemeral=True)
                return
            t["chosen"] = chosen_tag
            threads[str(thread_id)] = t

        await interaction.response.send_message(embed=emb("Selected", f"Great—selected **{chosen_tag}**. I’ll notify leadership now."), ephemeral=True)
        await self.notify_leadership(interaction.guild, thread_id, applicant_id, chosen_tag)

    # -------- leadership handoff --------
    async def notify_leadership(self, guild: discord.Guild, thread_id: int, member_id: int, club_tag: str):
        gconf = self.config.guild(guild)
        lead_id = await gconf.leadership_channel_id()
        if not lead_id:
            return
        channel = guild.get_channel(lead_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        member = guild.get_member(member_id)
        thread = guild.get_thread(thread_id)
        if not member or not thread:
            return

        await channel.send(
            embed=emb(
                "New applicant needs invite",
                f"Applicant: {member.mention}\nThread: {thread.mention}\nClub: **{club_tag}**\n\n"
                "Click the button below to provide the invite link."
            ),
            view=ProvideLinkButton(self, thread.id, member.id, club_tag),
        )

    async def handle_leader_link(self, interaction: discord.Interaction, thread_id: int, member_id: int, club_tag: str, url: str):
        if not interaction.guild:
            await interaction.response.send_message(embed=emb("Guild missing"), ephemeral=True)
            return
        thread = interaction.guild.get_thread(thread_id)
        member = interaction.guild.get_member(member_id)
        if not thread or not member:
            await interaction.response.send_message(embed=emb("Thread/member not found"), ephemeral=True)
            return
        if not url.startswith("http"):
            await interaction.response.send_message(embed=emb("Invalid link", "Please provide a valid URL."), ephemeral=True)
            return

        await interaction.response.send_message(embed=emb("Thanks!", "I’ve sent the link to the applicant."), ephemeral=True)

        await thread.send(
            embed=emb(
                "Your club invite",
                f"{member.mention}, here’s your invite link for **{club_tag}**:\n{url}\n\n"
                "Once you’ve joined, press the button below and I’ll verify and finish up."
            ),
            view=JoinedView(self, club_tag, member.id),
        )

    # -------- verification & finish --------
    async def handle_joined(self, interaction: discord.Interaction, club_tag: str, member_id: int):
        if not interaction.guild:
            await interaction.response.send_message(embed=emb("Guild missing"), ephemeral=True)
            return
        if interaction.user.id != member_id:
            await interaction.response.send_message(embed=emb("Not for you", "Only the applicant can confirm joining."), ephemeral=True)
            return

        gconf = self.config.guild(interaction.guild)
        # find thread state
        async with gconf.threads() as threads:
            t = threads.get(str(interaction.channel_id), {})

        tag = t.get("tag")
        if not tag:
            await interaction.response.send_message(embed=emb("No tag on file", "Please restart the flow."), ephemeral=True)
            return

        api = await self._api(interaction.guild)
        try:
            pdata = await api.player(tag)
        except Exception as e:
            await interaction.response.send_message(embed=emb("API error", f"Couldn’t verify via API: {e}"), ephemeral=True)
            return

        club_now = (pdata.get("club") or {}).get("tag")
        ign = pdata.get("name", "Unknown")
        if not club_now or norm_tag(club_now) != norm_tag(club_tag):
            await interaction.response.send_message(embed=emb("Not yet", "I don’t see you in that club yet. Try again in a minute."), ephemeral=True)
            return

        # Assign Family + Club role, rename, ping in club chat
        clubs_raw = await gconf.clubs()
        cconf_raw = clubs_raw.get(club_tag.upper())
        if not cconf_raw:
            await interaction.response.send_message(embed=emb("Config missing", "Club config missing; contact staff."), ephemeral=True)
            return
        cconf = ClubConfig.from_dict(cconf_raw)

        family_role_id = await gconf.family_role_id()
        family_role = interaction.guild.get_role(family_role_id) if family_role_id else None
        club_role = interaction.guild.get_role(cconf.role_id)
        chat_chan = interaction.guild.get_channel(cconf.chat_channel_id)

        member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)

        # Build nickname
        try:
            cinfo = await api.club(club_tag)
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

        await interaction.response.send_message(embed=emb("Verified!", f"Assigned your roles and set nickname to **{new_nick}**."), ephemeral=True)

        if chat_chan and isinstance(chat_chan, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
            with contextlib.suppress(Exception):
                await chat_chan.send(embed=emb("Welcome!", f"Welcome {member.mention} to **{club_suffix}**! 🎉"))

        # Close/lock thread
        if isinstance(interaction.channel, discord.Thread):
            with contextlib.suppress(Exception):
                await interaction.channel.edit(locked=True, archived=True)

    # ----------------- Commands (admin & user) -----------------
    @commands.group(name="bs", invoke_without_command=True)
    async def bs(self, ctx: commands.Context):
        """Brawl Stars manager setup."""
        await ctx.send_help()

    # API KEY (natural style) + fallback to env var
    @bs.group(name="apikey", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_apikey(self, ctx: commands.Context):
        """Manage the Brawl Stars API key (guild-scoped)."""
        key = await self.config.guild(ctx.guild).api_key()
        status = "configured" if key else ("using env var" if os.getenv("BRAWLSTARS_API_KEY") else "not set")
        await ctx.send(embed=emb("API Key", f"Status: **{status}**"))

    @bs_apikey.command(name="set")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_apikey_set(self, ctx: commands.Context, *, token: str):
        """Set the Brawl Stars API key for this guild."""
        await self.config.guild(ctx.guild).api_key.set(token.strip())
        await ctx.send(embed=emb("API Key Saved", "Key stored for this guild."))

    @bs_apikey.command(name="clear")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_apikey_clear(self, ctx: commands.Context):
        """Clear the guild API key (env var may still be used)."""
        await self.config.guild(ctx.guild).api_key.clear()
        await ctx.send(embed=emb("API Key Cleared", "I will try the BRAWLSTARS_API_KEY env var if present."))

    # Lobby, leadership, family role
    @bs.command(name="setlobby")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_setlobby(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the onboarding lobby channel (where Start button lives)."""
        await self.config.guild(ctx.guild).lobby_channel_id.set(channel.id)
        await ctx.send(embed=emb("Lobby Set", f"Lobby channel is now {channel.mention}"))

    @bs.command(name="setleadership")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_setleadership(self, ctx: commands.Context, channel: discord.TextChannel | discord.Thread):
        """Set the leadership notification channel."""
        await self.config.guild(ctx.guild).leadership_channel_id.set(channel.id)
        await ctx.send(embed=emb("Leadership Set", f"Leadership channel is now {channel.mention}"))

    @bs.command(name="setfamilyrole")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_setfamilyrole(self, ctx: commands.Context, role: discord.Role):
        """Set the default Family role (e.g., 'TLG Family')."""
        await self.config.guild(ctx.guild).family_role_id.set(role.id)
        await ctx.send(embed=emb("Family Role Set", f"Family role is now {role.mention}"))

    # --- Clubs management
    @bs.group(name="club", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_club(self, ctx: commands.Context):
        """Manage club entries."""
        await ctx.send_help()

    @bs_club.command(name="add")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_club_add(self, ctx: commands.Context):
        """Start the interactive wizard to add/update a club."""
        if not ctx.guild:
            await ctx.send(embed=emb("Use in a server"))
            return
        view = ClubAddWizard(self, ctx.author)
        await ctx.send(
            embed=emb(
                "Add a Club (wizard)",
                "Steps:\n1) **Set Club Tag** (validated via API)\n2) **Set Min Trophies**\n3) **Pick Role**\n4) **Pick Chat Channel**\nThen press **Save**."
            ),
            view=view,
        )

    @bs_club.command(name="close")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_club_close(self, ctx: commands.Context, tag: str, closed: bool):
        """Mark a club as closed/open (override)."""
        key = f"#{norm_tag(tag)}".upper()
        async with self.config.guild(ctx.guild).clubs() as clubs:
            if key not in clubs:
                await ctx.send(embed=emb("Unknown club", f"{key} not configured."))
                return
            clubs[key]["closed"] = closed
        await ctx.send(embed=emb("Club Updated", f"{key} closed = **{closed}**"))

    @bs_club.command(name="list")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_club_list(self, ctx: commands.Context):
        """List configured clubs."""
        clubs = await self.config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send(embed=emb("No clubs", "Use `[p]bs club add` to configure some."))
            return
        lines = []
        for k, v in clubs.items():
            lines.append(f"{k}: min {v['min_trophies']} • role <@&{v['role_id']}> • chat <#{v['chat_channel_id']}> • closed={v.get('closed', False)}")
        await ctx.send(embed=emb("Clubs", box("\n".join(lines), lang="ini")))

    # Post the lobby button
    @bs.command(name="lobby")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_lobby(self, ctx: commands.Context, sub: str):
        """`postbutton` to post the Start button in the lobby channel."""
        if sub.lower() != "postbutton":
            await ctx.send_help()
            return
        lobby_id = await self.config.guild(ctx.guild).lobby_channel_id()
        if not lobby_id:
            await ctx.send(embed=emb("Set lobby first", "Use `[p]bs setlobby #channel`"))
            return
        ch = ctx.guild.get_channel(lobby_id)
        if not ch:
            await ctx.send(embed=emb("Lobby not found"))
            return
        await ch.send(
            embed=emb("Club Placement", "Click **Get Started** to begin the self-service flow. I'll create a private thread for you."),
            view=StartView(self),
        )
        await ctx.send(embed=emb("Posted", f"Start button posted in {ch.mention}"))

    # --- User tag management
    @bs.group(name="tag", invoke_without_command=True)
    async def bs_tag(self, ctx: commands.Context):
        """Manage your saved Brawl Stars tag."""
        await ctx.send_help()

    @bs_tag.command(name="show")
    async def bs_tag_show(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show your (or another member’s) saved tag."""
        target = member or ctx.author
        tag = await self.config.user(target).tag()
        if tag:
            await ctx.send(embed=emb("Saved Tag", f"{target.mention}: `{tag}`"))
        else:
            await ctx.send(embed=emb("Saved Tag", f"{target.mention} has no saved tag."))

    @bs_tag.command(name="set")
    async def bs_tag_set(self, ctx: commands.Context, tag: str):
        """Set your own tag (normalizes and validates via API)."""
        if not ctx.guild:
            await ctx.send(embed=emb("Use in a server"))
            return
        n = norm_tag(tag)
        api = await self._api(ctx.guild)
        try:
            await api.player(n)  # validate
        except Exception as ex:
            await ctx.send(embed=emb("Invalid Tag", f"Could not validate: {ex}"))
            return
        await self.config.user(ctx.author).tag.set(f"#{n}")
        await ctx.send(embed=emb("Saved", f"Your tag is now `#{n}`"))

    @bs_tag.command(name="clear")
    async def bs_tag_clear(self, ctx: commands.Context):
        """Clear your saved tag."""
        await self.config.user(ctx.author).tag.clear()
        await ctx.send(embed=emb("Cleared", "Your saved tag has been removed."))

    @bs_tag.command(name="setfor")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_tag_setfor(self, ctx: commands.Context, member: discord.Member, tag: str):
        """Admin: set a member’s tag."""
        if not ctx.guild:
            await ctx.send(embed=emb("Use in a server"))
            return
        n = norm_tag(tag)
        api = await self._api(ctx.guild)
        try:
            await api.player(n)
        except Exception as ex:
            await ctx.send(embed=emb("Invalid Tag", f"Could not validate: {ex}"))
            return
        await self.config.user(member).tag.set(f"#{n}")
        await ctx.send(embed=emb("Saved", f"{member.mention} tag set to `#{n}`"))

    @bs_tag.command(name="clearfor")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_tag_clearfor(self, ctx: commands.Context, member: discord.Member):
        """Admin: clear a member’s saved tag."""
        await self.config.user(member).tag.clear()
        await ctx.send(embed=emb("Cleared", f"Removed saved tag for {member.mention}"))

