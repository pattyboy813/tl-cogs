from redbot.core.bot import Red

from .modplus import ModPlus


async def setup(bot: Red) -> None:
    await bot.add_cog(ModPlus(bot))