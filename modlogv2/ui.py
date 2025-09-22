import discord
from redbot.core import commands

# Pretty labels for the event toggles shown in the UI
EVENT_PRETTY = {
    # messages
    "message_create": "Message creates",
    "message_edit": "Message edits",
    "message_delete": "Message deletes",
    "message_bulk_delete": "Bulk deletes",
    # reactions
    "reaction_add": "Reaction add",
    "reaction_remove": "Reaction remove",
    "reaction_clear": "Reaction clear",
    # members
    "member_join": "Member joins",
    "member_remove": "Member leaves",
    "member_update": "Member updates",
    # voice/presence
    "voice_change": "Voice changes",
    "presence_update": "Presence updates",
    # roles/channels/threads
    "role_changes": "Role changes",
    "channel_changes": "Channel changes",
    "thread_changes": "Thread changes",
    # emojis/stickers
    "emoji_changes": "Emoji changes",
    "sticker_changes": "Sticker changes",
    # invites/webhooks/integrations
    "invites": "Invites",
    "webhooks": "Webhooks",
    "integrations": "Integrations",
    # scheduled events / stage / guild
    "scheduled_events": "Scheduled events",
    "stage": "Stage instances",
    "guild_change": "Guild changes",
    # commands / automod
    "commands_used": "Commands used",
    "automod_rules": "AutoMod rule changes",
    "automod_action_execution": "AutoMod hits",
}


class ChannelPicker(discord.ui.ChannelSelect):
    """Native channel picker (avoids the 25-options limit)."""

    def __init__(self, view: "SetupView"):
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Pick a modlog channel",
            min_values=1,
            max_values=1,
        )
        self.v = view

    async def callback(self, itx: discord.Interaction):
        # Permissions: allow Manage Guild or Manage Channels to set logging channel
        if not (
            itx.user.guild_permissions.manage_guild
            or itx.user.guild_permissions.manage_channels
        ):
            return await itx.response.send_message(
                "Need Manage Guild or Manage Channels.", ephemeral=True
            )

        # ChannelSelect provides channel objects directly
        ch = self.values[0]  # type: ignore[assignment]
        data = await self.v.cog._get_settings(self.v.guild.id)
        data["log_channel_id"] = ch.id if ch else None
        await self.v.cog._save_settings(self.v.guild.id, data)

        mention = ch.mention if ch else "None"
        await itx.response.send_message(
            f"Log channel set to {mention}.", ephemeral=True
        )


class ToggleEvents(discord.ui.Select):
    """Chunked multi-select to toggle groups of events."""

    def __init__(self, view: "SetupView", keys: list[str], placeholder: str):
        self.keys = keys
        self.v = view
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(keys),
            options=[
                discord.SelectOption(label=EVENT_PRETTY[k], value=k) for k in keys
            ],
        )

    async def callback(self, itx: discord.Interaction):
        if not (
            itx.user.guild_permissions.manage_guild
            or itx.user.guild_permissions.manage_channels
        ):
            return await itx.response.send_message(
                "Need Manage Guild or Manage Channels.", ephemeral=True
            )

        data = await self.v.cog._get_settings(self.v.guild.id)
        changed = []
        # Toggle only the selected keys in this page; leave others as-is
        for key in self.keys:
            if key in self.values:
                data["events"][key] = not bool(data["events"].get(key, True))
                changed.append(key)

        await self.v.cog._save_settings(self.v.guild.id, data)
        if changed:
            await itx.response.send_message(
                "Toggled: " + ", ".join(EVENT_PRETTY[k] for k in changed),
                ephemeral=True,
            )
        else:
            await itx.response.send_message("No changes.", ephemeral=True)


class EmbedToggle(discord.ui.Button):
    """Flip embeds on/off for channel messages."""

    def __init__(self, v: "SetupView"):
        super().__init__(label="Toggle embeds", style=discord.ButtonStyle.primary)
        self.v = v

    async def callback(self, itx: discord.Interaction):
        if not (
            itx.user.guild_permissions.manage_guild
            or itx.user.guild_permissions.manage_channels
        ):
            return await itx.response.send_message(
                "Need Manage Guild or Manage Channels.", ephemeral=True
            )

        data = await self.v.cog._get_settings(self.v.guild.id)
        data["use_embeds"] = not bool(data["use_embeds"])
        await self.v.cog._save_settings(self.v.guild.id, data)
        await itx.response.send_message(
            f"Embeds now {'ON' if data['use_embeds'] else 'OFF'}.", ephemeral=True
        )


class EnableButton(discord.ui.Button):
    """Enable/disable all events at once (useful for big servers)."""

    def __init__(self, v: "SetupView", enable: bool):
        super().__init__(
            label=("Enable all" if enable else "Disable all"),
            style=(discord.ButtonStyle.success if enable else discord.ButtonStyle.danger),
        )
        self.v = v
        self.enable = enable

    async def callback(self, itx: discord.Interaction):
        if not (
            itx.user.guild_permissions.manage_guild
            or itx.user.guild_permissions.manage_channels
        ):
            return await itx.response.send_message(
                "Need Manage Guild or Manage Channels.", ephemeral=True
            )

        data = await self.v.cog._get_settings(self.v.guild.id)
        for k in list(data["events"].keys()):
            data["events"][k] = self.enable
        await self.v.cog._save_settings(self.v.guild.id, data)
        await itx.response.send_message(
            f"All events set to {self.enable}.", ephemeral=True
        )


class AutoModPresetButton(discord.ui.Button):
    """One-click helper to create a simple AutoMod preset (optional)."""

    def __init__(self, v: "SetupView"):
        super().__init__(label="Create AutoMod Preset", style=discord.ButtonStyle.success)
        self.v = v

    async def callback(self, itx: discord.Interaction):
        if not itx.user.guild_permissions.administrator:
            return await itx.response.send_message("Admin only.", ephemeral=True)
        await self.v.cog._create_automod_preset(itx)


class SetupView(discord.ui.View):
    """Top-level view: channel picker, event toggles, embed switch, mass on/off, AutoMod."""

    def __init__(self, cog: commands.Cog, guild: discord.Guild):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild

        # Native channel picker (no 25-option cap)
        self.add_item(ChannelPicker(self))

        # Break event toggles into manageable pages
        keys = list(EVENT_PRETTY.keys())
        self.add_item(ToggleEvents(self, keys[:10], "Toggle events (1/3)"))
        self.add_item(ToggleEvents(self, keys[10:20], "Toggle events (2/3)"))
        self.add_item(ToggleEvents(self, keys[20:], "Toggle events (3/3)"))

        # Misc controls
        self.add_item(EmbedToggle(self))
        self.add_item(EnableButton(self, True))
        self.add_item(EnableButton(self, False))
        self.add_item(AutoModPresetButton(self))
