from __future__ import annotations

import random
import time
from typing import Optional, Dict, List

import discord
from discord.ui import View, Select, button, Button
from redbot.core import commands, Config

CONF_ID = 9876543210123456  # change to any random large int you own

# -----------------------
# Quip Pools (tiered)
# 0=nice, 1=cheeky, 2=snarky, 3=roast (playful, not hateful)
# -----------------------

# -----------------------
# Quip Pools (tiered)
# 0=nice, 1=cheeky, 2=snarky, 3=roast (clear + playful)
# -----------------------

PRAISE: Dict[int, List[str]] = {
    0: [
        "Great job, {user}! Keep it rolling.",
        "Clean count. Love to see it.",
        "Solid work—math is proud of you.",
        "Nice and tidy. {count} achieved.",
    ],
    1: [
        "Look at you, counting like a functional adult.",
        "Sharp work, {user}.",
        "That’s the stuff. Onward.",
    ],
    2: [
        "Numbers wish they were as consistent as you.",
        "Calculator not required. Impressive.",
        "Textbook execution.",
    ],
    3: [
        "You didn’t just count—you owned that number.",
        "Crisp. Fast. Correct. Next.",
        "{user}, that was clean. Keep pushing.",
    ],
}

FAIL_WRONG: Dict[int, List[str]] = {
    0: [
        "Whoops—needed **{expected}**, not **{given}**. Reset to **0**. Start at `1`.",
        "Close! Expected **{expected}**. Back to **0**. Start at `1`.",
    ],
    1: [
        "Almost—answer was **{expected}**. Reset to **0**. Start at `1`.",
        "Bold choice: **{given}**. Correct: **{expected}**. Reset to **0**.",
    ],
    2: [
        "We needed **{expected}**. You sent **{given}**. Reset to **0**. Start at `1`.",
        "Wrong number: wanted **{expected}**. Back to **0**. Start at `1`.",
    ],
    3: [
        "Not it. Expected **{expected}**. Count resets to **0**. Start at `1`.",
        "Try again: the next number was **{expected}**. We’re back at **0**.",
    ],
}

FAIL_DOUBLE: Dict[int, List[str]] = {
    0: [
        "No doubles—share the fun. Reset to **0**. Start at `1`.",
        "Same person twice breaks the chain. Back to **0**. Start at `1`.",
    ],
    1: [
        "Back-to-back isn’t allowed. Let someone else go. Reset to **0**.",
        "This isn’t solitaire. No doubles. Restart at `1`.",
    ],
    2: [
        "Hold up—no doubles, {user}. Reset to **0**. Wait one turn.",
        "Tag in a teammate next time. Reset to **0**. Start at `1`.",
    ],
    3: [
        "Two in a row isn’t allowed. Reset to **0**. Let someone else take `1`.",
        "No doubles, {user}. Chain resets to **0**. Wait a turn, then jump in.",
    ],
}


def choose_quip(pool: Dict[int, List[str]], level: int) -> str:
    level = max(0, min(3, level))
    return random.choice(pool[level])

# -----------------------
# Pretty Help UI
# -----------------------

HELP_SECTIONS = [
    "Overview",
    "Setup",
    "Gameplay",
    "Admin",
    "Sass Controls",
    "Custom Quips",
]

