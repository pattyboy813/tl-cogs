from discord import user
from redbot.core import commands, Config, checks, modlog
import discord
import asyncio
import datetime
import time
from pyrate_limiter import (
    BucketFullException,
    Duration,
    Rate,
    Limiter,
    MemoryListBucket,
    MemoryQueueBucket,
)

ERROR_MESSAGES = {
    'NOTIF_UNRECOGNIZED': "Notification Key was not recognized, please do `!notifs info` to get more info about the keys. List of valid keys: kick, ban, mute, jail, warn, channelperms, editchannel, deletemessages, ratelimit, adminrole, bot",
    'PERM_UNRECOGNIZED': "Permission Key was not recognized, please do `!modpset perms info` to get more info about the keys. List of valid keys: kick, ban, mute, jail, warn, channelperms, editchannel, deletemessages."
}

PERM_SYS_INFO = """
**__Permission System Information__**
**Kick:** Can Kick Members (5 per hour max)
**Ban:** Can Ban Members (3 per hour max)
**Mute:** Can Mute Members
**Jail:** Can Jail Members
**Warn:** Can Warn Members
**ChannelPerms:** Can Add / Remove Members from Channels
**EditChannel:** Can Create, Rename, Enable Slowmode and Move Channels
**DeleteMessages:** Can Delete and Pin Messages. (50 per hour max)
"""

NOTIF_SYS_INFO = """
**__Notification System Information__**
You will be DMed on the events that you choose, listed below:
**Kick:** When someone is kicked
**Ban:** When someone is banned
**Mute:** When someone is muted
**Jail:** When someone is jailed
**Warn:** When someone is warned
**ChannelPerms:** When someone has been added / removed from a channel
**EditChannel:** When a channel has been created, moved or renamed
**DeleteMessages:** When messages have been deleted (note this will get spammy)
**RateLimit:** When a moderator has hit a rate limit (recommended)
**AdminRole:** When a member has been given admin or a role has been given admin (recommended)
**Bot:** When a bot has been added to the server
"""


