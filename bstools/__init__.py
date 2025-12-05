from .bstools import BrawlStarsTools

async def setup(bot):
    await bot.add_cog(BrawlStarsTools(bot))
