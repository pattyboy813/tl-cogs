from .brawlstarsmanager import BrawlStarsManager

async def setup(bot):
    await bot.add_cog(BrawlStarsManager(bot))