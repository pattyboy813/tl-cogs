from .ai import AI

async def setup(bot):
    await bot.add_cog(AI(bot))