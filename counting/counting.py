from __future__ import annotations
import asyncio
from typing import Optional, Dict, List
import re
import ast
import operator
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import commands, Config

CONF_ID = 347209384723923874


class Counting(commands.Cog):
    """+1 counting game with stats and optional bot participation."""

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
        if not content:
            return

        value = self._parse_number(content)
        if value is None:
            # ignore messages that don't clearly represent a number
            return

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

    # -------------- Parsing Helpers --------------

    def _parse_number(self, content: str) -> Optional[int]:
        """
        Try to interpret content as:
        - plain integer: "5"
        - math expression: "2+3", "(10/2)+1", "7*3"
        - written number: "three", "twenty one", "one hundred and five"
        """
        s = content.strip()

        # 1) direct integer
        try:
            return int(s)
        except ValueError:
            pass

        # 2) math expression
        expr_source = s
        # If user writes "1+2=3", take the part after '=' as the "answer" (3)
        if "=" in expr_source:
            expr_source = expr_source.split("=")[-1].strip()

        math_val = self._parse_math(expr_source)
        if math_val is not None:
            return math_val

        # 3) number words
        word_val = self._parse_word_number(s.lower())
        return word_val

    def _parse_math(self, expr: str) -> Optional[int]:
        """Safely evaluate a simple arithmetic expression and return an int if possible."""
        # Hard guard: only allow digits, whitespace, + - * / % ( ) and dots
        if not re.fullmatch(r"[0-9\s\+\-\*\/\%\(\)\.]+", expr):
            return None

        try:
            node = ast.parse(expr, mode="eval")
        except SyntaxError:
            return None

        allowed_operators = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod,
            ast.Pow: operator.pow,
        }

        def _eval(n):
            if isinstance(n, ast.Expression):
                return _eval(n.body)
            if isinstance(n, ast.Constant):
                if isinstance(n.value, (int, float)):
                    return n.value
                raise ValueError
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
                return -_eval(n.operand)
            if isinstance(n, ast.BinOp) and type(n.op) in allowed_operators:
                left = _eval(n.left)
                right = _eval(n.right)
                return allowed_operators[type(n.op)](left, right)
            raise ValueError

        try:
            result = _eval(node)
        except Exception:
            return None

        # Coerce to int if it's "really" an int
        if isinstance(result, (int, float)):
            int_result = int(round(result))
            if abs(result - int_result) < 1e-9:
                return int_result
        return None

    def _parse_word_number(self, text: str) -> Optional[int]:
        """
        Parse a basic English number phrase like:
        "zero", "ten", "twenty one", "one hundred and five", "two thousand three"
        """
        units = {
            "zero": 0,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
            "thirteen": 13,
            "fourteen": 14,
            "fifteen": 15,
            "sixteen": 16,
            "seventeen": 17,
            "eighteen": 18,
            "nineteen": 19,
        }
        tens = {
            "twenty": 20,
            "thirty": 30,
            "forty": 40,
            "fifty": 50,
            "sixty": 60,
            "seventy": 70,
            "eighty": 80,
            "ninety": 90,
        }
        scales = {
            "hundred": 100,
            "thousand": 1000,
            "million": 1_000_000,
        }

        tokens = re.split(r"[\s\-]+", text)
        if not tokens:
            return None

        current = 0
        total = 0
        used_any = False

        for word in tokens:
            if word == "and":
                continue
            if word in units:
                current += units[word]
                used_any = True
            elif word in tens:
                current += tens[word]
                used_any = True
            elif word in scales:
                if current == 0:
                    current = 1
                current *= scales[word]
                total += current
                current = 0
                used_any = True
            else:
                # Not a recognizable number word
                return None

        if not used_any:
            return None
        return total + current

    # -------------- Fail Helper --------------

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

        # Try to timeout the user for 10 minutes
        timed_out = False
        try:
            until = datetime.now(timezone.utc) + timedelta(minutes=10)
            await message.author.edit(timed_out_until=until, reason="Failed the counting game.")
            timed_out = True
        except (discord.Forbidden, discord.HTTPException):
            # Bot doesn't have permission or can't modify this member
            timed_out = False

        if reason == "double":
            base = "No doubles (same user twice in a row)."
        else:
            base = f"Expected **{expected}**, but got **{given}**."

        text = f"{base} Count resets to **0**. Start again with `1`."
        if timed_out:
            text += " You have been timed out for **10 minutes**."

        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException:
            pass

    # -------------- Commands --------------

    @commands.guild_only()
    @commands.group(name="counting", invoke_without_command=True)
    async def _counting(self, ctx: commands.Context):
        """Base command for the counting game."""
        # Default behaviour: show status
        await ctx.invoke(self.counting_status)

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
