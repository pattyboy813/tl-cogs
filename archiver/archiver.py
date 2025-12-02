import asyncio
import io
from typing import Optional, List

import discord
from redbot.core import commands, checks, Config


__author__ = "DABIGMANPATTY"
__version__ = "1.3.0"


DEFAULT_GUILD = {
    "management_guild_id": None,         # Optional[int]
    "management_category_id": None,      # Optional[int]
    "delete_after_archive": True,        # bool
}


class ChannelArchiver(commands.Cog):
    """
    Archive a channel to a management server: create a channel with the same
    name, copy all messages (content + attachments) via a webhook to preserve
    author names/avatars, then delete the original channel.

    ⚠️ Requires bot permissions:
      - Read Message History
      - Manage Webhooks (in destination channel)
      - Manage Channels (to create and delete channels)
      - Send Messages, Embed Links, Attach Files
    """

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA4C11FEE, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)

    # ---------- Helpers ----------

    async def _get_or_create_category(
        self,
        dest_guild: discord.Guild,
        name: Optional[str],
        fallback_category_id: Optional[int],
    ) -> Optional[discord.CategoryChannel]:
        """
        Return a CategoryChannel in dest_guild.
        - If 'name' is provided, get-or-create a category with that name.
        - Else if 'fallback_category_id' is provided, return that category if valid.
        - Else return None (root).
        """
        if name:
            for c in dest_guild.categories:
                if c.name.lower() == name.lower():
                    return c
            try:
                return await dest_guild.create_category(name=name, reason="Archive destination category")
            except Exception:
                return None

        if fallback_category_id:
            cat = dest_guild.get_channel(int(fallback_category_id))
            if isinstance(cat, discord.CategoryChannel):
                return cat
        return None

    async def _archive_channel(
        self,
        ctx: commands.Context,
        src_channel: discord.TextChannel,
        *,
        dest_guild: discord.Guild,
        dest_category_name: Optional[str] = None,
        use_configured_category: bool = True,
        delete_after: bool = True,
    ) -> Optional[discord.TextChannel]:
        """
        Archive a single text channel to dest_guild.
        Returns the created dest channel or None on failure.
        """
        src_guild: discord.Guild = src_channel.guild
        settings = await self.config.guild(src_guild).all()
        cat_id = settings.get("management_category_id") if use_configured_category else None

        status_msg = await ctx.send(
            f"🚚 Archiving **#{src_channel.name}** …\n"
            f"📦 Archived **0** messages so far."
        )

        # Destination category
        dest_category = await self._get_or_create_category(dest_guild, dest_category_name, cat_id)
        if dest_category_name and not dest_category:
            await status_msg.edit(content="❌ Could not create/find destination category.")
            return None

        # Create destination text channel with same name
        try:
            kwargs = {
                "name": src_channel.name,
                "reason": f"Archive of #{src_channel.name} from {src_guild.name}",
            }
            if dest_category:
                kwargs["category"] = dest_category
            dest_channel = await dest_guild.create_text_channel(**kwargs)
        except discord.Forbidden:
            await status_msg.edit(content="❌ Missing permission to create channel in management server.")
            return None
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to create destination channel: {e}")
            return None

        # Create a webhook to mirror author name & avatar
        try:
            webhook = await dest_channel.create_webhook(name=f"ArchiveMirror-{src_channel.name}")
        except discord.Forbidden:
            await status_msg.edit(content="❌ Missing Manage Webhooks in destination channel.")
            try:
                await dest_channel.delete(reason="Archive failed: webhook creation forbidden")
            except Exception:
                pass
            return None
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to create webhook in destination channel: {e}")
            try:
                await dest_channel.delete(reason="Archive failed: webhook creation error")
            except Exception:
                pass
            return None

        # Header message in destination
        try:
            await dest_channel.send(
                embed=discord.Embed(
                    title="Channel Archived",
                    description=(
                        f"Archived from **{src_guild.name}** `{src_guild.id}`\n"
                        f"Source channel: **#{src_channel.name}** `{src_channel.id}`\n"
                        f"Messages are replayed below using a webhook to preserve author names and avatars."
                    ),
                    color=discord.Color.blurple(),
                ).set_footer(text=f"Started by {ctx.author} ({ctx.author.id})")
            )
        except Exception:
            # Not fatal, but log to status
            await status_msg.edit(content="⚠️ Created destination channel, but failed to send header embed.")

        # Copy messages oldest -> newest
        total = 0
        try:
            async for message in src_channel.history(limit=None, oldest_first=True):
                # Skip our own status message if it's actually in this channel
                if message.id == status_msg.id and message.channel.id == src_channel.id:
                    continue

                username = f"{message.author.display_name}"
                avatar_url = getattr(message.author.display_avatar, "url", None) or None

                # Timestamp: plain text for guaranteed rendering across clients
                ts = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
                header = f"[{ts}]"
                content = (message.content or "").strip()
                final_text = f"{header} {content}" if content else header

                # Attachments: download and re-upload
                files: List[discord.File] = []
                for att in message.attachments:
                    try:
                        buf = io.BytesIO()
                        await att.save(buf)
                        buf.seek(0)
                        files.append(discord.File(buf, filename=att.filename))
                    except Exception:
                        final_text += f"\n[Attachment could not be mirrored, original URL]({att.url})"

                # Embeds: best-effort copy, but don't exceed Discord limits
                embeds: List[discord.Embed] = []
                for em in message.embeds[:10]:  # Discord max 10 embeds per message
                    try:
                        e = discord.Embed.from_dict(em.to_dict())
                        embeds.append(e)
                    except Exception:
                        pass

                async def send_chunk(chunk_text: str, first: bool = False):
                    safe_content = chunk_text if chunk_text is not None else ""
                    payload_embeds = embeds if (first and embeds) else []
                    payload_files = files if (first and files) else []

                    try:
                        await webhook.send(
                            content=safe_content,
                            username=username,
                            avatar_url=avatar_url,
                            embeds=payload_embeds,
                            files=payload_files,
                            allowed_mentions=discord.AllowedMentions.none(),
                            wait=True,
                        )
                    except discord.HTTPException:
                        # Skip this message but keep going
                        pass

                # Chunking for long content
                if final_text and isinstance(final_text, str) and len(final_text) > 2000:
                    remaining = final_text
                    first = True
                    while remaining:
                        piece = remaining[:2000]
                        if len(remaining) > 2000:
                            cut = piece.rfind("\n")
                            if cut < 1000:
                                cut = piece.rfind(" ")
                            if cut < 1:
                                cut = 2000
                            piece = remaining[:cut]
                            remaining = remaining[cut:]
                        else:
                            remaining = ""
                        await send_chunk(piece, first=first)
                        first = False
                else:
                    await send_chunk(final_text, first=True)

                total += 1

                # Progress updates every 25 messages with date info
                if total % 25 == 0:
                    date_str = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        await status_msg.edit(
                            content=(
                                f"🚚 Archiving **#{src_channel.name}** …\n"
                                f"📦 Archived **{total:,}** messages so far.\n"
                                f"🕒 Currently at messages from **{date_str}**."
                            )
                        )
                    except Exception:
                        pass

                # Gentle pacing to reduce global rate limits (only every 50 msgs)
                if total % 50 == 0:
                    await asyncio.sleep(0.1)

        except discord.Forbidden:
            await status_msg.edit(content="❌ I don't have permission to read the channel history.")
            try:
                await webhook.delete(reason="Archive aborted: read history forbidden")
            except Exception:
                pass
            return None
        except asyncio.TimeoutError:
            await status_msg.edit(
                content=f"⚠️ Archive hit a network timeout after {total} messages. "
                        f"Some messages may not have been mirrored."
            )
            try:
                await webhook.delete(reason="Archive aborted: timeout")
            except Exception:
                pass
            return None
        except Exception as e:
            await status_msg.edit(content=f"⚠️ Archive encountered an error after {total} messages: {e}")
            try:
                await webhook.delete(reason="Archive aborted: error")
            except Exception:
                pass
            return None
        finally:
            # If we got here without the above returns, try to clean the webhook
            try:
                await webhook.delete(reason="Archive complete")
            except Exception:
                pass

        await dest_channel.send(f"✅ Archive complete. Mirrored **{total:,}** messages.")
        await status_msg.edit(
            content=(
                f"✅ Archive complete for **#{src_channel.name}**.\n"
                f"📦 Mirrored **{total:,}** messages to {dest_guild.name} → {dest_channel.mention}."
            )
        )

        if delete_after:
            try:
                await src_channel.delete(reason=f"Archived to {dest_guild.name} by {ctx.author} ({ctx.author.id})")
            except discord.Forbidden:
                await ctx.send("⚠️ Archive succeeded but I couldn't delete the source channel (missing Manage Channels).")
            except Exception as e:
                await ctx.send(f"⚠️ Archive succeeded but failed to delete source channel: {e}")

        return dest_channel

    # ---------- Admin setup commands ----------

    @commands.group(name="archiveset")
    @checks.admin()
    @commands.guild_only()
    async def archiveset(self, ctx: commands.Context):
        """Configure where archives go and behavior."""
        if ctx.invoked_subcommand is None:
            data = await self.config.guild(ctx.guild).all()
            mg = data.get("management_guild_id")
            cat = data.get("management_category_id")
            await ctx.send(
                f"Management guild: {mg if mg else 'not set'}\n"
                f"Management category: {cat if cat else 'not set'}\n"
                f"Delete after archive: {data.get('delete_after_archive')}"
            )

    @archiveset.command(name="guild")
    async def archiveset_guild(self, ctx: commands.Context, management_guild_id: int):
        """Set the destination (management) server ID."""
        await self.config.guild(ctx.guild).management_guild_id.set(management_guild_id)
        await ctx.send(f"✅ Set management guild ID to `{management_guild_id}`.")

    @archiveset.command(name="category")
    async def archiveset_category(self, ctx: commands.Context, category_id: Optional[int] = None):
        """Set (or clear) the category ID in the management server for new channels."""
        await self.config.guild(ctx.guild).management_category_id.set(category_id)
        await ctx.send(
            f"✅ Set management category ID to `{category_id}`." if category_id else "✅ Cleared management category."
        )

    @archiveset.command(name="delete")
    async def archiveset_delete(self, ctx: commands.Context, delete_after: bool):
        """Choose whether to delete the original channel after archiving (default: True)."""
        await self.config.guild(ctx.guild).delete_after_archive.set(delete_after)
        await ctx.send(f"✅ Delete after archive set to `{delete_after}`.")

    # ---------- Archive commands ----------

    @commands.command(name="archive")
    @checks.admin()
    @commands.guild_only()
    async def archive(self, ctx: commands.Context, *, confirm: Optional[str] = None):
        """
        Archive *this* channel to the configured management server.

        Usage: `[p]archive` — prompts for confirmation
               `[p]archive yes` — skip confirmation
        """
        src_channel: discord.TextChannel = ctx.channel
        settings = await self.config.guild(ctx.guild).all()
        mg_id = settings.get("management_guild_id")
        delete_after = settings.get("delete_after_archive", True)

        if not mg_id:
            return await ctx.send("❌ Management guild ID is not set. Use `[p]archiveset guild <id>`.")

        dest_guild = self.bot.get_guild(int(mg_id))
        if not dest_guild:
            return await ctx.send("❌ I am not in the management guild or it is unavailable.")

        if confirm is None or confirm.lower() not in {"y", "yes", "confirm", "--force"}:
            return await ctx.send(
                "⚠️ This will copy all messages & attachments to the management server and "
                + ("**delete this channel**" if delete_after else "**keep this channel**")
                + ".\nType `[p]archive yes` to proceed."
            )

        await self._archive_channel(
            ctx,
            src_channel,
            dest_guild=dest_guild,
            dest_category_name=None,          # use configured management_category_id (if set)
            use_configured_category=True,
            delete_after=delete_after,
        )

    @commands.command(name="archivecategory", aliases=["archivecat", "archive_category"])
    @checks.admin()
    @commands.guild_only()
    async def archivecategory(self, ctx: commands.Context, *, confirm: Optional[str] = None):
        """
        Archive **all text channels** in the current channel's category, except the channel
        you are running the command from (so we have somewhere to report progress).

        Destination category on the management server will be named: `ARCHIVE | <CategoryName>`.
        """
        src_category = ctx.channel.category
        if not src_category:
            return await ctx.send("❌ This channel isn't in a category.")

        settings = await self.config.guild(ctx.guild).all()
        mg_id = settings.get("management_guild_id")
        delete_after = settings.get("delete_after_archive", True)

        if not mg_id:
            return await ctx.send("❌ Management guild ID is not set. Use `[p]archiveset guild <id>`.")

        dest_guild = self.bot.get_guild(int(mg_id))
        if not dest_guild:
            return await ctx.send("❌ I am not in the management guild or it is unavailable.")

        # Only text channels, and skip the current one so it isn't deleted mid-archive
        text_channels = [
            c for c in src_category.channels
            if isinstance(c, discord.TextChannel) and c.id != ctx.channel.id
        ]
        if not text_channels:
            return await ctx.send("ℹ️ No text channels (other than this one) found in this category.")

        dest_cat_name = f"ARCHIVE | {src_category.name}"

        if confirm is None or confirm.lower() not in {"y", "yes", "confirm", "--force"}:
            return await ctx.send(
                f"⚠️ This will archive **{len(text_channels)}** channel(s) from category **{src_category.name}** "
                f"to `{dest_cat_name}` on the management server and "
                + ("**delete them**" if delete_after else "**keep them**")
                + ".\nType `[p]archivecategory yes` to proceed."
            )

        progress_msg = await ctx.send(
            f"📂 Starting category archive of **{len(text_channels)}** channel(s) "
            f"from **{src_category.name}** to `{dest_cat_name}`.\n"
            f"Channels will be processed one at a time (this may take a while for large histories)."
        )

        successes = 0
        total_channels = len(text_channels)

        for idx, ch in enumerate(text_channels, start=1):
            try:
                await progress_msg.edit(
                    content=(
                        f"📂 Category archive in progress…\n"
                        f"▶️ Now archiving channel **{idx}/{total_channels}**: **#{ch.name}**\n"
                        f"Channels are processed sequentially to avoid overloading the bot."
                    )
                )
            except Exception:
                pass

            dest = await self._archive_channel(
                ctx,
                ch,
                dest_guild=dest_guild,
                dest_category_name=dest_cat_name,   # force ARCHIVE | Category
                use_configured_category=False,
                delete_after=delete_after,
            )
            if dest is not None:
                successes += 1

            # brief pause between channels to reduce rate limits and CPU spikes
            await asyncio.sleep(1.0)

        failures = total_channels - successes

        try:
            await progress_msg.edit(
                content=(
                    f"✅ Category archive finished.\n"
                    f"📦 Channels archived successfully: **{successes}/{total_channels}**.\n"
                    f"❌ Failed: **{failures}**.\n"
                    f"Destination category: `{dest_cat_name}`."
                )
            )
        except Exception:
            await ctx.send(
                f"✅ Category archive finished: {successes} succeeded, {failures} failed. "
                f"Destination category: `{dest_cat_name}`."
            )
