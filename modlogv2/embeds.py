import discord

COLOURS = {
    "message_delete": discord.Colour.red(),
    "message_edit": discord.Colour.orange(),
    "member_join": discord.Colour.green(),
    "member_remove": discord.Colour.red(),
    "automod_action_execution": discord.Colour.purple(),
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
        emb.set_author(name=f"{author} • ID {author.id}",
                       icon_url=getattr(author.display_avatar, "url", discord.Embed.Empty))
    icon = getattr(getattr(guild, "icon", None), "url", None)
    if icon:
        emb.set_footer(text=f"{guild.name} • ID {guild.id}", icon_url=icon)
    else:
        emb.set_footer(text=f"{guild.name} • ID {guild.id}")
    if fields:
        for n, v, inline in fields:
            emb.add_field(name=n, value=v, inline=inline)
    return emb
