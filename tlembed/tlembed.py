import discord
from redbot.core import commands

class TLEmbed(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command()
    @commands.admin()
    async def embed(self, ctx):
        e1 = discord.Embed(
            title = "Counting Updated!",
            color = 0x5865F2
        )
        e2 = discord.Embed(
            color = 0x5865F2
        )
        e2.add_field(
            name = "Mess up the count? No problem!",
            value = "We all make mistakes, however we also need to learn from them. So we'll give you ~~10 minutes~~ 28 dayss to reflect on your mistake for free!\n- Courtesy of Discord Timeout",
            inline = False
        )
        e2.set_footer(text = "With love, Threat Level Gaming")

        await ctx.send(embed = e1)
        await ctx.send(embed = e2)
        await ctx.send(embed = e3)
