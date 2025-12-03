from __future__ import annotations

import random
import time

import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "smollm2:135m"


class AI(commands.Cog):

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xBEEFCAFE, force_registration=True
        )

        default_guild = {
            "enabled": True,
            "chat_channels": [],        # list of channel IDs where TLG AI may talk
            "auto_reply_chance": 0.08,  # 8% chance to reply to a normal message
            "cooldown_seconds": 40,     # minimum seconds between AI replies per guild
            "last_reply_ts": 0.0,
        }
        self.config.register_guild(**default_guild)

    # ---------- Core LLM call ----------

    async def ask_ollama(self, prompt: str) -> str:
        """Send a prompt to the local Ollama server and return the response text."""
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(OLLAMA_URL, json=payload, timeout=60) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        print(f"[AIChat] Ollama HTTP {resp.status}: {text}")
                        return "My brain just desynced with the server. Try again later."
                    data = await resp.json()
        except Exception as e:
            # Log to console but don't crash the cog
            print(f"[AIChat] Error talking to Ollama: {e}")
            return "My brain lagged harder than a bad Wi-Fi lobby. Try again in a bit."

        text = data.get("response", "").strip()
        if not text:
            return "I'm not sure what to say, but it probably involves a creeper and a bad decision."
        return text

    # ---------- Admin config group ----------

    @commands.group(name="aichat")
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def aichat_group(self, ctx: commands.Context):
        """Configure TLG AI for this server (admin only)."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "Subcommands: `aichat addchannel`, `aichat removechannel`, "
                "`aichat channels`, `aichat toggle`, `aichat chance`"
            )

    @aichat_group.command(name="channels")
    async def aichat_channels(self, ctx: commands.Context):
        """List all channels where TLG AI is active."""
        ids = await self.config.guild(ctx.guild).chat_channels()
        if not ids:
            await ctx.send("TLG AI is not active in any channels.")
            return

        channels = []
        for cid in ids:
            chan = ctx.guild.get_channel(cid)
            if chan:
                channels.append(chan.mention)

        if not channels:
            await ctx.send("The configured channels no longer exist.")
        else:
            await ctx.send("TLG AI is active in: " + ", ".join(channels))

    @aichat_group.command(name="addchannel")
    async def aichat_addchannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """Add a channel where TLG AI can auto-chat."""
        ids = await self.config.guild(ctx.guild).chat_channels()
        if channel.id in ids:
            await ctx.send(f"{channel.mention} is already enabled.")
            return

        ids.append(channel.id)
        await self.config.guild(ctx.guild).chat_channels.set(ids)
        await ctx.send(f"TLG AI will now join conversations in {channel.mention}.")

    @aichat_group.command(name="removechannel")
    async def aichat_removechannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """Remove a channel from TLG AI auto-chat."""
        ids = await self.config.guild(ctx.guild).chat_channels()
        if channel.id not in ids:
            await ctx.send(f"{channel.mention} is not currently active.")
            return

        ids.remove(channel.id)
        await self.config.guild(ctx.guild).chat_channels.set(ids)
        await ctx.send(f"TLG AI will no longer speak in {channel.mention}.")

    @aichat_group.command(name="toggle")
    async def aichat_toggle(self, ctx: commands.Context):
        """Toggle TLG AI auto-chat on/off for this server."""
        enabled = await self.config.guild(ctx.guild).enabled()
        enabled = not enabled
        await self.config.guild(ctx.guild).enabled.set(enabled)
        await ctx.send(f"TLG AI auto-chat is now {'enabled' if enabled else 'disabled'}.")

    @aichat_group.command(name="chance")
    async def aichat_chance(self, ctx: commands.Context, chance: float):
        """
        Set chance (0–1) that TLG AI responds to a message in active channels.
        """
        chance = max(0.0, min(1.0, chance))
        await self.config.guild(ctx.guild).auto_reply_chance.set(chance)
        await ctx.send(f"TLG AI auto-reply chance set to **{chance:.2f}**.")

    # ---------- Admin-only direct AI command ----------

    @commands.command(name="ai")
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def ai_command(self, ctx: commands.Context, *, message: str):
        """
        Talk directly to TLG AI (admin only).
        """
        try:
            await ctx.trigger_typing()
        except discord.HTTPException:
            pass

        guild_name = ctx.guild.name if ctx.guild else "this server"
        prompt = (
            "You are TLG AI, the friendly assistant for the Discord server called "
            f"\"{guild_name}\". "
            "You help keep chat fun, welcoming, and active. "
            "You love Supercell games (Clash Royale, Brawl Stars, etc.) and Minecraft, "
            "and you talk like a casual gamer: playful, kind, never toxic or offensive.\n\n"
            "Always stay in character as TLG AI.\n\n"
            f"User: {message}\n"
            "TLG AI:"
        )

        reply = await self.ask_ollama(prompt)
        await ctx.reply(reply)

    # ---------- Passive chat listener ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Occasionally reply in the configured channels, for everyone.
        """
        # Ignore DMs and bot messages
        if message.author.bot:
            return
        if not message.guild:
            return

        guild = message.guild
        guild_conf = await self.config.guild(guild).all()

        # Auto-chat disabled?
        if not guild_conf["enabled"]:
            return

        chan_ids = guild_conf["chat_channels"]
        if not chan_ids or message.channel.id not in chan_ids:
            return

        # Ignore command messages (rough filter using valid prefixes)
        try:
            prefixes = await self.bot.get_valid_prefixes(guild)
        except TypeError:
            # some Red versions don't expect guild here, fall back
            prefixes = await self.bot.get_valid_prefixes()

        if any(message.content.startswith(p) for p in prefixes):
            return

        # Guild-level cooldown
        now = time.time()
        last = guild_conf["last_reply_ts"]
        cooldown = guild_conf["cooldown_seconds"]
        if now - last < cooldown:
            return

        # Random chance to respond
        chance = guild_conf["auto_reply_chance"]
        if chance <= 0:
            return
        if random.random() > chance:
            return

        # Passed checks: update timestamp
        await self.config.guild(guild).last_reply_ts.set(now)

        user_text = message.content[:500]  # keep it sane
        guild_name = guild.name

        prompt = (
            "You are TLG AI, a playful AI that lives in the Discord server "
            f"\"{guild_name}\". "
            "Your job is to help the community have fun, feel welcome, and keep conversations going. "
            "The server mainly plays Supercell games and Minecraft. "
            "Reply to the last user message as if you are TLG AI chatting in the channel. "
            "Keep replies short (1–3 sentences), friendly, and slightly humorous. "
            "If the user sounds frustrated, be supportive. "
            "If they share something cool, hype them up. "
            "Never be rude, offensive, or edgy.\n\n"
            f"Last message in chat: \"{user_text}\"\n"
            "TLG AI:"
        )

        try:
            await message.channel.trigger_typing()
        except discord.HTTPException:
            pass

        reply = await self.ask_ollama(prompt)
        try:
            await message.reply(reply)
        except discord.HTTPException:
            pass



