import discord

COLOURS = {
    "message_delete": discord.Colour.red(),
    "message_edit": discord.Colour.orange(),
    "message_bulk_delete": discord.Colour.dark_orange(),
    "member_join": discord.Colour.green(),
    "member_remove": discord.Colour.red(),
    "member_update": discord.Colour.teal(),
    "role_create": discord.Colour.blurple(),
    "role_update": discord.Colour.blurple(),
    "role_delete": discord.Colour.dark_blue(),
    "channel_create": discord.Colour.blurple(),
    "channel_update": discord.Colour.blurple(),
    "channel_delete": discord.Colour.dark_blue(),
    "thread_changes": discord.Colour.dark_teal(),
    "emoji_update": discord.Colour.gold(),
    "sticker_update": discord.Colour.gold(),
    "invite_create": discord.Colour.lighter_grey(),
    "invite_delete": discord.Colour.dark_grey(),
    "webhooks_update": discord.Colour.dark_grey(),
    "integration_update": discord.Colour.dark_grey(),
    "scheduled_event_create": discord.Colour.dark_magenta(),
    "scheduled_event_update": discord.Colour.dark_magenta(),
    "scheduled_event_delete": discord.Colour.dark_magenta(),
    "stage_create": discord.Colour.fuchsia(),
    "stage_update": discord.Colour.fuchsia(),
    "stage_delete": discord.Colour.fuchsia(),
    "guild_update": discord.Colour.dark_green(),
    "automod_rules": discord.Colour.purple(),
    "automod_action_execution": discord.Colour.purple(),
    "reaction_add": discord.Colour.blue(),
    "reaction_remove": discord.Colour.blue(),
    "reaction_clear": discord.Colour.blue(),
    "voice_state_update": discord.Colour.dark_teal(),
    "presence_update": discord.Colour.dark_teal(),
    "command_used": discord.Colour.dark_grey(),
}

def colour_for(event: str) -> discord.Colour:
    return COLOURS.get(event, discord.Colour.blurple())

def build_embed(
    *, guild: discord.Guild, event: str, title: str,
    description: str = "", author: discord.abc.User | None = None,
    jump_url: str | None = None, fields: list[tuple[str, str, bool]] | None = None
) -> discord.Embed:
    emb = discord.Embed(
        title=title, description=description, colour=colour_for(event),
        timestamp=discord.utils.utcnow(), url=jump_url or discord.Embed.Empty
    )
    if author:
        emb.set_author(
            name=f"{author} • ID {author.id}",
            icon_url=getattr(author.display_avatar, "url", discord.Embed.Empty)
        )
    icon = getattr(getattr(guild, "icon", None), "url", None)
    if icon:
        emb.set_footer(text=f"{guild.name} • ID {guild.id}", icon_url=icon)
    else:
        emb.set_footer(text=f"{guild.name} • ID {guild.id}")
    if fields:
        for n, v, inline in fields:
            emb.add_field(name=n, value=v, inline=inline)
    return emb
