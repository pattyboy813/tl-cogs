# channeltree.py
# Red-DiscordBot cog: Export hierarchical view of categories/channels/threads (TXT | JSON | CSV)
# Author: tl | Version: 1.1.0

from __future__ import annotations

import io
import csv
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Iterable

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
    "unknown": "•",
}

# ---------- Utilities ----------

def _to_bool(val: Optional[object], default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _chan_symbol(ch: discord.abc.GuildChannel) -> str:
    # Works both for real channels and our fake stand-in object (see renderer).
    ctype = getattr(getattr(ch, "type", None), "name", None)
    if not ctype:
        ctype = getattr(ch, "_type_name", "unknown")

    ctype = ctype.lower()
    if ctype == "text":
        # News/Announcement channels show as ChannelType.news on newer libs
        if getattr(ch, "news", False) or ctype == "news":
            return SYMBOLS["news"]
        return SYMBOLS["text"]
    if ctype == "news":
        return SYMBOLS["news"]
    if ctype == "voice":
        return SYMBOLS["voice"]
    if ctype == "stage_voice" or ctype == "stage":
        return SYMBOLS["stage"]
    if ctype == "forum":
        return SYMBOLS["forum"]
    if ctype == "category":
        return SYMBOLS["category"]
    return SYMBOLS["unknown"]


# ---------- Cog ----------

class ChannelTree(commands.Cog):
    """
    Export a hierarchical map of categories, channels, and (optionally) threads.

    Usage:
      [p]exporttree
      [p]exporttree json true true false
      [p]exporttree csv true true true
      [p]exporttree txt false false

    Args (positional, optional):
      format: txt (default) | json | csv
      include_ids: true/false (default false)
      include_threads: true/false (default true)
      include_archived_threads: true/false (default false)

    Output:
      • TXT => nice tree
      • JSON => structured hierarchy
      • CSV => flat rows with Category/Channel/Thread columns
    """

    __author__ = "tl"
    __version__ = "1.1.0"

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
        """
        guild: discord.Guild = ctx.guild
        if guild is None:
            await ctx.send("This command must be used in a server.")
            return

        fmt = (fmt or "txt").strip().lower()
        if fmt not in {"txt", "json", "csv"}:
            await ctx.send("Format must be `txt`, `json`, or `csv`.")
            return

        include_ids_b = _to_bool(include_ids, False)
        include_threads_b = _to_bool(include_threads, True)
        include_archived_b = _to_bool(include_archived_threads, False)

        try:
            data = await self._collect_guild_structure(
                guild,
                include_ids=include_ids_b,
                include_threads=include_threads_b,
                include_archived=include_archived_b,
            )
        except discord.Forbidden:
            await ctx.send("I don't have permission to view some channels or threads.")
            return
        except Exception as e:
            await ctx.send(f"Failed to collect structure: `{type(e).__name__}: {e}`")
            return

        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = f"server-map-{guild.id}-{ts}"

        try:
            if fmt == "json":
                import json
                payload = json.dumps(data, ensure_ascii=False, indent=2)
                filename = f"{base}.json"

            elif fmt == "csv":
                payload = self._render_csv(data)
                filename = f"{base}.csv"

            else:  # txt
                payload = self._render_text_tree(data)
                filename = f"{base}.txt"
        except Exception as e:
            await ctx.send(f"Failed to render output: `{type(e).__name__}: {e}`")
            return

        fp = io.BytesIO(payload.encode("utf-8"))
        file = discord.File(fp, filename=filename)

        await ctx.send(
            content=(
                f"Export complete for **{discord.utils.escape_markdown(guild.name)}** "
                f"(format: `{fmt}`, ids={include_ids_b}, "
                f"threads={include_threads_b}, archived={include_archived_b})."
            ),
            file=file,
        )

    # ---------- Data collection ----------

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
        Keeps display order (categories then channels in-position).
        """
        groups = guild.by_category()  # [(Category or None, [channels...]), ...]

        root: Dict[str, Any] = {
            "guild": {
                "name": guild.name,
                "id": str(guild.id) if include_ids else None,
            },
            "categories": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        for category, channels in groups:
            cat_entry: Dict[str, Any] = {
                "name": category.name if category else "No Category",
                "id": str(category.id) if (category and include_ids) else (None if include_ids else None),
                "type": "category" if category else "uncategorized",
                "position": getattr(category, "position", None) if category else None,
                "channels": [],
            }

            for ch in channels:
                ctype = getattr(getattr(ch, "type", None), "name", "text").lower()
                ch_entry: Dict[str, Any] = {
                    "name": ch.name,
                    "id": str(ch.id) if include_ids else None,
                    "type": ctype,
                    "position": getattr(ch, "position", None),
                    "nsfw": getattr(ch, "is_nsfw", lambda: False)() if hasattr(ch, "is_nsfw") else False,
                    "threads": [],
                }

                if include_threads and isinstance(ch, (discord.TextChannel, discord.ForumChannel, discord.NewsChannel)):
                    # Active threads (visible)
                    active_threads: Iterable[discord.Thread] = getattr(ch, "threads", []) or []
                    for t in active_threads:
                        ch_entry["threads"].append(self._thread_dump(t, include_ids))

                    # Archived threads (public + private), if requested and API supports it
                    if include_archived and hasattr(ch, "archived_threads"):
                        # Public archived
                        try:
                            async for t in ch.archived_threads(limit=100, private=False):
                                ch_entry["threads"].append(self._thread_dump(t, include_ids))
                        except Exception:
                            pass
                        # Private archived (requires perms)
                        try:
                            async for t in ch.archived_threads(limit=100, private=True):
                                ch_entry["threads"].append(self._thread_dump(t, include_ids))
                        except Exception:
                            pass

                cat_entry["channels"].append(ch_entry)

            root["categories"].append(cat_entry)

        return root

    @staticmethod
    def _thread_dump(t: discord.Thread, include_ids: bool) -> Dict[str, Any]:
        return {
            "name": t.name,
            "id": str(t.id) if include_ids else None,
            "archived": bool(getattr(t, "archived", False)),
            "locked": bool(getattr(t, "locked", False)),
        }

    # ---------- Renderers ----------

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
                prefix = "├─" if idx < len(channels) - 1 else "└─"

                # Create a tiny fake object so _chan_symbol works for string types on render.
                fake = type("Fake", (), {})()
                fake.type = type("T", (), {"name": ch.get("type", "text")})()
                icon = _chan_symbol(fake)

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

        lines.append(f"Generated: {data.get('generated_at', datetime.now(timezone.utc).isoformat())}")
        return "\n".join(lines)

    def _render_csv(self, data: Dict[str, Any]) -> str:
        """
        CSV columns:
          Category, CategoryID, Channel, ChannelID, ChannelType, ChannelPosition, NSFW,
          Thread, ThreadID, ThreadArchived, ThreadLocked
        One row per channel (with empty thread fields) OR per thread (with thread fields filled).
        """
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Category",
            "CategoryID",
            "Channel",
            "ChannelID",
            "ChannelType",
            "ChannelPosition",
            "NSFW",
            "Thread",
            "ThreadID",
            "ThreadArchived",
            "ThreadLocked",
        ])

        for cat in data.get("categories", []):
            cat_name = cat.get("name", "")
            cat_id = cat.get("id") or ""
            for ch in cat.get("channels", []):
                ch_name = ch.get("name", "")
                ch_id = ch.get("id") or ""
                ch_type = ch.get("type", "")
                ch_pos = ch.get("position", "")
                ch_nsfw = ch.get("nsfw", False)

                threads = ch.get("threads", [])
                if threads:
                    for t in threads:
                        writer.writerow([
                            cat_name,
                            cat_id,
                            ch_name,
                            ch_id,
                            ch_type,
                            ch_pos,
                            ch_nsfw,
                            t.get("name", ""),
                            t.get("id") or "",
                            t.get("archived", False),
                            t.get("locked", False),
                        ])
                else:
                    writer.writerow([
                        cat_name,
                        cat_id,
                        ch_name,
                        ch_id,
                        ch_type,
                        ch_pos,
                        ch_nsfw,
                        "", "", "", "",
                    ])

        return output.getvalue()


async def setup(bot):
    await bot.add_cog(ChannelTree(bot))
