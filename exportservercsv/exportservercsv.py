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
        writer = csv.writer(roles_buffer, delimiter = ",")
        writer.writerow(["Role ID", "Role Name", "Position"])
        for role in reversed(guild.roles): # make it print top to bottom not bottom to top
            writer.writerow([
                str(role.id),
                role.name,
                role.position
            ])
        roles_buffer.seek(0)
        
        channels_buffer = io.StringIO()
        writer = csv.writer(channels_buffer, delimiter = ",") # make the columns easy to read
        writer.writerow(["Category Name", "Channel ID", "Channel Name", "Channel Type"])
        for channel in reversed(guild.channels):
            writer.writerow([
                channel.category.name if channel.category else "N/A",
                str(channel.id),
                channel.name,
                str(channel.type)
            ])
        channels_buffer.seek(0)
        
        await ctx.send(
            "Export Complete! Please enjoy these files equally!",
            files = [
                discord.File(io.BytesIO(roles_buffer.getvalue().encode("utf-8-sig")), filename = "tlgroles.csv"),
                discord.File(io.BytesIO(channels_buffer.getvalue().encode("utf-8-sig")), filename = "tlgchannels.csv")
            ]
        )