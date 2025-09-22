from .therevamper import RevampSync

async def setup(bot: commands.Bot):
    await bot.add_cog(RevampSync(bot))
