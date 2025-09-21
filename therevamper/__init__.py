from .therevamper import TheRevamper

async def setup(bot):
    await bot.add_cog(TheRevamper(bot))