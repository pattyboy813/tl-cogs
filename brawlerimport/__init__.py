from redbot.core.bot import Red
from .bsemoji import BSEmoji


async def setup(bot: Red):
    await bot.add_cog(BSEmoji(bot))
