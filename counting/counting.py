# counting_cog.py
# Red-DiscordBot cog: Cooperative counting game
# Modernized for discord.py 2.x / Red v3
# - No payouts
# - Fun, playful copy everywhere
# - Rich embeds for info, status, record, resets
# - Facepalm/celebration GIFs (randomized)
# - Cleaner config schema (ints, not strings)
# - Robust message parsing & reasons for reset
# - Leaderboard of contributors (rotations)
# - Admin utilities: set channel, set expected, reset

from __future__ import annotations

import re
import random
import logging
from typing import Dict, Optional, Tuple, List

import discord
from redbot.core import commands, checks, Config

log = logging.getLogger("red.counting")

BOT_CREDITS = "Bot by: Legend Gaming | Generaleoley"

# Emojis
OK_EMOJI = "✅"
PARTY_EMOJI = "🎉"
FAIL_EMOJI = "❌"
INFO_EMOJI = "ℹ️"
FIRE_EMOJI = "🔥"
SMILE_EMOJI = "😎"
FACEPALM_EMOJI = "🤦"

# Colors
GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB
GOLD = 0xF1C40F
PURPLE = 0x9B59B6

NUMBER_RE = re.compile(r"^\s*(\d+)\s*$")

# --- Fun language pools & GIFs ---
FUN_OK_LINES = [
    "Clean as a whistle. Next up: **{next}**.",
    "Numbers so fresh, they crunch. Say **{next}**.",
    "Mathemagical! **{next}** is calling you.",
    f"{SMILE_EMOJI} You nailed it. Up next: **{{next}}**.",
    "Certified correct™ — now give me **{next}**.",
]

FUN_RECORD_LINES = [
    f"{PARTY_EMOJI} New server record: **{{record}}**! The number line trembles.",
    f"{FIRE_EMOJI} You broke it! Record now **{{record}}**. Keep heating up!",
    "Legendary vibes only — fresh record at **{record}**!",
]

FUN_RESET_LINES = [
    f"{FACEPALM_EMOJI} Oof. That borked the count.",
    "RIP the momentum — back to **1** we go.",
    "The Count (™) has left the building. Rebooting at **1**.",
    "Plot twist! That wasn't the number. Start over at **1**.",
]

GIF_FACEPALMS = [
    # Curated SFW facepalm-ish GIFs (direct links)
    "https://media.tenor.com/i2sPJMpSJ6kAAAAC/facepalm-picard.gif",
    "https://media.tenor.com/Lk1WvR7bI8EAAAAC/oh-no-facepalm.gif",
    "https://media.tenor.com/3V8J6s2k3_sAAAAC/disappointed-homer-simpson.gif",
    "https://media.tenor.com/9G5F1_-8q0UAAAAC/facepalm-the-office.gif",
]

GIF_CELEBRATE = [
    "https://media.tenor.com/2roX3uxz_68AAAAC/celebration.gif",
    "https://media.tenor.com/at0wJt6iY7gAAAAC/party-parrot.gif",
    "https://media.tenor.com/Tw8kM9Ce1lwAAAAC/confetti-celebrate.gif",
    "https://media.tenor.com/xV2bqJPSm6wAAAAC/success-kid.gif",
]


