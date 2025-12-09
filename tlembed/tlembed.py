import discord
from redbot.core import commands

class TLEmbed(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command()
    @commands.admin()
    async def embed(self, ctx):
        e1 = discord.Embed(
            title = "Looking for a club? Join here!",
            color = 0x5865F2
        )
        e1.add_field(
            name = "What we need from you!",
            value = "  - Your Player Tag (eg. #LYRG2L2U)\n  - A clear screenshot of your profile (example below)",
            inline = False
        )
        e1.set_image(url='https://cdn.discordapp.com/attachments/1344186242115305495/1447873735074775050/IMG_0789.png?ex=693934fe&is=6937e37e&hm=0402269be0a0fb648ba515336cd41f0db6bd0e525887cb0fe564c3630c010362&')

        e2 = discord.Embed(
            color = 0x5865F2
        )
        e2.add_field(
            name = "How it works",
            value = "1. Run `!clubapply` below and check your DM's\n2. Provide the bot the screenshot of your profile and player tag when prompted\n  - The bot will then contact the Brawl Stars servers to pull your details\n3. Verify your details are correct and go from there!",
            inline = False
        )
        e2.add_field(
            name = "What happens then?",
            value = "The bot will open a thread attached to this channel and ping our Brawl Stars Staff to get you into a club. Once in a club, our leadership run 1 command and the rest is done by the bot.",
            inline =  False
        )

        await ctx.send(embed = e1)
        await ctx.send(embed = e2)
