from .cog import BrawlStarsTools  # noqa: F401

async def setup(bot):
    await bot.add_cog(BrawlStarsTools(bot))