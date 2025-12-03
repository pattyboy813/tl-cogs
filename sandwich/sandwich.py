from __future__ import annotations

import datetime
import random
import calendar

import discord
from discord.ext import tasks
from redbot.core import commands, Config
from redbot.core.bot import Red



# Duane's userID
duaneuid = 506325350335119360


class sandwich(commands.Cog):
    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xA1B2C3D4, force_registration=True
        )

        default_guild = {
            "channel_id": None,
            "current_month": None,
            "sent_this_month": 0,
        }

        self.config.register_guild(**default_guild)
        self.monthly_loop.start()

    def cog_unload(self):
        self.monthly_loop.cancel()

    # Background task runs once a day
    @tasks.loop(hours=24)
    async def monthly_loop(self):
        today = datetime.datetime.utcnow()
        current_month = today.strftime("%Y-%m")
        _, days_in_month = calendar.monthrange(today.year, today.month)
        day_of_month = today.day

        guild_data = await self.config.all_guilds()

        for guild_id, data in guild_data.items():
            channel_id = data.get("channel_id")
            if not channel_id:
                continue

            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            user = guild.get_member(duaneuid)
            if not channel or not user:
                continue

            stored_month = data.get("current_month")
            sent = int(data.get("sent_this_month", 0))

            # Reset on new month
            if stored_month != current_month:
                sent = 0
                await self.config.guild(guild).current_month.set(current_month)
                await self.config.guild(guild).sent_this_month.set(0)

            if sent >= 3:
                continue

            remaining_days = days_in_month - day_of_month + 1
            remaining_needed = 3 - sent
            probability = remaining_needed / remaining_days

            if random.random() <= probability:
                try:
                    await channel.send(f"{user.mention} go make a sandwich.")
                except discord.HTTPException:
                    pass

                sent += 1
                await self.config.guild(guild).sent_this_month.set(sent)

    @monthly_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_red_ready()

    @commands.command()
    @commands.guild_only()
    async def sandwich(self, ctx: commands.Context):
        """Sets the channel where the sandwich reminders will be sent."""
        await self.config.guild(ctx.guild).channel_id.set(ctx.channel.id)

        now = datetime.datetime.utcnow().strftime("%Y-%m")
        await self.config.guild(ctx.guild).current_month.set(now)
        await self.config.guild(ctx.guild).sent_this_month.set(0)

        await ctx.send(
            f"Okay, I'll remind <@{duaneuid}> **3 times a month** to go make a sandwich"
        )



