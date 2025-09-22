from .modlogv2 import ModLogV2

async def setup(bot):
    # refuse to load if anything already owns "modlog"
    if bot.get_command("modlog"):
        raise RuntimeError("ModLogV2: 'modlog' already exists (likely another cog). Unload it first.")
    await bot.add_cog(ModLogV2(bot))
