from __future__ import annotations
import asyncio
from typing import Optional, Dict, List
import discord
from discord.ui import View, Select, button, Button
from redbot.core import commands, Config

CONF_ID = 347209384723923874

helpSections = [
    "Overview",
    "Setup",
    "Gameplay",
    "Admin",
]

def helpEmbed(section: str) -> discord.Embed:
    e = discord.Embed(
        title = "Counting - Help",
        colour = discord.color.red(),
        description = "Some help for keeping the count in line.",
    )
    e.set_footer(text = "Tip: Made you look")
    if section == "Overview":
        e.add_field(
            name = "What is this counting business?",
            value = (
                "Like it's name, counting is just number after number going up by one.\n"
                "This tool simply helps keep it that way and get's rid of the pesky cheaters."
            ),
            inline = False,
        )
        e.add_field(
            name="Quickstart",
            value=(
                f"`!counting setup` — run the interactive setup wizard\n"
                "Then start at `1` in the selected channel. I’ll ✅ correct counts, ❌ on fails."
            ),
            inline=False,
        )
    elif section == "Setup":
        e.add_field(
            name="Setup options",
            value=(
                f"Run `!counting setup` and pick:\n"
                "• Counting channel\n"
                "• Allow bots (on/off)\n"
                "• Starting number\n\n"
                "You can also tweak things manually:\n"
                f"• `!counting setchannel #counting`\n"
                f"• `!counting allowbots true|false`\n"
                f"• `!counting setstart 0`"
            ),
            inline=False,
        )
    elif section == "Gameplay":
        e.add_field(
            name="Rules",
            value=(
                "• Post an integer that is exactly the next number (+1)\n"
                "• No doubles: same user cannot count twice in a row\n"
                "• Wrong number or double resets the chain to **0**"
            ),
            inline=False,
        )
        e.add_field(
            name="Feedback",
            value="• ✅ for correct counts • ❌ + a short explanation on fails",
            inline=False,
        )
        e.add_field(
            name="Leaderboard",
            value=f"`!counting leaderboard` — shows top best streaks",
            inline=False,
        )
    elif section == "Admin":
        e.add_field(
            name="Admin Commands",
            value=(
                f"`!counting setup` — interactive setup wizard\n"
                f"`!counting setchannel <#channel>`\n"
                f"`!counting status`\n"
                f"`!counting reset [start_at]`\n"
                f"`!counting setstart <n>`\n"
                f"`!counting allowbots <true|false>`\n"
            ),
            inline=False,
        )
    return e

class HelpSelect(Select):
    def __init__(self, default_section: str = "Overview"):
        options = [discord.SelectOption(label=s, value=s, default=(s == default_section)) for s in HELP_SECTIONS]
        super().__init__(placeholder="Pick a help section…", options=options, min_values=1, max_values=1)
        

    async def callback(self, interaction: discord.Interaction):
        section = self.values[0]
        await interaction.response.edit_message(embed=_help_embed(section, ), view=self.view)


