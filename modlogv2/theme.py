from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Iterable, Tuple
import discord

@dataclass
class Theme:
    name: str = "brand"
    accent: int = 0x5865F2  # Discord blurple
    soft_bg: int = 0x2B2D31
    danger: int = 0xED4245
    success: int = 0x57F287
    warning: int = 0xFEE75C

class EmbedFactory:
    def __init__(self, theme: Theme | None = None):
        self.theme = theme or Theme()

    def base(self, *, colour: Optional[discord.Colour] = None, ts=None) -> discord.Embed:
        kw = {}
        if ts is not None:
            kw["timestamp"] = ts
        return discord.Embed(colour=colour or discord.Colour(self.theme.accent), **kw)

    @staticmethod
    def userline(user: discord.abc.User | discord.Member | None) -> str:
        if not user:
            return "—"
        return f"{user} (`{getattr(user,'id','?')}`)"

    @staticmethod
    def ch_mention(ch) -> str:
        if hasattr(ch, "mention"):
            return ch.mention
        if hasattr(ch, "id"):
            return f"<#{ch.id}>"
        return str(ch)

    def author(self, emb: discord.Embed, *, title: str, icon_url: Optional[str] = None):
        emb.set_author(name=title, icon_url=icon_url or discord.Embed.Empty)
        return emb

    def footer_ids(self, emb: discord.Embed, *, user: Optional[discord.abc.User] = None, extra: Optional[str] = None):
        parts = []
        if user:
            parts.append(f"User ID: {user.id}")
        if extra:
            parts.append(extra)
        if parts:
            emb.set_footer(text=" • ".join(parts))
        return emb

    @staticmethod
    def add_fields(emb: discord.Embed, fields: Iterable[Tuple[str, str, bool]]):
        for name, value, inline in fields:
            if value is None:
                continue
            emb.add_field(name=name, value=str(value)[:1024] or "—", inline=inline)
        return emb
