from .therevamper import RevampSync

async def setup(bot):
    await bot.add_cog(RevampSync(bot))

