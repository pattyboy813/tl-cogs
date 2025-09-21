from .modlogv2 import ModLogV2

async def setup(bot):
    cog = ModLogV2(bot)
    await bot.add_cog(cog)
