from .tlembed import TLEmbed

async def setup(bot):
    await bot.add_cog(TLEmbed(bot))