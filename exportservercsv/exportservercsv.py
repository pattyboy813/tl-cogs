import discord
from redbot.core import commands
import csv
import io

class ExportServerCSV(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.guild_only()
    @commands.admin()
    async def exportdata(self, ctx):
        guild = ctx.guild

        roles_buffer = io.StringIO()
        writer = csv.writer(roles_buffer)
        writer.writerow(["Role ID", "Role Name", "Position"])
        for role in guild.roles:
            writer.writerow([
                role.id,
                role.name,
                role.position
            ])
        roles_buffer.seek(0)
        
        channels_buffer = io.StringIO()
        writer = csv.writer(channels_buffer)
        writer.writerow(["Category Name", "Channel ID", "Channel Name", "Channel Type"])
        for channel in guild.channels:
            writer.writerow([
                channel.category.name if channel.category else "N/A",
                channel.id,
                channel.name,
                str(channel.type)
            ])
        channels_buffer.seek(0)
        
        await ctx.send(
            "Export Complete! Please enjoy these files equally!",
            files = [
                discord.File(io.BytesIO(roles_buffer.getvalue().encode()), filename = "tlgroles.csv"),
                discord.File(io.BytesIO(channels_buffer.getvalue().encode()), filename = "tlgchannels.csv")
            ]
        )