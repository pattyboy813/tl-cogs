from .modlogv2 import ModLogX

async def setup(bot):
    await bot.add_cog(ModLogX(bot))

