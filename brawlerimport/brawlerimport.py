import asyncio
import io
import re
from typing import List, Dict, Any, Optional

import aiohttp
import discord
from redbot.core import commands, checks
from redbot.core.bot import Red

BRAWLERS_API = "https://api.brawlapi.com/v1/brawlers"
EMOJI_BASE = "https://cdn.brawlify.com/brawlers/emoji"  # {id}.png


class BSEmoji(commands.Cog):
    """
    Import & export Brawl Stars brawler emojis from Brawlify CDN.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()
            self.session = None

    # ---------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------

    async def _get_brawlers(self) -> List[Dict[str, Any]]:
        """Get brawler list from BrawlAPI."""
        if self.session is None:
            self.session = aiohttp.ClientSession()

        async with self.session.get(BRAWLERS_API) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Failed to fetch brawlers: HTTP {resp.status}"
                )
            data = await resp.json()

        # Handle possible shapes
        if isinstance(data, list):
            return data

        for key in ("list", "brawlers", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]

        # Last ditch attempt
        for val in data.values():
            if isinstance(val, list):
                return val

        raise RuntimeError("Unknown BrawlAPI JSON layout.")

    @staticmethod
    def _emoji_name_from_brawler(name: str) -> str:
        """
        Turn brawler names into valid Discord emoji names:
        “El Primo” -> “elprimo”
        “Mr. P” -> “mrp”
        """
        raw = name.lower().replace(" ", "").replace("-", "").replace(".", "")
        cleaned = re.sub(r"[^a-z0-9_]", "", raw)
        return cleaned[:32] or "brawler"

    async def _download_image(self, url: str) -> bytes:
        if self.session is None:
            self.session = aiohttp.ClientSession()

        async with self.session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to download {url}")
            return await resp.read()

    # ---------------------------------------------------------
    # Command group
    # ---------------------------------------------------------

    @commands.group(name="bsemoji")
    async def bsemoji_group(self, ctx: commands.Context):
        """Brawl Stars emoji importer."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    # ---------------------------------------------------------
    # IMPORT COMMAND
    # ---------------------------------------------------------

    @bsemoji_group.command(name="import")
    @checks.admin_or_permissions(manage_guild=True)
    async def bsemoji_import(
        self,
        ctx: commands.Context,
        limit: int = 0,
        overwrite: bool = False,
    ):
        """
        Import Brawl Stars emojis into your server.

        Usage:
          [p]bsemoji import
          [p]bsemoji import 30
          [p]bsemoji import 0 true
        """
        guild = ctx.guild

        if guild is None:
            await ctx.send("Must be used inside a server.")
            return

        if not guild.me.guild_permissions.manage_emojis_and_stickers:
            await ctx.send(
                "I need **Manage Emojis and Stickers** permission."
            )
            return

        try:
            brawlers = await self._get_brawlers()
        except Exception as e:
            await ctx.send(f"Error fetching brawlers:\n`{e}`")
            return

        if limit > 0:
            brawlers = brawlers[:limit]

        existing = {e.name: e for e in guild.emojis}
        max_emojis = guild.emoji_limit
        current = len(guild.emojis)

        msg = await ctx.send(
            f"📥 Importing **{len(brawlers)}** brawlers...\n"
            f"Emoji slots: {current}/{max_emojis}"
        )

        created = replaced = skipped = failed = 0

        for entry in brawlers:
            b_id = entry.get("id")
            b_name = entry.get("name", f"id{b_id}")

            if b_id is None:
                skipped += 1
                continue

            emoji_name = self._emoji_name_from_brawler(b_name)
            existing_emoji = existing.get(emoji_name)

            # If full & not overwriting, skip
            if not overwrite and len(guild.emojis) >= max_emojis:
                skipped += 1
                continue

            # If exists & not overwriting
            if existing_emoji and not overwrite:
                skipped += 1
                continue

            # Download emoji
            url = f"{EMOJI_BASE}/{b_id}.png"
            try:
                data = await self._download_image(url)
            except Exception:
                failed += 1
                continue

            # If overwriting existing emoji
            if existing_emoji and overwrite:
                try:
                    await existing_emoji.delete(reason="Overwritten by bsemoji")
                except discord.HTTPException:
                    pass  # ignore failures

                try:
                    new_emoji = await guild.create_custom_emoji(
                        name=emoji_name, image=data
                    )
                    existing[emoji_name] = new_emoji
                    replaced += 1
                except discord.HTTPException:
                    failed += 1
                await asyncio.sleep(0.3)
                continue

            # Otherwise create new emoji
            try:
                new_emoji = await guild.create_custom_emoji(
                    name=emoji_name, image=data
                )
                existing[emoji_name] = new_emoji
                created += 1
            except discord.HTTPException:
                failed += 1

            await asyncio.sleep(0.3)  # rate limit safety

        final = (
            "✅ **Emoji Import Complete**\n"
            f"Created: **{created}**\n"
            f"Replaced: **{replaced}**\n"
            f"Skipped: **{skipped}**\n"
            f"Failed: **{failed}**\n"
            f"Final Count: **{len(guild.emojis)}/{guild.emoji_limit}**"
        )

        await msg.edit(content=final)

    # ---------------------------------------------------------
    # EXPORT COMMAND
    # ---------------------------------------------------------

    @bsemoji_group.command(name="export")
    @checks.admin_or_permissions(manage_guild=True)
    async def bsemoji_export(self, ctx: commands.Context, prefix: str = ""):
        """
        Export emojis in the form <:name:id>.
        
        Examples:
          [p]bsemoji export
          [p]bsemoji export b_    (filter by prefix)
        """
        guild = ctx.guild
        if guild is None:
            await ctx.send("Must be used in a server.")
            return

        emojis = guild.emojis
        if prefix:
            emojis = [e for e in emojis if e.name.startswith(prefix)]

        if not emojis:
            await ctx.send("No emojis matched that filter.")
            return

        # Build lines like <:darryl:1234567890>
        lines = [f"<:{e.name}:{e.id}>" for e in emojis]
        text = "\n".join(lines)

        # Fit message or send as file
        if len(text) < 1900:
            await ctx.send(f"**Emoji Export:**\n{text}")
        else:
            fp = io.StringIO(text)
            file = discord.File(fp, filename="emojis.txt")
            await ctx.send("Emoji list attached:", file=file)
