# bsemoji/bsemoji.py

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
    Import Brawl Stars brawler emojis from Brawlify's CDN.
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

    # --------------------------
    # Internal helpers
    # --------------------------

    async def _get_brawlers(self) -> List[Dict[str, Any]]:
        """
        Fetch the list of brawlers from BrawlAPI.
        We don't know the exact top-level key, so we handle a few cases.
        """
        if self.session is None:
            self.session = aiohttp.ClientSession()

        async with self.session.get(BRAWLERS_API) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"BrawlAPI error {resp.status}: {text}")

            data = await resp.json()

        # Try common shapes: {"list": [...]}, {"brawlers":[...]}, or just [...]
        if isinstance(data, list):
            return data

        for key in ("list", "brawlers", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]

        # Fallback to "all values flattened" if necessary
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    return v

        raise RuntimeError("Unexpected brawler JSON structure from BrawlAPI")

    @staticmethod
    def _emoji_name_from_brawler(name: str) -> str:
        """
        Turn 'El Primo' -> 'elprimo', 'Mr. P' -> 'mrp' etc.
        Discord emoji rules: 2-32 chars, a-z 0-9 underscore.
        """
        # lower, remove spaces / hyphens / dots
        raw = name.lower().replace(" ", "").replace("-", "").replace(".", "")
        # keep only allowed chars
        allowed = re.sub(r"[^a-z0-9_]", "", raw)
        if not allowed:
            allowed = "brawler"
        return allowed[:32]

    async def _download_image(self, url: str) -> bytes:
        if self.session is None:
            self.session = aiohttp.ClientSession()

        async with self.session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Emoji fetch failed {resp.status} for {url}")
            return await resp.read()

    # --------------------------
    # Command group
    # --------------------------

    @commands.group(name="bsemoji")
    async def bsemoji_group(self, ctx: commands.Context):
        """
        Brawl Stars emoji importer.
        """
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    # --------------------------
    # Import command
    # --------------------------

    @bsemoji_group.command(name="import")
    @checks.admin_or_permissions(manage_guild=True)
    async def bsemoji_import(
        self,
        ctx: commands.Context,
        limit: int = 0,
        overwrite: bool = False,
    ):
        """
        Import brawler emojis from Brawlify CDN into this server.

        Usage:
          [p]bsemoji import
          [p]bsemoji import 30
          [p]bsemoji import 0 true   (0 = no limit, true = overwrite)

        - `limit`  : How many brawlers to import (0 = all).
        - `overwrite` : If true, replace existing emojis with the same name.
        """
        guild: discord.Guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        perms = guild.me.guild_permissions
        if not perms.manage_emojis_and_stickers:
            await ctx.send(
                "I need the **Manage Emojis and Stickers** permission "
                "to upload emojis here."
            )
            return

        try:
            brawlers = await self._get_brawlers()
        except Exception as e:
            await ctx.send(f"Failed to fetch brawlers: `{e}`")
            return

        if limit > 0:
            brawlers = brawlers[:limit]

        # Map existing emoji names for quick lookup
        existing_by_name = {e.name: e for e in guild.emojis}

        # Quick capacity check
        max_emojis = guild.emoji_limit  # static emoji cap
        current_count = len(guild.emojis)
        available_slots = max_emojis - current_count
        total_to_process = len(brawlers)

        if available_slots <= 0 and not overwrite:
            await ctx.send("This server has no emoji slots left.")
            return

        msg = await ctx.send(
            f"Starting import of **{total_to_process}** brawlers...\n"
            f"Current emojis: {current_count}/{max_emojis}"
        )

        created = 0
        replaced = 0
        skipped = 0
        failed = 0

        for idx, b in enumerate(brawlers, start=1):
            # BrawlAPI should have id + name
            b_id = b.get("id")
            b_name = b.get("name") or f"id{b_id}"

            if b_id is None:
                skipped += 1
                continue

            emoji_name = self._emoji_name_from_brawler(b_name)

            # Capacity guard if not overwriting
            if not overwrite and len(guild.emojis) >= max_emojis:
                skipped += 1
                continue

            # If emoji already exists
            existing = existing_by_name.get(emoji_name)
            if existing and not overwrite:
                skipped += 1
                continue

            url = f"{EMOJI_BASE}/{b_id}.png"

            try:
                image_bytes = await self._download_image(url)
            except Exception:
                failed += 1
                continue

            try:
                if existing and overwrite:
                    # delete and recreate
                    try:
                        await existing.delete(reason="BSEmoji overwrite")
                    except discord.HTTPException:
                        pass  # ignore if we can't delete

                    new_emoji = await guild.create_custom_emoji(
                        name=emoji_name, image=image_bytes
                    )
                    existing_by_name[emoji_name] = new_emoji
                    replaced += 1
                else:
                    new_emoji = await guild.create_custom_emoji(
                        name=emoji_name, image=image_bytes
                    )
                    existing_by_name[emoji_name] = new_emoji
                    created += 1

            except discord.HTTPException:
                failed += 1
                continue

            # Gentle rate limit safety buffer
            await asyncio.sleep(0.3)

        final = (
            f"✅ **Import finished.**\n"
            f"Created: **{created}**\n"
            f"Replaced: **{replaced}**\n"
            f"Skipped: **{skipped}**\n"
            f"Failed: **{failed}**\n"
            f"Now at: **{len(guild.emojis)}/{guild.emoji_limit}** emojis"
        )

        await msg.edit(content=final)
