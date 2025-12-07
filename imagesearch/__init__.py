from .imagesearcher import ImageSearcher
async def setup(bot):
    await bot.add_cog(ImageSearcher(bot))