class HelpView(View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.add_item(HelpSelect())
        
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        perms = interaction.channel.permissions_for(interaction.user) if interaction.channel else None
        return bool(perms and perms.manage_guild)

    @button(label="⟵ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        current = self._current_section_from_embed(interaction.message)
        idx = HELP_SECTIONS.index(current)
        new_section = HELP_SECTIONS[(idx - 1) % len(HELP_SECTIONS)]
        await interaction.response.edit_message(embed=_help_embed(new_section, ), view=self)

    @button(label="Next ⟶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        current = self._current_section_from_embed(interaction.message)
        idx = HELP_SECTIONS.index(current)
        new_section = HELP_SECTIONS[(idx + 1) % len(HELP_SECTIONS)]
        await interaction.response.edit_message(embed=_help_embed(new_section, ), view=self)

    @button(label="Close", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            await interaction.response.edit_message(content="(closed)", embed=None, view=None)

    def _current_section_from_embed(self, message: discord.Message) -> str:
        emb = message.embeds[0] if message.embeds else None
        if not emb:
            return "Overview"
        names = [f.name for f in emb.fields]
        if "What it does" in names:
            return "Overview"
        for s in HELP_SECTIONS:
            if any(s in n for n in names):
                return s
        return "Overview"


class countingSetupView(View):
    def __init__(self, cog: "Counting", ctx: commands.Context, current: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.guild = ctx.guild
        self.author_id = ctx.author.id

        # working values (not yet saved)
        self.channel_id: Optional[int] = current.get("channel_id")
        self.allow_bots: bool = current.get("allow_bots", False)
        self.start_at: int = current.get("last_number", 0)

        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        perms = interaction.channel.permissions_for(interaction.user) if interaction.channel else None
        return bool(perms and perms.manage_guild)

    def _build_embed(self, final: bool = False) -> discord.Embed:
        ch = self.guild.get_channel(self.channel_id) if self.channel_id else None
        e = discord.Embed(
            title="🧮 Counting — Setup Wizard",
            colour=discord.Colour.blurple(),
        )
        if final:
            e.description = "Setup complete. You can rerun this anytime with the setup command."
        else:
            e.description = (
                "Use the buttons below to configure the counting game.\n"
                "When you're happy, press **Save & Close**."
            )

        e.add_field(
            name="Counting channel",
            value=ch.mention if ch else "_not set (required)_",
            inline=False,
        )
        e.add_field(
            name="Allow bots",
            value="✅ Yes" if self.allow_bots else "❌ No",
            inline=True,
        )
        e.add_field(
            name="Starting number",
            value=f"{self.start_at} (next expected will be **{self.start_at + 1}**)",
            inline=True,
        )
        return e

    async def refresh(self):
        if self.message:
            try:
                await self.message.edit(embed=self._build_embed(), view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, Button):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content="Setup timed out. Run the setup command again if you still want to configure counting.",
                    view=self,
                    embed=self._build_embed(final=True),
                )
            except discord.HTTPException:
                pass

    @button(label="Use this channel", style=discord.ButtonStyle.primary, row=0)
    async def set_here(self, interaction: discord.Interaction, button: Button):
        self.channel_id = interaction.channel.id
        await interaction.response.defer()
        await self.refresh()

    @button(label="Toggle allow bots", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_bots(self, interaction: discord.Interaction, button: Button):
        self.allow_bots = not self.allow_bots
        await interaction.response.defer()
        await self.refresh()

    @button(label="Set starting number", style=discord.ButtonStyle.secondary, row=1)
    async def set_start(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Reply with the starting number (>= 0) in this channel within 30 seconds.",
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await self.cog.bot.wait_for("message", check=check, timeout=30.0)
            try:
                value = int(msg.content.strip())
                if value < 0:
                    raise ValueError
            except ValueError:
                await msg.reply("That wasn't a valid non-negative integer. Keeping previous value.", mention_author=False)
            else:
                self.start_at = value
                try:
                    await msg.add_reaction("✅")
                except discord.HTTPException:
                    pass
        except asyncio.TimeoutError:
            # user didn't respond in time; ignore
            pass

        await self.refresh()

    @button(label="Save & Close", style=discord.ButtonStyle.success, row=2)
    async def save_close(self, interaction: discord.Interaction, button: Button):
        if self.channel_id is None:
            await interaction.response.send_message(
                "You need to set a counting channel first (use **Use this channel**).",
                ephemeral=True,
            )
            return

        # Persist config
        await self.cog.config.guild(self.guild).channel_id.set(self.channel_id)
        await self.cog.config.guild(self.guild).allow_bots.set(self.allow_bots)
        await self.cog.config.guild(self.guild).last_number.set(self.start_at)
        await self.cog.config.guild(self.guild).last_user_id.set(None)

        for child in self.children:
            if isinstance(child, Button):
                child.disabled = True

        await interaction.response.send_message("Counting setup saved.", ephemeral=True)
        if self.message:
            try:
                await self.message.edit(embed=self._build_embed(final=True), view=self)
            except discord.HTTPException:
                pass

    @button(label="Cancel", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        for child in self.children:
            if isinstance(child, Button):
                child.disabled = True
        await interaction.response.send_message("Setup cancelled.", ephemeral=True)
        if self.message:
            try:
                await self.message.edit(
                    content="Setup cancelled.",
                    embed=self._build_embed(final=True),
                    view=self,
                )
            except discord.HTTPException:
                pass


class Counting(commands.Cog):
    """+1 counting game with a simple setup wizard and leaderboard."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_ID, force_registration=True)

        default_guild = {
            "channel_id": None,
            "last_number": 0,
            "last_user_id": None,
            "high_score": 0,
            "allow_bots": False,
        }
        default_member = {
            "correct": 0,
            "fails": 0,
            "current_streak": 0,
            "best_streak": 0,
        }
        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

    # -------------- Listener --------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author is None:
            return

        gconf = await self.config.guild(message.guild).all()
        channel_id = gconf["channel_id"]
        if channel_id is None or message.channel.id != channel_id:
            return

        # ignore bots unless explicitly allowed
        if message.author.bot and not gconf.get("allow_bots", False):
            return

        content = message.content.strip()
        try:
            value = int(content)
        except ValueError:
            return  # ignore non-numeric messages

        last_number = gconf["last_number"]
        last_user_id = gconf["last_user_id"]

        # No doubles
        if last_user_id is not None and message.author.id == last_user_id:
            await self._fail(message, reason="double", expected=last_number + 1, given=value)
            return

        expected = last_number + 1
        if value != expected:
            await self._fail(message, reason="wrong", expected=expected, given=value)
            return

        # Success
        await self.config.guild(message.guild).last_number.set(value)
        await self.config.guild(message.guild).last_user_id.set(message.author.id)

        # Member stats
        async with self.config.member(message.author).all() as m:
            m["correct"] += 1
            m["current_streak"] += 1
            if m["current_streak"] > m["best_streak"]:
                m["best_streak"] = m["current_streak"]

        # High score
        if value > gconf["high_score"]:
            await self.config.guild(message.guild).high_score.set(value)

        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

    # -------------- Helpers --------------

    async def _fail(
        self,
        message: discord.Message,
        *,
        reason: str,
        expected: Optional[int] = None,
        given: Optional[int] = None,
    ):
        guild = message.guild

        # reset guild state
        await self.config.guild(guild).last_number.set(0)
        await self.config.guild(guild).last_user_id.set(None)

        # member stats
        async with self.config.member(message.author).all() as m:
            m["fails"] += 1
            m["current_streak"] = 0

        try:
            await message.add_reaction("❌")
        except discord.HTTPException:
            pass

        if reason == "double":
            base = "No doubles (same user twice in a row)."
        else:
            base = f"Expected **{expected}**, but got **{given}**."

        text = f"{base} Count resets to **0**. Start again with `1`."

        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException:
            pass

    # -------------- Commands --------------

    @commands.guild_only()
    @commands.group(name="counting", invoke_without_command=True)
    async def _counting(self, ctx: commands.Context):
        """Base command shows the interactive help menu."""
        view = HelpView(ctx.author.id)
        emb = _help_embed("Overview", )
        await ctx.send(embed=emb, view=view)

    @_counting.command(name="help")
    async def counting_help(self, ctx: commands.Context):
        """Show the interactive help menu."""
        view = HelpView(ctx.author.id)
        emb = _help_embed("Overview", )
        await ctx.send(embed=emb, view=view)

    @_counting.command(name="setup")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_setup(self, ctx: commands.Context):
        """Run an interactive setup wizard with buttons."""
        current = await self.config.guild(ctx.guild).all()
        view = CountingSetupView(self, ctx, current)
        emb = view._build_embed()
        msg = await ctx.send(embed=emb, view=view)
        view.message = msg

    @_counting.command(name="setchannel")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the counting channel manually."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"Counting channel set to {channel.mention}. Start with `1`!")

    @_counting.command(name="status")
    async def counting_status(self, ctx: commands.Context):
        """Show current status and high score."""
        g = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(g["channel_id"]) if g["channel_id"] else None
        last_user = f"<@{g['last_user_id']}>" if g["last_user_id"] else "None"
        await ctx.send(
            f"**Channel:** {ch.mention if ch else 'Not set'}\n"
            f"**Last number:** {g['last_number']}\n"
            f"**Last user:** {last_user}\n"
            f"**High score:** {g['high_score']}\n"
            f"**Allow bots:** {g['allow_bots']}"
        )

    @_counting.command(name="reset")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_reset(self, ctx: commands.Context, start_at: Optional[int] = 0):
        """Reset the count (default 0)."""
        if start_at is None or start_at < 0:
            start_at = 0
        await self.config.guild(ctx.guild).last_number.set(start_at)
        await self.config.guild(ctx.guild).last_user_id.set(None)
        await ctx.send(f"Count reset. Next expected is **{start_at + 1}**.")

    @_counting.command(name="setstart")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_setstart(self, ctx: commands.Context, start_at: int):
        """Set the starting number (next expected will be start+1)."""
        if start_at < 0:
            return await ctx.send("Start must be >= 0.")
        await self.config.guild(ctx.guild).last_number.set(start_at)
        await self.config.guild(ctx.guild).last_user_id.set(None)
        await ctx.send(f"Start set to **{start_at}**. Next expected is **{start_at + 1}**.")

    @_counting.command(name="allowbots")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_allowbots(self, ctx: commands.Context, toggle: bool):
        """Allow or disallow bot accounts to participate."""
        await self.config.guild(ctx.guild).allow_bots.set(bool(toggle))
        await ctx.send(f"Allow bots set to **{bool(toggle)}**.")

    @_counting.command(name="leaderboard")
    async def counting_leaderboard(self, ctx: commands.Context, top: int = 10):
        """Show top members by best streak."""
        data = await self.config.all_members(ctx.guild)
        items = []
        for member_id, stats in data.items():
            best = stats.get("best_streak", 0)
            if best > 0:
                member = ctx.guild.get_member(int(member_id))
                items.append((member.mention if member else f"<@{member_id}>", best))
        if not items:
            return await ctx.send("No data yet.")
        items.sort(key=lambda x: x[1], reverse=True)
        top = max(1, min(top, 25))
        lines = [f"**{i+1}.** {name} — **{score}**" for i, (name, score) in enumerate(items[:top])]
        await ctx.send("__**Best Streaks**__\n" + "\n".join(lines))

    # help hook
    async def format_help_for_context(self, ctx: commands.Context) -> str:
        return (
            "Counting — a +1 counting game with a simple setup wizard.\n"
            f"Try `!counting setup` to configure it."
        )