def _help_embed(section: str, prefix: str) -> discord.Embed:
    e = discord.Embed(
        title="🧮 Counting — Help",
        colour=discord.Colour.blurple(),
        description="A clean, fast counting game with optional sass. Use the selector below to browse sections.",
    )
    e.set_footer(text="Tip: You can run commands from here by copy/paste.")
    if section == "Overview":
        e.add_field(
            name="What it does",
            value=(
                "• Enforces +1 counting in a chosen channel\n"
                "• Blocks doubles (same user twice)\n"
                "• Tracks highscores and streaks\n"
                "• Optional ‘smartass’ mode with tiered quips"
            ),
            inline=False,
        )
        e.add_field(
            name="Quickstart",
            value=(
                f"`{prefix}counting setchannel #counting`\n"
                "Start at `1` in that channel. I’ll ✅ correct counts, ❌ fails."
            ),
            inline=False,
        )
    elif section == "Setup":
        e.add_field(
            name="Initial setup",
            value=(
                f"• Set channel: `{prefix}counting setchannel #counting`\n"
                f"• Allow bots (optional): `{prefix}counting allowbots true|false`\n"
                f"• Set starting number: `{prefix}counting setstart 0`"
            ),
            inline=False,
        )
        e.add_field(
            name="Status & Reset",
            value=(
                f"• Status: `{prefix}counting status`\n"
                f"• Reset: `{prefix}counting reset`  → next expected becomes **1**\n"
                f"• Reset to N: `{prefix}counting reset 50` → next expected is **51**"
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
            value="• ✅ for correct counts • ❌ + cheeky reply on fails (if smartass is enabled)",
            inline=False,
        )
        e.add_field(
            name="Leaderboard",
            value=f"`{prefix}counting leaderboard` — shows top best streaks",
            inline=False,
        )
    elif section == "Admin":
        e.add_field(
            name="Admin Commands",
            value=(
                f"`{prefix}counting setchannel <#channel>`\n"
                f"`{prefix}counting status`\n"
                f"`{prefix}counting reset [start_at]`\n"
                f"`{prefix}counting setstart <n>`\n"
                f"`{prefix}counting allowbots <true|false>`"
            ),
            inline=False,
        )
    elif section == "Sass Controls":
        e.add_field(
            name="Smartass Mode",
            value=(
                f"`{prefix}counting smartass <true|false>` — turn quips on/off\n"
                f"`{prefix}counting sasslevel <0|1|2|3>` — 0=nice, 3=roast-cap\n"
                f"`{prefix}counting allowroast <true|false>` — allow tier-3 roasts\n"
                f"`{prefix}counting praiserate <N>` — 1 in N successes praised (min 2)\n"
                f"`{prefix}counting roastrate <N>` — 1 in N fails may roast (min 1)\n"
                f"`{prefix}counting gentlefirst <N>` — keep early game ≤ cheeky for first N"
            ),
            inline=False,
        )
    elif section == "Custom Quips":
        e.add_field(
            name="Add your own lines",
            value=(
                f"`{prefix}counting quips addpraise <line>`  — vars: `{{user}}`, `{{count}}`\n"
                f"`{prefix}counting quips addfailwrong <line>` — vars: `{{user}}`, `{{expected}}`, `{{given}}`\n"
                f"`{prefix}counting quips addfaildouble <line>` — vars: `{{user}}`\n"
                f"`{prefix}counting quips list`\n"
                f"`{prefix}counting quips clear <praise|wrong|double>`"
            ),
            inline=False,
        )
    return e

class HelpSelect(Select):
    def __init__(self, prefix: str, default_section: str = "Overview"):
        options = [discord.SelectOption(label=s, value=s, default=(s == default_section)) for s in HELP_SECTIONS]
        super().__init__(placeholder="Pick a help section…", options=options, min_values=1, max_values=1)
        self.prefix = prefix

    async def callback(self, interaction: discord.Interaction):
        section = self.values[0]
        await interaction.response.edit_message(embed=_help_embed(section, self.prefix), view=self.view)

class HelpView(View):
    def __init__(self, prefix: str, author_id: int):
        super().__init__(timeout=120)
        self.add_item(HelpSelect(prefix))
        self.prefix = prefix
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
        await interaction.response.edit_message(embed=_help_embed(new_section, self.prefix), view=self)

    @button(label="Next ⟶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        current = self._current_section_from_embed(interaction.message)
        idx = HELP_SECTIONS.index(current)
        new_section = HELP_SECTIONS[(idx + 1) % len(HELP_SECTIONS)]
        await interaction.response.edit_message(embed=_help_embed(new_section, self.prefix), view=self)

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
        # Guess based on field names present
        names = [f.name for f in emb.fields]
        if "What it does" in names:
            return "Overview"
        for s in HELP_SECTIONS:
            if any(s in n for n in names):
                return s
        return "Overview"

# -----------------------
# Cog
# -----------------------

class Counting(commands.Cog):
    """+1 counting game with tiered smartass mode (nice → roast) and a pretty help menu."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=CONF_ID, force_registration=True)

        default_guild = {
            "channel_id": None,
            "last_number": 0,
            "last_user_id": None,
            "high_score": 0,
            "allow_bots": False,

            # Sass controls
            "smartass": True,        # global toggle for quips
            "sass_level": 2,         # 0..3 upper bound
            "allow_roast": True,     # allow tier-3 lines
            "praise_rate": 12,       # 1 in N correct counts
            "roast_rate": 1,         # 1 in N fails may use roast (min 1)
            "gentle_first_n": 10,    # first N expected numbers: stay ≤ cheeky

            # Custom quips (persistence)
            "custom_praise": [],
            "custom_fail_wrong": [],
            "custom_fail_double": [],
        }
        default_member = {
            "correct": 0,
            "fails": 0,
            "current_streak": 0,
            "best_streak": 0,
            # fail escalation
            "recent_fail_count": 0,
            "recent_fail_ts": 0.0,
        }
        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

    # -------------- Listener --------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author is None:
            return

        # ignore bots unless explicitly allowed
        allow_bots = await self.config.guild(message.guild).allow_bots()
        if message.author.bot and not allow_bots:
            return

        gconf = await self.config.guild(message.guild).all()
        channel_id = gconf["channel_id"]
        if channel_id is None or message.channel.id != channel_id:
            return

        content = message.content.strip()
        try:
            value = int(content)
        except ValueError:
            return  # ignore non-numeric messages

        # state
        last_number = gconf["last_number"]
        last_user_id = gconf["last_user_id"]

        # No doubles
        if last_user_id is not None and message.author.id == last_user_id:
            await self._fail(message, reason="double")
            return

        # Must be +1
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
            m["recent_fail_count"] = 0
            m["recent_fail_ts"] = 0.0
            if m["current_streak"] > m["best_streak"]:
                m["best_streak"] = m["current_streak"]

        # High score
        if value > gconf["high_score"]:
            await self.config.guild(message.guild).high_score.set(value)

        # React to acknowledge
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        # Praise (smartass)
        if gconf.get("smartass", True):
            rate = max(2, int(gconf.get("praise_rate", 12)))
            gentle_ceiling = 1 if expected <= int(gconf.get("gentle_first_n", 10)) else gconf.get("sass_level", 2)
            if random.randint(1, rate) == 1:
                level_cap = min(3 if gconf.get("allow_roast", True) else 2, int(gentle_ceiling))
                weights = [4, 3, 2, 1]  # bias to nicer tones
                pool_levels = list(range(0, level_cap + 1))
                pool_weights = weights[: level_cap + 1]
                level = random.choices(pool_levels, weights=pool_weights, k=1)[0]
                line = random.choice(PRAISE[level] + gconf.get("custom_praise", []))
                try:
                    await message.reply(
                        line.format(user=message.author.mention, count=expected),
                        mention_author=False,
                    
                    )
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
        gconf = await self.config.guild(guild).all()

        # increment member fails and reset streak; track recent fails (10 min window)
        now = time.time()
        async with self.config.member(message.author).all() as m:
            m["fails"] += 1
            m["current_streak"] = 0
            if now - float(m.get("recent_fail_ts", 0)) <= 600:
                m["recent_fail_count"] = int(m.get("recent_fail_count", 0)) + 1
            else:
                m["recent_fail_count"] = 1
            m["recent_fail_ts"] = now
            recent_fails = m["recent_fail_count"]

        await self.config.guild(guild).last_number.set(0)
        await self.config.guild(guild).last_user_id.set(None)

        try:
            await message.add_reaction("❌")
        except discord.HTTPException:
            pass

        # Smartass reply
        if gconf.get("smartass", True):
            base_level = int(gconf.get("sass_level", 2))
            esc = 1 if recent_fails >= 2 else 0
            level_cap = 3 if gconf.get("allow_roast", True) else 2
            gentle_cap = 1 if (expected or 0) <= int(gconf.get("gentle_first_n", 10)) else 3
            level = min(base_level + esc, level_cap, gentle_cap)

            if level_cap == 3 and random.randint(1, max(1, int(gconf.get("roast_rate", 1)))) == 1:
                level = min(3, level)

            if reason == "double":
                line = choose_quip(FAIL_DOUBLE, level)
                extras = gconf.get("custom_fail_double", [])
                if extras and random.randint(1, 3) == 1:
                    line = random.choice(extras + [line])
                text = line.format(user=message.author.mention)
            else:
                line = choose_quip(FAIL_WRONG, level)
                extras = gconf.get("custom_fail_wrong", [])
                if extras and random.randint(1, 3) == 1:
                    line = random.choice(extras + [line])
                text = line.format(user=message.author.mention, expected=expected, given=given)
        else:
            base = f"Expected **{expected}**." if expected is not None else "No doubles (same user twice)."
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
        # Show the pretty help when invoked as just `!counting`
        prefix = ctx.clean_prefix if hasattr(ctx, "clean_prefix") else (ctx.prefix or "!")
        view = HelpView(prefix, ctx.author.id)
        emb = _help_embed("Overview", prefix)
        await ctx.send(embed=emb, view=view)

    @_counting.command(name="help")
    async def counting_help(self, ctx: commands.Context):
        """Show the interactive help menu."""
        prefix = ctx.clean_prefix if hasattr(ctx, "clean_prefix") else (ctx.prefix or "!")
        view = HelpView(prefix, ctx.author.id)
        emb = _help_embed("Overview", prefix)
        await ctx.send(embed=emb, view=view)

    @_counting.command(name="setchannel")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the counting channel."""
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
            f"**Allow bots:** {g['allow_bots']}\n"
            f"**Smartass:** {g.get('smartass', True)}\n"
            f"**Sass level:** {g.get('sass_level', 2)} (0 nice → 3 roast-cap)\n"
            f"**Allow roast:** {g.get('allow_roast', True)}\n"
            f"**Praise rate:** 1/{max(2, int(g.get('praise_rate', 12)))}\n"
            f"**Roast rate (fails):** 1/{max(1, int(g.get('roast_rate', 1)))}\n"
            f"**Gentle first N:** {int(g.get('gentle_first_n', 10))}"
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

    # ------- Sass controls -------
    @_counting.command(name="smartass")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_smartass(self, ctx: commands.Context, toggle: bool):
        """Enable/disable smartass mode."""
        await self.config.guild(ctx.guild).smartass.set(bool(toggle))
        await ctx.send(f"Smartass mode set to **{bool(toggle)}**.")

    @_counting.command(name="sasslevel")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_sasslevel(self, ctx: commands.Context, level: int):
        """Set sass level (0=nice, 1=cheeky, 2=snarky, 3=roast-cap)."""
        level = max(0, min(3, int(level)))
        await self.config.guild(ctx.guild).sass_level.set(level)
        await ctx.send(f"Sass level set to **{level}**.")

    @_counting.command(name="allowroast")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_allowroast(self, ctx: commands.Context, toggle: bool):
        """Allow tier-3 'roast' lines (disable if your server prefers gentle)."""
        await self.config.guild(ctx.guild).allow_roast.set(bool(toggle))
        await ctx.send(f"Allow roast set to **{bool(toggle)}**.")

    @_counting.command(name="praiserate")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_praiserate(self, ctx: commands.Context, one_in_n: int):
        """Set praise frequency; 1 in N correct counts gets a praise line (min 2)."""
        one_in_n = max(2, int(one_in_n))
        await self.config.guild(ctx.guild).praise_rate.set(one_in_n)
        await ctx.send(f"Praise rate set to **1/{one_in_n}**.")

    @_counting.command(name="roastrate")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_roastrate(self, ctx: commands.Context, one_in_n: int):
        """Set roast consideration on fails; 1 in N fails may use roast tier (min 1 = always)."""
        one_in_n = max(1, int(one_in_n))
        await self.config.guild(ctx.guild).roast_rate.set(one_in_n)
        await ctx.send(f"Roast rate (fails) set to **1/{one_in_n}**.")

    @_counting.command(name="gentlefirst")
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_gentlefirst(self, ctx: commands.Context, n: int):
        """Keep early game ≤ cheeky for the first N expected numbers (default 10)."""
        n = max(0, int(n))
        await self.config.guild(ctx.guild).gentle_first_n.set(n)
        await ctx.send(f"Gentle-first window set to **{n}**.")

    # ------- Custom quips -------
    @_counting.group(name="quips", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def counting_quips(self, ctx: commands.Context):
        """Manage custom quips: add your own lines."""
        await ctx.send_help()

    @counting_quips.command(name="addpraise")
    @commands.has_guild_permissions(manage_guild=True)
    async def quips_addpraise(self, ctx: commands.Context, *, line: str):
        """Add a custom praise line. Vars: {user}, {count}."""
        async with self.config.guild(ctx.guild).custom_praise() as arr:
            arr.append(line.strip())
        await ctx.send("Added custom praise line.")

    @counting_quips.command(name="addfailwrong")
    @commands.has_guild_permissions(manage_guild=True)
    async def quips_addfailwrong(self, ctx: commands.Context, *, line: str):
        """Add a custom wrong-number line. Vars: {user}, {expected}, {given}."""
        async with self.config.guild(ctx.guild).custom_fail_wrong() as arr:
            arr.append(line.strip())
        await ctx.send("Added custom wrong-number line.")

    @counting_quips.command(name="addfaildouble")
    @commands.has_guild_permissions(manage_guild=True)
    async def quips_addfaildouble(self, ctx: commands.Context, *, line: str):
        """Add a custom double-post line. Vars: {user}."""
        async with self.config.guild(ctx.guild).custom_fail_double() as arr:
            arr.append(line.strip())
        await ctx.send("Added custom double-post line.")

    @counting_quips.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def quips_list(self, ctx: commands.Context):
        """List custom quips."""
        g = await self.config.guild(ctx.guild).all()
        def fmt(arr): return "\n".join(f"- {x}" for x in arr) if arr else "_none_"
        await ctx.send(
            "**Custom Praise:**\n"
            f"{fmt(g.get('custom_praise', []))}\n\n"
            "**Custom Fail (wrong):**\n"
            f"{fmt(g.get('custom_fail_wrong', []))}\n\n"
            "**Custom Fail (double):**\n"
            f"{fmt(g.get('custom_fail_double', []))}"
        )

    @counting_quips.command(name="clear")
    @commands.has_guild_permissions(manage_guild=True)
    async def quips_clear(self, ctx: commands.Context, category: str):
        """Clear a category: praise | wrong | double"""
        category = category.lower().strip()
        key = {
            "praise": "custom_praise",
            "wrong": "custom_fail_wrong",
            "double": "custom_fail_double",
        }.get(category)
        if not key:
            return await ctx.send("Pick one: `praise`, `wrong`, or `double`.")
        await self.config.guild(ctx.guild).set_raw(key, value=[])
        await ctx.send(f"Cleared custom {category} quips.")

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

    # -------------- Help hook --------------

    async def format_help_for_context(self, ctx: commands.Context) -> str:
        prefix = ctx.clean_prefix if hasattr(ctx, "clean_prefix") else (ctx.prefix or "!")
        return (
            "Counting — a +1 counting game with optional sass.\n"
            f"Try `{prefix}counting` for the interactive help menu."
        )
