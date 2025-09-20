from .modplus import ModPlus


async def setup(bot):
    await bot.add_cog(ModPlus(bot))
