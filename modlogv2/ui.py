import discord
from redbot.core import commands

EVENT_PRETTY = {
    # messages
    "message_create":"Message creates","message_edit":"Message edits","message_delete":"Message deletes","message_bulk_delete":"Bulk deletes",
    # reactions
    "reaction_add":"Reaction add","reaction_remove":"Reaction remove","reaction_clear":"Reaction clear",
    # members
    "member_join":"Member joins","member_remove":"Member leaves","member_update":"Member updates",
    # voice/presence
    "voice_change":"Voice changes","presence_update":"Presence updates",
    # roles/channels/threads
    "role_changes":"Role changes","channel_changes":"Channel changes","thread_changes":"Thread changes",
    # emojis/stickers
    "emoji_changes":"Emoji changes","sticker_changes":"Sticker changes",
    # invites/webhooks/integrations
    "invites":"Invites","webhooks":"Webhooks","integrations":"Integrations",
    # scheduled events / stage / guild
    "scheduled_events":"Scheduled events","stage":"Stage instances","guild_change":"Guild changes",
    # commands / automod
    "commands_used":"Commands used","automod_rules":"AutoMod rule changes","automod_action_execution":"AutoMod hits",
}

class SetupView(discord.ui.View):
    def __init__(self, cog: commands.Cog, guild: discord.Guild):
        super().__init__(timeout=180); self.cog=cog; self.guild=guild
        self.add_item(ChannelSelect(self))
        keys = list(EVENT_PRETTY.keys())
        self.add_item(ToggleEvents(self, keys[:10], "Toggle events (1/3)"))
        self.add_item(ToggleEvents(self, keys[10:20], "Toggle events (2/3)"))
        self.add_item(ToggleEvents(self, keys[20:], "Toggle events (3/3)"))
        self.add_item(EmbedToggle(self))
        self.add_item(EnableButton(self, True))
        self.add_item(EnableButton(self, False))
        self.add_item(AutoModPresetButton(self))

class ChannelSelect(discord.ui.Select):
    def __init__(self, view: SetupView):
        opts=[discord.SelectOption(label=f"#{c.name}", value=str(c.id)) for c in view.guild.text_channels] or [discord.SelectOption(label="No text channels", value="0")]
        super().__init__(placeholder="Pick a modlog channel", options=opts, min_values=1, max_values=1); self.v=view
    async def callback(self, itx: discord.Interaction):
        if not (itx.user.guild_permissions.manage_guild or itx.user.guild_permissions.manage_channels):
            return await itx.response.send_message("Need Manage Guild or Manage Channels.", ephemeral=True)
        data = await self.v.cog._get_settings(self.v.guild.id)
        cid = int(self.values[0]); data["log_channel_id"] = cid if cid else None
        await self.v.cog._save_settings(self.v.guild.id, data)
        ch = self.v.guild.get_channel(cid); await itx.response.send_message(f"Log channel set to {ch.mention if ch else 'None'}.", ephemeral=True)

class ToggleEvents(discord.ui.Select):
    def __init__(self, view: SetupView, keys: list[str], placeholder: str):
        self.keys=keys; self.v=view
        super().__init__(placeholder=placeholder, min_values=0, max_values=len(keys),
            options=[discord.SelectOption(label=EVENT_PRETTY[k], value=k) for k in keys])
    async def callback(self, itx: discord.Interaction):
        if not (itx.user.guild_permissions.manage_guild or itx.user.guild_permissions.manage_channels):
            return await itx.response.send_message("Need Manage Guild or Manage Channels.", ephemeral=True)
        data = await self.v.cog._get_settings(self.v.guild.id); changed=[]
        for key in self.keys:
            if key in self.values:
                data["events"][key] = not bool(data["events"].get(key, True)); changed.append(key)
        await self.v.cog._save_settings(self.v.guild.id, data)
        await itx.response.send_message("Toggled: " + (", ".join(EVENT_PRETTY[k] for k in changed) if changed else "nothing"), ephemeral=True)

class EmbedToggle(discord.ui.Button):
    def __init__(self, v: SetupView): super().__init__(label="Toggle embeds", style=discord.ButtonStyle.primary); self.v=v
    async def callback(self, itx: discord.Interaction):
        if not (itx.user.guild_permissions.manage_guild or itx.user.guild_permissions.manage_channels):
            return await itx.response.send_message("Need Manage Guild or Manage Channels.", ephemeral=True)
        data = await self.v.cog._get_settings(self.v.guild.id); data["use_embeds"] = not bool(data["use_embeds"])
        await self.v.cog._save_settings(self.v.guild.id, data)
        await itx.response.send_message(f"Embeds now {'ON' if data['use_embeds'] else 'OFF'}.", ephemeral=True)

class EnableButton(discord.ui.Button):
    def __init__(self, v: SetupView, enable: bool):
        super().__init__(label=("Enable all" if enable else "Disable all"), style=(discord.ButtonStyle.success if enable else discord.ButtonStyle.danger))
        self.v=v; self.enable=enable
    async def callback(self, itx: discord.Interaction):
        if not (itx.user.guild_permissions.manage_guild or itx.user.guild_permissions.manage_channels):
            return await itx.response.send_message("Need Manage Guild or Manage Channels.", ephemeral=True)
        data = await self.v.cog._get_settings(self.v.guild.id)
        for k in list(data["events"].keys()):
            data["events"][k] = self.enable
        await self.v.cog._save_settings(self.v.guild.id, data)
        await itx.response.send_message(f"All events set to {self.enable}.", ephemeral=True)

class AutoModPresetButton(discord.ui.Button):
    def __init__(self, v: SetupView): super().__init__(label="Create AutoMod Preset", style=discord.ButtonStyle.success); self.v=v
    async def callback(self, itx: discord.Interaction):
        if not itx.user.guild_permissions.administrator:
            return await itx.response.send_message("Admin only.", ephemeral=True)
        await self.v.cog._create_automod_preset(itx)
