import discord
from redbot.core import commands
from redbot.core.bot import Red

class ReactRoleWelcome(commands.Cog):
    def __init__(self, bot: Red):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(ReactRoleView()) # Makes button persistent through restarts
    
    @commands.command()
    @commands.admin()
    async def welcome(self, ctx):
        e = discord.Embed(
            title = "Welcome to Threat Level!",
            description = "Please read the following carefully to avoid your removal from the server!\n"
            "To help us keep the inactive members out of your server, you'll need to select a role(s) from the ones available to avoid being kicked. If you don't select anything, you'll be kicked (not banned) after one week.\n"
            "If you experience any issues with this, feel free to ping an online staff member to get you into our server.\n"
            "Note: Clicking the first 3 options will also open up the server. So you won't need to click 'Join a club' and 'Join the Community' at the same time!"
        )
        e.add_field(name = "Want to join a Clash Royale clan?", value = "Click 'Join a Clan!' below to be taken to our recruitment channel.", inline = False)
        e.add_field(name = "Want to join a Brawl Stars club?", value = "Click 'Join a Club!' below to be taken our recruitment channel.", inline = False)
        e.add_field(name = "Looking for a Minecraft Server to join?", value = "Click 'Join the Craft!' below to be taken to the UTS Minecraft Server information channel.", inline = False)
        e.add_field(name = "Just looking to be apart of our community?", value = "Click 'Join the community!' below to open up our server!", inline = False)

        e.set_footer(text = "Threat Level - 2025")

        view = ReactRoleView()

        await ctx.send(embed = e, view = view)

class ReactRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(ReactRoleButton("Join a Clan!", [1405139477705261056, 1418925222345707644]))
        self.add_item(ReactRoleButton("Join a Club!", [1405139477705261056, 1418925387018141706]))
        self.add_item(ReactRoleButton("Join the Craft!", [1405139477705261056, 1418925441279983677]))
        self.add_item(ReactRoleButton("Join the Community!", [1405139477705261056]))

class ReactRoleButton(discord.ui.Button):
    def __init__(self, label: str, role_ids: list[int]):
        super().__init__(label = label, style = discord.ButtonStyle.success, custom_ids = f"multi_{'_'.join(str(r) for r in role_ids)}",)
        self.role_ids = role_ids

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild

        added, removed = [], []

        for r in self.role_ids:
            role = guild.get_role(r)
            if role in None:
                continue
            if role in member.roles:
                await member.remove_roles(role, reason = "User requested action via Welcome Reaction Role")
                removed.append(role.name)
            else:
                await member.add_roles(role, reason = "User requested action via Welcome Reaction Role")
                added.append(role.name)
            
        msg = []
        if added:
            msg.append(f"Added {'and '.join(added)}")
        if removed:
            msg.append(f"Remove {'and '.join(removed)}")
        
        if not msg:
            msg = ["Sorry, I can't add role that don't exist. Ping a staff member to get this sorted for you."]

        await interaction.response.send_message("\n".join(msg), ephemeral = True)