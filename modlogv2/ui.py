import discord
from redbot.core import commands

EVENT_PRETTY = {
    "message_delete":"Message deletes","message_edit":"Message edits","message_bulk_delete":"Bulk deletes",
    "member_join":"Member joins","member_remove":"Member leaves","member_update":"Member updates",
    "role_changes":"Role changes","channel_changes":"Channel changes","emoji_changes":"Emoji changes",
    "sticker_changes":"Sticker changes","thread_changes":"Thread changes","timeouts":"Timeouts",
    "integrations":"Integrations","webhooks":"Webhooks",
    "automod_rules":"AutoMod rule changes","automod_action_execution":"AutoMod hits",
}

class DSNModal(discord.ui.Modal, title="Set Postgres DSN"):
    dsn = discord.ui.TextInput(label="postgres DSN", placeholder="postgresql://user:pass@host:5432/dbname", required=True)
    def __init__(self, cog: commands.Cog, guild: discord.Guild): super().__init__(); self.cog=cog; self.guild=guild
    async def on_submit(self, itx: discord.Interaction):
        await self.cog._set_dsn(str(self.dsn))
        await itx.response.send_message("DSN saved and pool initialized ✅", ephemeral=True)

class SetupView(discord.ui.View):
    def __init__(self, cog: commands.Cog, guild: discord.Guild):
        super().__init__(timeout=180); self.cog=cog; self.guild=guild
        self.add_item(ChannelSelect(self))
        self.add_item(ToggleEvents(self, list(EVENT_PRETTY.keys())[:9], "Toggle events (1/2)"))
        self.add_item(ToggleEvents(self, list(EVENT_PRETTY.keys())[9:], "Toggle events (2/2)"))
        self.add_item(EmbedToggle(self))
        self.add_item(DSNButton(self))
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

class DSNButton(discord.ui.Button):
    def __init__(self, v: SetupView): super().__init__(label="Set Postgres DSN", style=discord.ButtonStyle.secondary); self.v=v
    async def callback(self, itx: discord.Interaction):
        if not itx.user.guild_permissions.administrator:
            return await itx.response.send_message("Admin only.", ephemeral=True)
        await itx.response.send_modal(DSNModal(self.v.cog, self.v.guild))

class AutoModPresetButton(discord.ui.Button):
    def __init__(self, v: SetupView): super().__init__(label="Create AutoMod Preset", style=discord.ButtonStyle.success); self.v=v
    async def callback(self, itx: discord.Interaction):
        if not itx.user.guild_permissions.administrator:
            return await itx.response.send_message("Admin only.", ephemeral=True)
        await self.v.cog._create_automod_preset(itx)
