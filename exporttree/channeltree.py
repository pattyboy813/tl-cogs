# channeltree.py
# Red-DiscordBot cog: Export hierarchical view of categories/channels (and threads)
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import discord
from redbot.core import commands, checks


SYMBOLS = {
    "text": "#",
    "voice": "🔊",
    "stage": "🎤",
    "news": "📢",
    "forum": "🗂️",
    "thread": "🧵",
    "category": "📁",
}

def chan_symbol(ch: discord.abc.GuildChannel) -> str:
    if isinstance(ch, discord.TextChannel):
        # Announcements might still appear as NewsChannel on some d.py versions
        if getattr(ch, "news", False) or ch.type.name.lower() == "news":
            return SYMBOLS["news"]
        return SYMBOLS["text"]
    if isinstance(ch, discord.VoiceChannel):
        return SYMBOLS["voice"]
    if isinstance(ch, discord.StageChannel):
        return SYMBOLS["stage"]
    # ForumChannel in newer d.py
    if ch.type.name.lower() == "forum":
        return SYMBOLS["forum"]
    if isinstance(ch, discord.CategoryChannel):
        return SYMBOLS["category"]
    return "•"

class ChannelTree(commands.Cog):
    """
    Export a hierarchical map of categories, channels, and (optionally) threads.
    """

    __author__ = "you"
    __version__ = "1.0.0"

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="exporttree")
    @commands.guild_only()
    @checks.mod_or_permissions(administrator=True)
    async def exporttree(
        self,
        ctx: commands.Context,
        fmt: Optional[str] = "txt",
        include_ids: Optional[bool] = False,
        include_threads: Optional[bool] = True,
        include_archived_threads: Optional[bool] = False,
    ):
        """
        Export a hierarchical view of this server's categories/channels.

        Usage:
          [p]exporttree
          [p]exporttree json true true false
          [p]exporttree txt true false

        Args (positional, optional):
          fmt: "txt" (default) or "json"
          include_ids: true/false (default false)
          include_threads: true/false (default true; includes active threads)
          include_archived_threads: true/false (default false; tries to fetch archived threads)

        Notes:
          • Fetching archived threads may require Manage Threads / View Channel History and can take longer.
        """
        guild: discord.Guild = ctx.guild
        if guild is None:
            await ctx.send("This must be used in a server.")
            return

        if fmt.lower() not in {"txt", "json"}:
            await ctx.send("Format must be `txt` or `json`.")
            return

        # Build model
        data = await self._collect_guild_structure(
            guild,
            include_ids=bool(include_ids),
            include_threads=bool(include_threads),
            include_archived=bool(include_archived_threads),
        )

        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = f"server-map-{guild.id}-{ts}"

        if fmt.lower() == "json":
            import json
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            filename = f"{base}.json"
        else:
            payload = self._render_text_tree(data)
            filename = f"{base}.txt"

        fp = io.BytesIO(payload.encode("utf-8"))
        file = discord.File(fp, filename=filename)

        await ctx.send(
            content=f"Export complete for **{discord.utils.escape_markdown(guild.name)}** "
                    f"(format: `{fmt.lower()}`, ids={bool(include_ids)}, "
                    f"threads={bool(include_threads)}, archived={bool(include_archived_threads)}).",
            file=file,
        )

    async def _collect_guild_structure(
        self,
        guild: discord.Guild,
        *,
        include_ids: bool,
        include_threads: bool,
        include_archived: bool,
    ) -> Dict[str, Any]:
        """
        Returns a JSON-serializable dict with the guild hierarchy.
        """
        # discord.Guild.by_category returns [(Category or None, [channels...]), ...] in display order
        groups = guild.by_category()

        root: Dict[str, Any] = {
            "guild": {"name": guild.name, "id": str(guild.id) if include_ids else None},
            "categories": [],
        }

        for category, channels in groups:
            cat_entry: Dict[str, Any] = {
                "name": category.name if category else "No Category",
                "id": str(category.id) if (category and include_ids) else (None if include_ids else None),
                "type": "category" if category else "uncategorized",
                "channels": [],
            }

            # Channels are already ordered by position in this list
            for ch in channels:
                ch_entry: Dict[str, Any] = {
                    "name": ch.name,
                    "id": str(ch.id) if include_ids else None,
                    "type": ch.type.name.lower(),
                    "threads": [],
                }

                if include_threads:
                    # Active threads are accessible via .threads on text/forum channels
                    active_threads = getattr(ch, "threads", []) or []
                    for t in active_threads:
                        ch_entry["threads"].append({
                            "name": t.name,
                            "id": str(t.id) if include_ids else None,
                            "archived": t.archived,
                            "locked": t.locked,
                        })

                    if include_archived and hasattr(ch, "archived_threads"):
                        # Try public archived threads
                        try:
                            async for t in ch.archived_threads(limit=100, private=False):
                                ch_entry["threads"].append({
                                    "name": t.name,
                                    "id": str(t.id) if include_ids else None,
                                    "archived": t.archived,
                                    "locked": t.locked,
                                })
                        except Exception:
                            pass
                        # Try private archived threads (requires perms)
                        try:
                            async for t in ch.archived_threads(limit=50, private=True):
                                ch_entry["threads"].append({
                                    "name": t.name,
                                    "id": str(t.id) if include_ids else None,
                                    "archived": t.archived,
                                    "locked": t.locked,
                                })
                        except Exception:
                            pass

                cat_entry["channels"].append(ch_entry)

            root["categories"].append(cat_entry)

        return root

    def _render_text_tree(self, data: Dict[str, Any]) -> str:
        g = data.get("guild", {})
        gname = g.get("name") or "Unknown Guild"
        gid = g.get("id")

        lines: List[str] = []
        header = f"{gname}" + (f" (ID {gid})" if gid else "")
        lines.append(header)
        lines.append("=" * len(header))
        lines.append("")

        for cat in data.get("categories", []):
            cname = cat["name"]
            cid = cat.get("id")
            ctype = cat.get("type") or "category"
            cicon = SYMBOLS["category"] if ctype == "category" else "•"
            lines.append(f"{cicon} {cname}" + (f" [ID {cid}]" if cid else ""))

            channels = cat.get("channels", [])
            for idx, ch in enumerate(channels):
                prefix = "├─"
                if idx == len(channels) - 1:
                    prefix = "└─"

                ctype = ch.get("type", "text")
                icon = chan_symbol(_FakeType(ctype))
                cname = ch["name"]
                cid = ch.get("id")
                lines.append(f"  {prefix} {icon} {cname}" + (f" [ID {cid}]" if cid else ""))

                threads = ch.get("threads", [])
                for jdx, t in enumerate(threads):
                    tpref = "├─" if jdx < len(threads) - 1 else "└─"
                    tname = t["name"]
                    tid = t.get("id")
                    lines.append(f"  │   {tpref} {SYMBOLS['thread']} {tname}" + (f" [ID {tid}]" if tid else ""))

            lines.append("")  # blank line between categories

        lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
        return "\n".join(lines)


class _FakeType:
    """Tiny helper so chan_symbol can work with string types when rendering."""
    def __init__(self, name: str):
        self.type = _FakeTypeName(name)

class _FakeTypeName:
    def __init__(self, name: str):
        self._name = name
    @property
    def name(self) -> str:
        return self._name


async def setup(bot):
    await bot.add_cog(ChannelTree(bot))
