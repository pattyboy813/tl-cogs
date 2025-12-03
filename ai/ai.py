from __future__ import annotations

import re
import random
import time
from collections import defaultdict
from typing import Dict, List, Tuple
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

# Adjust these to match your Ollama setup
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "smollm2:135m"  # e.g. "llama3.2:1b" if your server can handle it

INVITE_REGEX = re.compile(
    r"(?:discord\.gg/|discord\.com/invite/)([A-Za-z0-9-]+)",
    re.IGNORECASE,
)

# ---------- Unified persona prompt (single prompt for everything) ----------

BASE_PROMPT = """
You are "TLG AI", a chill, playful regular in the Threat Level Gaming Discord server.
You are NOT an assistant or moderator, just another human in chat.

Who you are and how you talk:
- Sound like a real Discord gamer: casual, relaxed, sometimes a bit sarcastic but never mean.
- Default mood is upbeat unless the other person is clearly upset.
- Talk in first person ("I") and speak directly to people ("you"), not "the user".
- NEVER address yourself by name in your reply (no "Hey TLG AI").
- Use simple, everyday language; avoid formal or corporate phrasing.
- Use slang and emojis sparingly and naturally; don't spam them.

Reply style:
- Reply with ONE message as if you were sending it in the channel.
- Usually 1–2 short sentences. Only go longer if they clearly ask for detail.
- If they just greet you ("hi", "hello", "hey", "yo", etc., maybe with your name), answer with a very short casual greeting back (a few words) and maybe one emoji, nothing more.
- If they say something like "how are you", respond briefly about how you're doing and bounce the question back.
- If they seem sad or frustrated, be kind and supportive first, then maybe add a light joke.
- If they share something cool or a win, hype them up.

Hard rules:
- Do not repeat the person's message word-for-word.
- Do not start with stiff phrases like "Hello there", "Greetings", or "Good day".
- Do not talk about "the user", "the prompt", or being an AI.
- Do not explain what you are doing or list steps.
- Do not invent fake life stories for yourself (no imaginary trips, jobs, exams, vacations, campaigns, etc.).
- Stay SFW: no slurs, no NSFW content, no harsh insults.

Now respond in character as TLG AI to the last message.
"""


