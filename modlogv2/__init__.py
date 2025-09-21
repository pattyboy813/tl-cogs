from .modlogv2 import ModLogV2

async def setup(bot):
    await bot.add_cog(ModLogV2(bot))
