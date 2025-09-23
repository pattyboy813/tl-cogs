from .counting import Counting


async def setup(bot):
    cog = Counting(bot)
    await bot.add_cog(cog)
