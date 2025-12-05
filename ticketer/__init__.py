from .ticketer import ticketcost

async def setup(bot):
    await bot.add_cog(Tickets(bot))