class ModPlus(commands.Cog):
    """Ultimate Moderation Cog for RedBot"""
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, 8818154, force_registration=True)
        # PyRateLimit.init(redis_host="localhost", redis_port=6379)
        hourly_rate5 = Rate(5, Duration.HOUR)
        hourly_rate3 = Rate(3, Duration.HOUR)
        self.kicklimiter = Limiter(hourly_rate5)
        self.banlimiter = Limiter(hourly_rate3)
        # self.kicklimit = PyRateLimit()
        # self.kicklimit.create(3600, 5)
        # self.banlimit = PyRateLimit()
        # self.banlimit.create(3600, 3)

        default_global = {
            'notifs': {
                'kick': [],
                'ban': [],
                'mute': [],
                'jail': [],
                'channelperms': [],
                'editchannel': [],
                'deletemessages': [],
                'ratelimit': [],
                'adminrole': [],
                'bot':[],
                'warn':[]
            },
            'notifchannels' : {
                'kick': [],
                'ban': [],
                'mute': [],
                'jail': [],
                'channelperms': [],
                'editchannel': [],
                'deletemessages': [],
                'ratelimit': [],
                'adminrole': [],
                'bot':[],
                'warn':[]
            }
        }
        default_guild = {
            'perms': {
                'kick': [],
                'ban': [],
                'mute': [],
                'jail': [],
                'channelperms': [],
                'editchannel': [],
                'deletemessages': [],
                'warn': []
            },
            'roles': {
                'warning1': None,
                'warning2': None,
                'warning3+': None,
                'jailed': None,
                'muted': None
            }
        }
        self.config.register_guild(**default_guild)   
        self.config.register_global(**default_global)   
        self.permkeys = [
            'kick',
            'ban',
            'mute',
            'jail',
            'channelperms',
            'editchannel',
            'deletemessages',
            'warn'
        ]
        self.notifkeys = [
            'kick',
            'ban',
            'mute',
            'jail',
            'channelperms',
            'editchannel',
            'deletemessages',
            'ratelimit',
            'adminrole',
            'bot',
            'warn'
        ]
        self.rolekeys = [
            'warning1',
            'warning2',
            'warning3+',
            'jailed',
            'muted'
        ]

    # Notifications Part

    @commands.group(aliases=['notifs', 'notif']) # CHANNEL
    @checks.mod()
    async def adminnotifications(self, ctx):
        """Configure what notifications to get"""
        pass

    @adminnotifications.group(name='channel')
    async def notifschannel(self, ctx):
        """Configure a channel to recieve notifications"""
        pass

    @adminnotifications.command(name='info')
    async def notifsinfo(self, ctx):
        """Get information about notification system"""
        await ctx.send(NOTIF_SYS_INFO)

    @adminnotifications.command(name='add')
    async def notifsadd(self, ctx, notifkey: str, user: discord.Member = None):
        """Get notified about something"""
        if user is None:
            user = ctx.author
        notifkey = notifkey.strip().lower()
        if notifkey not in self.notifkeys:
            return await ctx.send(ERROR_MESSAGES['NOTIF_UNRECOGNIZED'])

        data = await self.config.notifs()
        if user.id in data[notifkey]:
            return await ctx.send(f"{user.display_name} is already getting notified about {notifkey}")


        data[notifkey].append(user.id)
        await self.config.notifs.set(data)
        return await ctx.send(f"{user.display_name} will now be notified on {notifkey}")

    @adminnotifications.command(name='remove')
    async def notifsremove(self, ctx, notifkey: str, user: discord.Member = None):
        """Stop getting notified about something"""
        if user is None:
            user = ctx.author
        notifkey = notifkey.strip().lower()
        if notifkey not in self.notifkeys:
            return await ctx.send(ERROR_MESSAGES['NOTIF_UNRECOGNIZED'])

        data = await self.config.notifs()
        if user.id not in data[notifkey]:
            return await ctx.send(f"{user.display_name} isn't currently getting notified about {notifkey}")
        
        data[notifkey].remove(user.id)
        await self.config.notifs.set(data)
        return await ctx.send(f"{user.display_name} will now stop being notified about {notifkey}")

    # SHOW NOTIFICATIONS
    @adminnotifications.command(name='list')
    async def notifslist(self, ctx, user: discord.Member = None):
        """Show which notifications you / a user has enabled"""
        if user is None:
            user = ctx.author
        data = await self.config.notifs()
        notifs = []
        for notif in data:
            if user.id in data[notif]:
                notifs.append(notif)
        await ctx.send(f'{user.display_name} is getting notified for the following: ' + ', '.join(notifs))

    # Channel notifications
    @notifschannel.command(name='add')
    async def channelnotifsadd(self, ctx, notifkey: str, channel: discord.TextChannel):
        """Get notified about something (channel)"""
        notifkey = notifkey.strip().lower()
        if notifkey not in self.notifkeys:
            return await ctx.send(ERROR_MESSAGES['NOTIF_UNRECOGNIZED'])

        data = await self.config.notifchannels()
        channeldata = [channel.guild.id, channel.id]
        if channeldata in data[notifkey]:
            return await ctx.send(f"{channel.name} is already getting notified about {notifkey}")

        data[notifkey].append(channeldata)
        await self.config.notifchannels.set(data)
        return await ctx.send(f"{channel.name} will now be notified on {notifkey}")

    @notifschannel.command(name='remove')
    async def channelnotifsremove(self, ctx, notifkey: str, channel: discord.TextChannel):
        """Stop getting notified about something (channel)"""
        notifkey = notifkey.strip().lower()
        if notifkey not in self.notifkeys:
            return await ctx.send(ERROR_MESSAGES['NOTIF_UNRECOGNIZED'])

        data = await self.config.notifchannels()
        channeldata = [channel.guild.id, channel.id]
        if channeldata not in data[notifkey]:
            return await ctx.send(f"{channel.name} isn't currently getting notified about {notifkey}")
        
        data[notifkey].remove(channeldata)
        await self.config.notifchannels.set(data)
        return await ctx.send(f"{channel.name} will now stop being notified about {notifkey}")

    @notifschannel.command(name='list')
    async def channelnotifslist(self, ctx, channel: discord.TextChannel):
        """Show which notifications a channel has enabled"""
        data = await self.config.notifchannels()
        channeldata = [channel.guild.id, channel.id]
        notifs = []
        for notif in data:
            if channeldata in data[notif]:
                notifs.append(notif)
        await ctx.send(f'{channel.name} is getting notified for the following: ' + ', '.join(notifs))
    

    # NOTIFY FUNCTION
    async def notify(self, notifkey, payload):
        data = await self.config.all()
        for userid in data['notifs'][notifkey]:
            user: discord.User = await self.bot.fetch_user(userid)
            try:
                await user.send(payload)
            except Exception:
                pass
        for channel in data['notifchannels'][notifkey]:
            guild: discord.guild = self.bot.get_guild(channel[0])
            if guild is not None:
                txtchannel = guild.get_channel(channel[1])
            try:
                await txtchannel.send(payload, allowed_mentions=discord.AllowedMentions.all())
            except Exception:
                pass


    # Admin Logging
    @commands.Cog.listener(name='on_guild_role_update')
    async def role_add_admin(self, old: discord.Role, new: discord.Role):
        if new.permissions.administrator and not old.permissions.administrator:
            await self.notify('adminrole', f'@everyone Role {new.mention}({new.id}) was updated to contain administrator permission. \n IN: {old.guild.name}({old.guild.id})')

    @commands.Cog.listener(name='on_member_join')
    async def join_bot(self, member: discord.Member):
        """Detect if new joining member is a bot"""
        if member.bot:
            await self.notify('bot', f'@everyone Role Bot {member.mention}({member.id}) was added. \n IN: {member.guild.name}({member.guild.id})')
    
    @commands.Cog.listener(name='on_member_update')
    async def member_admin(self, old: discord.Member, new: discord.Member):
        new_roles = []
        for role in new.roles:
            if role not in old.roles:
                new_roles.append(role)
        for role in new_roles:
            if role.permissions.administrator:
                await self.notify('adminrole', f'@everyone Member{new.mention}({new.id}) was updated to contain administrator permission. \n IN: {old.guild.name}({old.guild.id})')


    async def rate_limit_exceeded(self, user: discord.Member, type):
        """Called to removed all moderation roles when a mod has hit ratelimit"""
        allmodroles = []
        data = await self.config.guild(user.guild).perms()
        for perm in data:
            for role in data[perm]:
                if role not in allmodroles:
                    allmodroles.append(role)
        rm_mention = []
        broken = []
        issue = False
        for role in user.roles:
            if role.id in allmodroles:
                try:
                    await user.remove_roles(role, reason='Rate limit exceeded.')
                    rm_mention.append(role.mention)
                    rm_mention.append('(' + str(role.id) + ')')
                except Exception:
                    issue = True
                    broken.append(role.mention)
                    broken.append('(' + str(role.id) + ')')
            
        if issue:
            await self.notify('ratelimit', "Removing roles in the ratelimit below ended in error. The user has a role above the bot. The following roles could not be removed: " + ', '.join(broken))
        payload = f"@everyone {type} ratelimit has been exceeded by {user.mention} ({user.display_name}, {user.id}). The following roles with power have been removed: " + ', '.join(rm_mention)
        await self.notify('ratelimit', payload)


    async def action_check(self, ctx, permkey):
        if await self.bot.is_admin(ctx.author) or await self.bot.is_owner(ctx.author) or ctx.author.guild_permissions.administrator: # Admin auto-bypass
            return True
        data = await self.config.guild(ctx.guild).all()
        canrun = False
        for role in ctx.author.roles:
            if role.id in data['perms'][permkey]:
                canrun = True
                break
        if not canrun:
            return False
