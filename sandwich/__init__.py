from .sandwich import sandwich

async def setup(bot):
    await bot.add_cog(sandwich(bot))