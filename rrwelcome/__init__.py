from .rrwelcome import ReactRoleWelcome

async def setup(bot):
    await bot.add_cog(ReactRoleWelcome(bot))