from redbot.core import commands, checks
import discord

from io import BytesIO
import asyncio

# You need these installed in your Red env:
# pip install pillow imagehash
from PIL import Image
import imagehash


class ImageSearcher(commands.Cog):
    """Search the server for images similar to a reference."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="findimage")
    @checks.admin_or_permissions(manage_guild=True)
    async def find_image(
        self,
        ctx: commands.Context,
        threshold: int = 5,
        per_channel: int = 2000,
    ):
        """
        Search this server for images similar to the one you attach.

        threshold: max Hamming distance of the perceptual hash (lower = stricter, default 5)
        per_channel: how many recent messages to scan in each text channel (default 2000)
        """
        if not ctx.message.attachments:
            await ctx.send("Attach the reference image to this command message.")
            return

        ref_attachment = ctx.message.attachments[0]

        # Load reference image
        buf = BytesIO()
        await ref_attachment.save(buf)
        buf.seek(0)
        try:
            ref_img = Image.open(buf).convert("RGB")
        except Exception:
            await ctx.send("I couldn't read that image, is it a valid picture file?")
            return

        ref_hash = imagehash.phash(ref_img)

        await ctx.send(
            f"Starting search with threshold `{threshold}` and "
            f"`{per_channel}` messages per channel. This might take a bit."
        )

        matches = []

        for channel in ctx.guild.text_channels:
            # Skip channels the bot can't see
            try:
                async for msg in channel.history(limit=per_channel, oldest_first=False):
                    if not msg.attachments:
                        continue

                    for att in msg.attachments:
                        filename = att.filename.lower()
                        if not filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                            continue

                        img_buf = BytesIO()
                        try:
                            await att.save(img_buf)
                        except discord.HTTPException:
                            # Couldn't download attachment, skip
                            continue

                        img_buf.seek(0)
                        try:
                            img = Image.open(img_buf).convert("RGB")
                        except Exception:
                            # Not actually an image, or broken file
                            continue

                        h = imagehash.phash(img)
                        distance = ref_hash - h

                        if distance <= threshold:
                            matches.append((channel.id, msg.id, distance))
                            await ctx.send(
                                f"Possible match in {channel.mention}: {msg.jump_url} "
                                f"(distance `{distance}`)"
                            )

                        # Let the event loop breathe to avoid locking up
                        await asyncio.sleep(0)
            except (discord.Forbidden, discord.HTTPException):
                # No perms or some API issue on this channel, skip it
                continue

        if not matches:
            await ctx.send("No similar images found with the current settings.")


