from redbot.core.bot import Red
from .brawlerimport import BSEmoji


async def setup(bot: Red):
    await bot.add_cog(BSEmoji(bot))