class AI(commands.Cog):
    """
    TLG AI - a local LLM-powered chat companion + light automod.

    - Uses a local Ollama model (no paid APIs).
    - Admin-only commands to control behavior.
    - Can occasionally join chat in configured channels.
    - Responds when pinged directly.
    - Basic automod: anti-spam + invite blocking with optional timeout.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xBEEFCAFE, force_registration=True
        )

        default_guild = {
            # AI chat config
            "enabled": True,              # AI auto-chat
            "chat_channels": [],          # list of channel IDs where TLG may auto-chat
            "auto_reply_chance": 0.08,    # 8% chance to reply to a normal message
            "cooldown_seconds": 40,       # min seconds between AI auto-replies per guild
            "last_reply_ts": 0.0,

            # Moderation settings
            "mod_enabled": True,
            "block_invites": True,
            "allowed_invite_codes": [],   # list of codes that are allowed
            "spam_messages": 6,           # messages...
            "spam_interval": 7,           # ...within this many seconds
            "invite_timeout_seconds": 600,  # timeout for bad invites (10 minutes)
        }
        self.config.register_guild(**default_guild)

        # In-memory spam tracking: (guild_id, user_id) -> [timestamps...]
        self._spam_tracker: Dict[Tuple[int, int], List[float]] = defaultdict(list)

    # ---------- Core LLM call + helpers ----------

    async def ask_ollama(self, prompt: str) -> str:
        """Send a prompt to the local Ollama server and return the response text."""
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "temperature": 0.65,  # a bit more playful
            "top_p": 0.9,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(OLLAMA_URL, json=payload, timeout=60) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        print(f"[TLG AI] Ollama HTTP {resp.status}: {text}")
                        return "My brain just desynced with the server. Try again later."
                    data = await resp.json()
        except Exception as e:
            # Log to console but don't crash the cog
            print(f"[TLG AI] Error talking to Ollama: {e}")
            return "My brain lagged harder than a bad Wi-Fi lobby. Try again in a bit."

        text = data.get("response", "").strip()
        if not text:
            return "I'm not sure what to say, but it probably involves a creeper and a bad decision."

        cleaned = self._cleanup_reply(text)
        short = self._shorten_reply(cleaned)
        return short

    def _is_simple_greeting(self, text: str) -> bool:
        """
        Detect very simple greetings so we can handle them ourselves
        instead of trusting the LLM to not be weird.
        """
        t = text.strip().lower()
        # Kill trailing punctuation
        t = re.sub(r"[!?.,]+$", "", t)

        # Remove our name if they typed it
        for name in ["tlg ai", "tlgai", "tlg", "@tlg", "@tlg ai"]:
            t = t.replace(name, "")
        t = t.strip()

        # Very short -> likely just a greeting
        if len(t) == 0:
            return True

        greetings = {"hi", "hey", "hello", "yo", "hiya", "heya", "sup"}
        return t in greetings

    def _fallback_smalltalk(self, text: str) -> str:
        """Fallback for stuff like 'hey, how are you' when the LLM is being cringe."""
        t = text.lower()
        if re.search(r"how\s+are\s+(you|u)", t) or "hru" in t or "how's it going" in t:
            options = [
                "pretty good, just vibing. you?",
                "tired but alive lmao, hbu?",
                "not bad at all, how about you?",
                "chillin as always, you good?",
            ]
            return random.choice(options)

        # Generic short answer if the model output was unusable
        options = [
            "lmao fair enough",
            "yeah I feel that 😅",
            "trueee",
            "valid tbh",
            "sounds about right 😂",
        ]
        return random.choice(options)

    def _random_greeting_reply(self) -> str:
        replies = [
            "hey 😄",
            "yo o/",
            "heyy 👋",
            "what's up?",
            "hiya 🙃",
            "hey hey",
        ]
        return random.choice(replies)

    async def generate_reply(self, user_text: str) -> str:
        """
        Decide if we handle this as a simple greeting or send it to the LLM,
        and patch over obviously bad model habits.
        """
        if self._is_simple_greeting(user_text):
            return self._random_greeting_reply()

        prompt = f"{BASE_PROMPT}\n\nLast message: {user_text}\nTLG AI:"
        reply = await self.ask_ollama(prompt)

        # If it still talks to "TLG AI" like a third person, just use a fallback.
        if "tlg ai" in reply.lower():
            return self._fallback_smalltalk(user_text)

        return reply

    def _cleanup_reply(self, text: str) -> str:
        """
        Lightly de-corporatize the reply:
        - strip super formal greetings
        - remove/replace assistant-y phrases
        - strip narrator-style junk or echoed prompt text
        - strip wrapping quotes
        """
        original = text
        t = text.strip()
        lower = t.lower()

        # Remove wrapping quotes like " ... " or ‘…’
        quote_pairs = [('\"', '\"'), ("'", "'"), ("“", "”"), ("‘", "’")]
        for ql, qr in quote_pairs:
            if t.startswith(ql) and t.endswith(qr) and len(t) > 2:
                t = t[1:-1].strip()
        # Also nuke stray leading quotes
        t = t.lstrip("\"'“”‘’").rstrip()

        lower = t.lower()

        # If the model echoed the prompt, cut off anything after these markers
        end_markers = [
            "tlg's reply (just the message):",
            "tlg's reply:",
            "\nuser:",
            "\nuser\n",
            "\nUser:",
            "\nUser\n",
        ]
        for mark in end_markers:
            idx = lower.find(mark)
            if idx != -1:
                t = t[:idx].rstrip()
                lower = t.lower()
                break

        # Kill narrator-style meta intros the model loves
        meta_prefixes = [
            "here is a short response from tlg",
            "here's a short response from tlg",
            "here is a response from tlg",
            "here’s a response from tlg",
            "here is a short response:",
            "here's a short response:",
            "and here's what they used to say:",
            "and here is what they used to say:",
        ]
        for mp in meta_prefixes:
            idx = lower.find(mp)
            if idx != -1:
                cut = idx + len(mp)
                t = t[cut:].lstrip(" :\n-")
                lower = t.lower()
                break

        # Kill very formal greeting sentences at the start
        formal_starts = (
            "hello! good day",
            "hello good day",
            "good day",
            "hello there",
            "hello!",
            "hello.",
            "hello,",
            "hi there",
            "greetings",
        )
        for fs in formal_starts:
            if lower.startswith(fs):
                sentence_end = len(t)
                for ch in [".", "!", "?", "\n"]:
                    idx = t.find(ch)
                    if idx != -1:
                        sentence_end = min(sentence_end, idx + 1)
                t = t[sentence_end:].lstrip()
                lower = t.lower()
                break

        # Replace some obvious assistant phrases / polite filler
        replacements = {
            "I'm here to help with the fun stuff": "I'm just here hanging out with everyone",
            "I'm here to help with the fun stuff!": "I'm just here hanging out with everyone!",
            "I'm here to help with the fun stuff.": "I'm just here hanging out with everyone.",
            "I'm here to help with the fun": "I'm just here hanging out",
            "I'm here to help": "I'm just chilling here with everyone",
            "I am here to help": "I'm just chilling here with everyone",
            "AI assistant": "gremlin in this server",
            "assistant for the games and challenges": "goblin that won't stop talking about games",
            "Hope all is well with you and your campaign.": "",
            "Hope all is well with you and your": "",
            "Hope all is well with you.": "",
            "Hope all is well.": "",
        }
        for old, new in replacements.items():
            t = t.replace(old, new)

        if not t.strip():
            return original.strip()
        return t.strip()

    def _shorten_reply(self, text: str, max_sentences: int = 2, max_chars: int = 220) -> str:
        """
        Keep the reply short: max N sentences and max length.
        """
        parts = re.split(r'(?<=[.!?])\s+', text)
        if len(parts) > max_sentences:
            text = " ".join(parts[:max_sentences])

        if len(text) > max_chars:
            cut = text.rfind(" ", 0, max_chars)
            if cut == -1:
                cut = max_chars
            text = text[:cut].rstrip()

        return text

    # ---------- Moderation helpers ----------

    async def handle_invites(
        self,
        message: discord.Message,
        guild_conf: dict,
    ) -> bool:
        """
        Detect and block invite links if needed.
        Returns True if the message was moderated (deleted/blocked).
        """
        if not guild_conf.get("mod_enabled", True):
            return False
        if not guild_conf.get("block_invites", True):
            return False

        content = message.content
        matches = INVITE_REGEX.findall(content)
        if not matches:
            return False

        allowed_codes = set(code.lower() for code in guild_conf.get("allowed_invite_codes", []))
        timeout_seconds = int(guild_conf.get("invite_timeout_seconds", 0))

        # If ANY invite in the message is not whitelisted, block the message
        for code in matches:
            if code.lower() not in allowed_codes:
                # Delete the message
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

                # Warn in channel
                try:
                    await message.channel.send(
                        f"{message.author.mention} Discord invite links aren't allowed here."
                    )
                except discord.HTTPException:
                    pass

                # Optional timeout
                if (
                    timeout_seconds > 0
                    and isinstance(message.author, discord.Member)
                    and message.guild is not None
                ):
                    me = message.guild.me
                    if me and me.guild_permissions.moderate_members:
                        until = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
                        try:
                            await message.author.edit(
                                timed_out_until=until,
                                reason="Posting disallowed Discord invite link.",
                            )
                        except discord.HTTPException:
                            pass

                return True

        return False

    async def handle_spam(
        self,
        message: discord.Message,
        guild_conf: dict,
    ) -> bool:
        """
        Basic per-user spam detection.
        Returns True if the message was moderated.
        """
        if not guild_conf.get("mod_enabled", True):
            return False

        max_msgs = int(guild_conf.get("spam_messages", 6))
        interval = float(guild_conf.get("spam_interval", 7))

        if max_msgs <= 0 or interval <= 0:
            return False

        now = time.time()
        key = (message.guild.id, message.author.id)
        timestamps = self._spam_tracker[key]

        cutoff = now - interval
        timestamps = [t for t in timestamps if t >= cutoff]
        timestamps.append(now)
        self._spam_tracker[key] = timestamps

        if len(timestamps) > max_msgs:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                await message.channel.send(
                    f"{message.author.mention} you're sending messages too fast, slow down a bit."
                )
            except discord.HTTPException:
                pass
            return True

        return False

    # ---------- Admin config group ----------

    @commands.group(name="aichat")
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def aichat_group(self, ctx: commands.Context):
        """Configure TLG AI and automod for this server (admin only)."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "Subcommands: `aichat addchannel`, `aichat removechannel`, "
                "`aichat channels`, `aichat toggle`, `aichat chance`, "
                "`aichat modtoggle`, `aichat blockinvites`, `aichat allowinvite`, "
                "`aichat removeinvite`, `aichat listinvites`, `aichat spamlimit`, "
                "`aichat invitetimeout`"
            )

    # ----- AI channel config -----

    @aichat_group.command(name="channels")
    async def aichat_channels(self, ctx: commands.Context):
        """List all channels where TLG auto-chat is active."""
        ids = await self.config.guild(ctx.guild).chat_channels()
        if not ids:
            await ctx.send("TLG isn't auto-chatting in any channels.")
            return

        channels = []
        for cid in ids:
            chan = ctx.guild.get_channel(cid)
            if chan:
                channels.append(chan.mention)

        if not channels:
            await ctx.send("The configured channels no longer exist.")
        else:
            await ctx.send("TLG auto-chats in: " + ", ".join(channels))

    @aichat_group.command(name="addchannel")
    async def aichat_addchannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """Add a channel where TLG may auto-chat."""
        ids = await self.config.guild(ctx.guild).chat_channels()
        if channel.id in ids:
            await ctx.send(f"{channel.mention} is already enabled.")
            return

        ids.append(channel.id)
        await self.config.guild(ctx.guild).chat_channels.set(ids)
        await ctx.send(f"TLG will now sometimes join conversations in {channel.mention}.")

    @aichat_group.command(name="removechannel")
    async def aichat_removechannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """Remove a channel from TLG auto-chat."""
        ids = await self.config.guild(ctx.guild).chat_channels()
        if channel.id not in ids:
            await ctx.send(f"{channel.mention} is not currently active.")
            return

        ids.remove(channel.id)
        await self.config.guild(ctx.guild).chat_channels.set(ids)
        await ctx.send(f"TLG will no longer speak in {channel.mention}.")

    @aichat_group.command(name="toggle")
    async def aichat_toggle(self, ctx: commands.Context):
        """Toggle TLG auto-chat on/off for this server."""
        enabled = await self.config.guild(ctx.guild).enabled()
        enabled = not enabled
        await self.config.guild(ctx.guild).enabled.set(enabled)
        await ctx.send(f"TLG auto-chat is now {'enabled' if enabled else 'disabled'}.")

    @aichat_group.command(name="chance")
    async def aichat_chance(self, ctx: commands.Context, chance: float):
        """
        Set chance (0–1) that TLG responds to a message in active channels.
        """
        chance = max(0.0, min(1.0, chance))
        await self.config.guild(ctx.guild).auto_reply_chance.set(chance)
        await ctx.send(f"TLG auto-reply chance set to **{chance:.2f}**.")

    # ----- Moderation config -----

    @aichat_group.command(name="modtoggle")
    async def aichat_modtoggle(self, ctx: commands.Context):
        """Toggle TLG's automod features on/off."""
        conf = await self.config.guild(ctx.guild).all()
        current = conf.get("mod_enabled", True)
        new_val = not current
        await self.config.guild(ctx.guild).mod_enabled.set(new_val)
        await ctx.send(f"TLG automod is now {'enabled' if new_val else 'disabled'}.")

    @aichat_group.command(name="blockinvites")
    async def aichat_blockinvites(self, ctx: commands.Context, toggle: bool):
        """Enable or disable blocking of Discord invite links."""
        await self.config.guild(ctx.guild).block_invites.set(toggle)
        await ctx.send(
            f"Blocking of Discord invites is now {'enabled' if toggle else 'disabled'}."
        )

    @aichat_group.command(name="allowinvite")
    async def aichat_allowinvite(self, ctx: commands.Context, code: str):
        """
        Add an invite code that is allowed (e.g. 'abcdef' from 'discord.gg/abcdef').
        """
        code = code.strip()
        ids = await self.config.guild(ctx.guild).allowed_invite_codes()
        if code.lower() in (c.lower() for c in ids):
            await ctx.send(f"Invite code `{code}` is already allowed.")
            return

        ids.append(code)
        await self.config.guild(ctx.guild).allowed_invite_codes.set(ids)
        await ctx.send(f"Invite code `{code}` added to the allowlist.")

    @aichat_group.command(name="removeinvite")
    async def aichat_removeinvite(self, ctx: commands.Context, code: str):
        """Remove an invite code from the allowlist."""
        code = code.strip()
        ids = await self.config.guild(ctx.guild).allowed_invite_codes()
        lower_ids = [c.lower() for c in ids]
        if code.lower() not in lower_ids:
            await ctx.send(f"Invite code `{code}` is not on the allowlist.")
            return

        new_ids = [c for c in ids if c.lower() != code.lower()]
        await self.config.guild(ctx.guild).allowed_invite_codes.set(new_ids)
        await ctx.send(f"Invite code `{code}` removed from the allowlist.")

    @aichat_group.command(name="listinvites")
    async def aichat_listinvites(self, ctx: commands.Context):
        """List all allowed invite codes."""
        ids = await self.config.guild(ctx.guild).allowed_invite_codes()
        if not ids:
            await ctx.send("No invite codes are allowed; all invites are blocked.")
            return

        await ctx.send("Allowed invite codes: " + ", ".join(f"`{c}`" for c in ids))

    @aichat_group.command(name="spamlimit")
    async def aichat_spamlimit(
        self,
        ctx: commands.Context,
        messages: int,
        seconds: int,
    ):
        """
        Set spam limit: <messages> per <seconds>.
        Example: aichat spamlimit 6 7
        """
        messages = max(1, messages)
        seconds = max(1, seconds)
        await self.config.guild(ctx.guild).spam_messages.set(messages)
        await self.config.guild(ctx.guild).spam_interval.set(seconds)
        await ctx.send(
            f"Spam limit set to **{messages} messages per {seconds} seconds**."
        )

    @aichat_group.command(name="invitetimeout")
    async def aichat_invitetimeout(self, ctx: commands.Context, seconds: int):
        """
        Set timeout length (in seconds) for users who post blocked invite links.
        Use 0 to disable timeouts (still deletes the message).
        """
        seconds = max(0, seconds)
        await self.config.guild(ctx.guild).invite_timeout_seconds.set(seconds)
        if seconds == 0:
            await ctx.send(
                "Users will no longer be timed out for blocked invite links "
                "(messages are still deleted)."
            )
        else:
            await ctx.send(
                f"Users will now be timed out for **{seconds} seconds** when posting blocked invite links."
            )

    # ---------- Admin-only direct AI command ----------

    @commands.command(name="ai")
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def ai_command(self, ctx: commands.Context, *, message: str):
        """
        Talk directly to TLG AI (admin only).
        """
        async with ctx.typing():
            reply = await self.generate_reply(message)

        await ctx.reply(reply)

    # ---------- Passive chat listener (mention + auto-chat + automod) ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        - Runs basic automod (invites + spam) if enabled.
        - If someone mentions the bot in a message (anywhere) and it's not a command,
          TLG AI replies directly.
        - Otherwise, it may occasionally auto-reply in configured channels.
        """
        if message.author.bot:
            return
        if not message.guild:
            return

        guild = message.guild
        guild_conf = await self.config.guild(guild).all()

        # --- Automod first ---
        if await self.handle_invites(message, guild_conf):
            return
        if await self.handle_spam(message, guild_conf):
            return

        content = message.content or ""
        content_stripped = content.lstrip()

        # Figure out prefixes once so we can avoid answering inside commands
        try:
            prefixes = await self.bot.get_valid_prefixes(guild)
        except TypeError:
            prefixes = await self.bot.get_valid_prefixes()
        is_command_like = any(content.startswith(p) for p in prefixes)

        # --- Direct mention detection: reply if bot is mentioned anywhere ---
        bot_user = self.bot.user
        if bot_user and (bot_user in message.mentions) and not is_command_like:
            # Remove the first occurrence of the bot mention from the text
            mention_pattern = rf"<@!?{bot_user.id}>"
            user_text = re.sub(mention_pattern, "", content, count=1).strip()
            if not user_text:
                user_text = "hi"  # pure ping → treat as greeting

            try:
                await message.channel.trigger_typing()
            except discord.HTTPException:
                pass

            reply = await self.generate_reply(user_text)
            try:
                await message.reply(reply)
            except discord.HTTPException:
                pass

            return  # don't auto-chat on the same message

        # --- Auto-chat behavior ---
        if not guild_conf.get("enabled", True):
            return

        chan_ids = guild_conf.get("chat_channels", [])
        if not chan_ids or message.channel.id not in chan_ids:
            return

        # Ignore command messages in auto-chat mode
        if is_command_like:
            return

        # Guild-level cooldown
        now = time.time()
        last = guild_conf.get("last_reply_ts", 0.0)
        cooldown = float(guild_conf.get("cooldown_seconds", 40))
        if now - last < cooldown:
            return

        # Random chance to respond
        chance = float(guild_conf.get("auto_reply_chance", 0.08))
        if chance <= 0 or random.random() > chance:
            return

        # Passed checks: update timestamp
        await self.config.guild(guild).last_reply_ts.set(now)

        user_text = content[:500]  # keep it sane

        try:
            await message.channel.trigger_typing()
        except discord.HTTPException:
            pass

        reply = await self.generate_reply(user_text)
        try:
            await message.reply(reply)
        except discord.HTTPException:
            pass
