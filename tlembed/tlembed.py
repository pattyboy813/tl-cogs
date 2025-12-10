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
            description = "Now with 40% more ~~punishments~~ enforcement on keeping the count correct!",
            color = 0x5865F2
        )
        e2 = discord.Embed(
            color = 0x5865F2
        )
        e2.add_field(
            name = "Mess up the count? No problem!",
            value = "We all make mistakes, however we also need to learn from them. So we'll give you 10 minutes to reflect on your mistake for free!\n- Courtesy of Discord Timeout",
            inline = False
        )
        e2.add_field(
            name = "Math without letters just wasn't fun, so we fixed it!",
            value = "Counting with just numbers is so 2016, so now you can type out your numbers for the ultimate experience! (eg. six million seven hundred and sixty seven thousand six hundred and seven)",
            inline = False
        )
        e2.add_field(
            name = "Advanced math for our nerdy counters.",
            value = "Love typing out math? Well we've got you covered. Now you can do 1*1, 1/1, 1+1 and 1-1 and so much more!",
            inline =  False
        )
        e3 = discord.Embed(
            color = 0x5865F2
        )
        e3.add_field(
            name = "Got any suggestions for features in the count?",
            value = "Ping Pat, he'll get around to it somewhere in the near future.",
            inline = False
        )
        e3.set_footer(text = "With love, Threat Level Gaming")

        await ctx.send(embed = e1)
        await ctx.send(embed = e2)
        await ctx.send(embed = e3)