def _make_embed(
    *,
    title: str,
    description: Optional[str] = None,
    color: int = BLUE,
    fields: Optional[List[Tuple[str, str, bool]]] = None,
    footer: Optional[str] = BOT_CREDITS,
    image_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    if fields:
        for name, value, inline in fields:
            e.add_field(name=name, value=value, inline=inline)
    if image_url:
        e.set_image(url=image_url)
    if thumbnail_url:
        e.set_thumbnail(url=thumbnail_url)
    if footer:
        e.set_footer(text=footer)
    return e


class Counting(commands.Cog):
    """Cooperative counting game — with embeds, gifs, and fun language (no payouts)."""

    __author__ = "BigPattyOG | Concept by Gen"
    __version__ = "3.0.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=903428736)
        default_guild = {
            "channel": None,           # int channel ID
            "record": 0,               # highest number reached
            "expected": 1,             # next expected number
            "last_user": None,         # last user ID who counted
            "players": {},             # user_id -> counts contributed in current run
        }
        self.config.register_guild(**default_guild)

    # -----------------------------
    # Helpers
    # -----------------------------
    async def _get_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        channel_id = await self.config.guild(guild).channel()
        if channel_id is None:
            return None
        ch = guild.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        return None

    async def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        conf = self.config.guild(guild)
        expected = await conf.expected()
        record = await conf.record()
        last_user = await conf.last_user()
        channel = await self._get_channel(guild)
        last_user_str = f"<@{last_user}>" if last_user else "—"
        channel_str = channel.mention if channel else "Not set"
        fields = [
            ("Next Number", f"**{expected}**", True),
            ("Record", f"**{record}**", True),
            ("Last Counter", last_user_str, True),
            ("Channel", channel_str, True),
        ]
        return _make_embed(
            title=f"{INFO_EMOJI} Counting Status",
            description=(
                "Work together to count upwards, one human at a time.\n\n"
                "**Rules**: rotate users, no edits, only the number."
            ),
            color=BLUE,
            fields=fields,
            thumbnail_url="https://media.tenor.com/3u4r0aZ9k5wAAAAC/counting-the-count.gif",
        )

    async def _announce_reset(
        self,
        message: discord.Message,
        *,
        reason: str,
        got: Optional[str] = None,
    ) -> None:
        try:
            await message.add_reaction(FAIL_EMOJI)
        except discord.HTTPException:
            pass
        fields = [
            ("Reset Reason", reason, False),
            ("Back To", "**1**", True),
            ("How To", "Just send the number only.", True),
        ]
        if got is not None:
            fields.insert(0, ("You Sent", f"`{got}`", False))
        embed = _make_embed(
            title=random.choice(FUN_RESET_LINES),
            description="The sacred count has reset. Breathe in. Start at **1**.",
            color=RED,
            fields=fields,
            image_url=random.choice(GIF_FACEPALMS),
        )
        await message.channel.send(embed=embed)

    async def _announce_correct(
        self,
        message: discord.Message,
        *,
        new_expected: int,
    ) -> None:
        try:
            await message.add_reaction(OK_EMOJI)
        except discord.HTTPException:
            pass
        # Post an embed at helpful cadence
        if new_expected in (2, 3) or (new_expected - 1) % 10 == 0:
            line = random.choice(FUN_OK_LINES).format(next=new_expected)
            embed = _make_embed(
                title="Nice!",
                description=line,
                color=GREEN,
            )
            await message.channel.send(embed=embed)

    async def _announce_record(
        self,
        message: discord.Message,
        *,
        record: int,
        players: Dict[str, int],
    ) -> None:
        try:
            await message.add_reaction(PARTY_EMOJI)
        except discord.HTTPException:
            pass
        # Build a simple top-10 leaderboard snapshot for this run
        if players:
            top = sorted(players.items(), key=lambda kv: kv[1], reverse=True)[:10]
            lines = [f"<@{uid}> — **{cnt}**" for uid, cnt in top]
            leaderboard = "\n".join(lines)
        else:
            leaderboard = "—"
        fields = [
            ("New Record!", f"Reached **{record}**", False),
            ("Top Contributors (this run)", leaderboard, False),
        ]
        embed = _make_embed(
            title=random.choice(FUN_RECORD_LINES).format(record=record),
            description="Numbers fear you now.",
            color=GOLD,
            fields=fields,
            image_url=random.choice(GIF_CELEBRATE),
        )
        await message.channel.send(embed=embed)

    async def _hard_reset(self, guild: discord.Guild) -> None:
        conf = self.config.guild(guild)
        await conf.expected.set(1)
        await conf.last_user.set(None)
        await conf.players.set({})

    def _is_commandish(self, content: str) -> bool:
        # Treat common command prefixes as exempt from counting
        return content.startswith(("!", "/", ".", "?", "-"))

    # -----------------------------
    # Commands
    # -----------------------------
    @commands.group()
    async def counting(self, ctx: commands.Context):
        """Counting game controls."""
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=await self._status_embed(ctx.guild))

    @counting.command(aliases=["info"])
    async def help(self, ctx: commands.Context):
        """How to play."""
        embed = _make_embed(
            title="Welcome to Counting!",
            description=(
                "You're all teaming up to count upwards. Easy, right? **Wrong.**\n\n"
                "**Send only the number.**\n"
                "Rotate users (no double-dipping).\n"
                "Edits, extra words, or wrong numbers = **reset to 1**."
            ),
            color=PURPLE,
            image_url="https://media.tenor.com/4Oe1i3ZyqS4AAAAC/sesame-street-count.gif",
        )
        await ctx.send(embed=embed)

    @counting.command()
    async def status(self, ctx: commands.Context):
        """Show current status."""
        await ctx.send(embed=await self._status_embed(ctx.guild))

    @counting.command()
    async def record(self, ctx: commands.Context):
        """Get the highest count record."""
        record = await self.config.guild(ctx.guild).record()
        embed = _make_embed(
            title="All-Time Record",
            description=f"Server best is **{record}**. Dare to beat it?",
            color=GOLD,
        )
        await ctx.send(embed=embed)

    @commands.group(aliases=["setcounting"]) 
    async def setcount(self, ctx: commands.Context):
        """Configure counting."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @checks.admin_or_permissions()
    @setcount.command()
    async def channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where counting happens."""
        await self.config.guild(ctx.guild).channel.set(channel.id)
        embed = _make_embed(
            title="Channel Updated",
            description=f"Counting will now happen in {channel.mention}.",
            color=BLUE,
        )
        await ctx.send(embed=embed)

    @checks.admin_or_permissions()
    @setcount.command()
    async def expected(self, ctx: commands.Context, count: int):
        """Manually set the next expected number (no rewards, no refunds)."""
        if count < 1:
            count = 1
        await self.config.guild(ctx.guild).expected.set(count)
        embed = _make_embed(
            title="Expected Number Updated",
            description=f"Okay brainiacs, next number is now **{count}**.",
            color=PURPLE,
        )
        await ctx.send(embed=embed)

    @checks.admin_or_permissions()
    @setcount.command()
    async def reset(self, ctx: commands.Context):
        """Reset the run back to 1 and clear contributors."""
        await self._hard_reset(ctx.guild)
        embed = _make_embed(
            title="Manual Reset",
            description="Dusting off the abacus. Start again at **1**.",
            color=RED,
            image_url=random.choice(GIF_FACEPALMS),
        )
        await ctx.send(embed=embed)

    # -----------------------------
    # Listeners
    # -----------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        conf = self.config.guild(message.guild)
        channel = await self._get_channel(message.guild)
        if not channel or message.channel.id != channel.id:
            return

        content = message.content
        # Treat common bot prefixes as non-counting
        if content.startswith(("!", "/", ".", "?", "-")):
            return

        m = NUMBER_RE.match(content)
        if not m:
            await self._hard_reset(message.guild)
            await self._announce_reset(message, reason="Message wasn't just a number.", got=content)
            return

        number = int(m.group(1))
        expected = await conf.expected()
        last_user = await conf.last_user()

        # Must be the exact expected number
        if number != expected:
            await self._hard_reset(message.guild)
            await self._announce_reset(
                message,
                reason=f"Expected **{expected}**, got **{number}**.",
                got=str(number),
            )
            return

        # Must rotate users
        if last_user is not None and last_user == str(message.author.id):
            await self._hard_reset(message.guild)
            await self._announce_reset(message, reason="Same user twice in a row.", got=str(number))
            return

        # Update state for a correct number
        new_expected = expected + 1
        await conf.expected.set(new_expected)
        await conf.last_user.set(str(message.author.id))
        players = await conf.players()
        players[str(message.author.id)] = players.get(str(message.author.id), 0) + 1
        await conf.players.set(players)

        # React OK & maybe say nice line
        await self._announce_correct(message, new_expected=new_expected)

        # Check for record
        record = await conf.record()
        just_reached = number
        if just_reached > record:
            await conf.record.set(just_reached)
            await self._announce_record(message, record=just_reached, players=players)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Any edit in the counting channel resets
        if not after.guild:
            return
        channel = await self._get_channel(after.guild)
        if channel and after.channel.id == channel.id and before.content != after.content:
            await self._hard_reset(after.guild)
            await self._announce_reset(after, reason="Message was edited.", got=after.content)

    # Metadata for [p]cog info
    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre = super().format_help_for_context(ctx)
        return f"{pre}\n\nVersion: {self.__version__}\nAuthor: {self.__author__}"
