from .modlogx import ModLogX

async def setup(bot):
    await bot.add_cog(ModLogX(bot))

