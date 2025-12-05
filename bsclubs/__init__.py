from .bsclubs import BrawlStarsClubs


async def setup(bot):
    await bot.add_cog(BrawlStarsClubs(bot))
