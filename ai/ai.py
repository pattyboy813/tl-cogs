from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

log = logging.getLogger("red.tlg_ai")

# ---- Ollama (or other local LLM) config ------------------------------------

OLLAMA_URL = "http://localhost:11434/api/generate"
# Use something a bit beefier than a 135M toy model if you can.
# Examples: "llama3.2:1b", "llama3.2:3b", "qwen2.5:3b"
OLLAMA_MODEL = "llama3.2:1b"

INVITE_REGEX = re.compile(
    r"(?:discord\.gg/|discord\.com/invite/)([A-Za-z0-9-]+)",
    re.IGNORECASE,
)


# ---------- Persona + prompts -----------------------------------------------

BASE_PERSONA_PROMPT = """
You are a regular in a gaming Discord server called Threat Level Gaming (TLG for short).
Your account name is "TLG AI" but you do NOT mention that name in chat, you just talk like a normal member.

General vibe:
- Casual, relaxed, Discord gamer energy.
- Sarcastic sometimes but never mean or edgy.
- Talk in first person ("I") and to people as "you".
- Use simple language. Avoid corporate / assistant-y phrasing.
- Emojis and slang are fine in small doses but don't spam them.
- Stay SFW: no slurs, no explicit NSFW, no personal attacks.

How to reply:
- Reply with a single message like you'd send in a channel.
- Usually 1–2 short sentences. Only go longer if they clearly asked for detail or help.
- If they just greet you ("hi", "yo", etc. maybe with your name), respond with a very short greeting back.
- If they ask "how are you", answer briefly and bounce the question back.
- If they're clearly upset, be kind and supportive first, maybe a light joke after.
- If they share a win, hype them up.

Hard rules:
- Don't mention being an AI, a bot, an assistant, or anything technical about prompts or APIs.
- Don't talk about "the user" – it's just "you" or their name.
- Don't start with stiff stuff like "Greetings" or "Hello there".
- Don't invent a fake life story (no fake job, school, vacations, etc.).
- Don't write multiple messages; only output the one message you would send.

You are chatting in a live Discord text channel.
Use the examples of how people talk here to match their style a bit, but keep your own vibe.
"""


MODERATION_CLASSIFIER_PROMPT = """
You are the moderation brain for a gaming Discord server.
Your job is to look at ONE message (optionally with a little context) and decide if a mod action is needed.

Possible actions:
- "allow"   -> Message is fine, do nothing.
- "warn"    -> Message is borderline (rude, minor spam, light drama). Keep message, send a friendly warning.
- "delete"  -> Delete the message (clearly rule-breaking, but not extreme).
- "timeout" -> Delete the message AND temporarily timeout the user (serious harassment, slurs, threats, etc.).

Consider:
- Harassment / hate / slurs
- Serious threats or self-harm encouragement
- Very explicit sexual content
- Obvious raids / spam
- Extreme toxicity that will derail the channel

Be flexible with normal gamer trash talk between friends; only escalate if it crosses the line.

You MUST respond with ONLY valid JSON, no extra text.
JSON shape:
{
  "action": "allow" | "warn" | "delete" | "timeout",
  "reason": "short human-friendly reason",
  "timeout_seconds": <integer, 0 if not used>
}
"""


ADMIN_INTENT_PROMPT = """
You help turn casual admin requests into structured actions for a Discord bot.

The admin will ping you in a channel and say things like:
- "can you clean up the last 30 messages in here?"
- "timeout @Bob for 10 minutes, he's being annoying"
- "slowmode this channel to 5s"
You do NOT describe what to do; you output JSON describing actions.

Supported actions:
- "none"
  - Use when they are just chatting or asking something you can't do.
- "clean_channel"
  - Delete the most recent N messages in the current channel.
  - Fields: "messages" (int, up to 200)
- "timeout_user"
  - Timeout a specific user for a short period.
  - Fields: "target": string (mention, id, or name they used),
            "timeout_seconds": int (max 604800 = 7 days)
- "set_slowmode"
  - Set slowmode in the current channel.
  - Fields: "seconds": int (0–21600; 0 disables slowmode)

You MUST respond with ONLY valid JSON, no extra commentary.
JSON shape:
{
  "action": "none" | "clean_channel" | "timeout_user" | "set_slowmode",
  "messages": <int>,              # for clean_channel, else omit or null
  "target": "<string or null>",   # for timeout_user
  "timeout_seconds": <int>,       # for timeout_user
  "seconds": <int>,               # for set_slowmode
  "human_reply": "<short message to say in channel as the bot>"
}

If you're not sure what they want, use "action": "none" and write a normal chat-style "human_reply".
"""


class AI(commands.Cog):
    """
    TLG AI - local LLM-powered server regular + automod + light server manager.

    Big ideas:
    - Hangs out in chat like another member.
    - Learns how people in the server talk by watching messages.
    - Uses AI to help with moderation.
    - Admins can talk to it in natural language for simple management tasks.
    """

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xBEEFCAFE, force_registration=True
        )

        default_guild = {
            # Chat behaviour
            "enabled": True,                # auto-chat enabled
            "chat_channels": [],            # channels where it can auto-reply
            "auto_reply_chance": 0.08,      # 8% chance to reply to a normal message
            "cooldown_seconds": 40.0,       # min seconds between auto-replies per guild
            "last_reply_ts": 0.0,

            # Observation / "learning how people talk"
            "observe_history_on_start": False,  # if True, read some history at startup
            "history_messages_per_channel": 400,  # how deep to scan per text channel

            # Moderation settings
            "mod_enabled": True,
            "block_invites": True,
            "allowed_invite_codes": [],
            "spam_messages": 6,
            "spam_interval": 7,
            "invite_timeout_seconds": 600,
            "ai_moderation": True,          # use AI to classify messages
        }
        self.config.register_guild(**default_guild)

        # In-memory spam tracking: (guild_id, user_id) -> [timestamps...]
        self._spam_tracker: Dict[Tuple[int, int], List[float]] = defaultdict(list)

        # Guilds where a full-history scan is running (lockdown mode)
        self._history_lockdown_guilds: set[int] = set()

        # Per-guild, per-user short "how they talk" samples; not persisted
        self._style_samples: Dict[int, Dict[int, Deque[str]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=30))
        )

        # Whether we've kicked off a history observation task for a guild
        self._history_tasks: Dict[int, asyncio.Task] = {}

    # ---------------------------------------------------------------------- #
    # LLM core
    # ---------------------------------------------------------------------- #

    async def _llm_request(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> str:
        """
        Low-level call to the local LLM (Ollama by default).
        Returns raw text (no shortening / persona cleanup).
        """
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "temperature": temperature,
            "top_p": top_p,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(OLLAMA_URL, json=payload, timeout=60) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        log.warning("Ollama HTTP %s: %s", resp.status, text[:300])
                        return ""
                    data = await resp.json()
        except Exception as e:
            log.exception("Error talking to Ollama: %s", e)
            return ""

        return (data.get("response") or "").strip()

    # ---------------------------------------------------------------------- #
    # Style observation
    # ---------------------------------------------------------------------- #

    def _remember_style(self, message: discord.Message) -> None:
        """Store a short sample of how this user talks in this guild."""
        if not message.guild or not message.content:
            return
        if message.author.bot:
            return

        gid = message.guild.id
        uid = message.author.id
        text = message.content.strip()

        # Ignore super short or super long stuff
        if len(text) < 4:
            return
        if len(text) > 220:
            text = text[:220]

        self._style_samples[gid][uid].append(text)

    def _get_user_samples(
        self,
        guild_id: int,
        user_id: int,
        max_count: int = 5,
    ) -> List[str]:
        samples = list(self._style_samples[guild_id][user_id])
        random.shuffle(samples)
        return samples[:max_count]

    def _get_server_samples(
        self,
        guild_id: int,
        exclude_user: Optional[int] = None,
        max_count: int = 10,
    ) -> List[str]:
        all_samples: List[str] = []
        for uid, dq in self._style_samples[guild_id].items():
            if exclude_user is not None and uid == exclude_user:
                continue
            all_samples.extend(list(dq))
        random.shuffle(all_samples)
        return all_samples[:max_count]

    async def _observe_history_for_guild(
        self,
        guild: discord.Guild,
        *,
        scan_all: bool = False,
    ) -> None:
        """
        History scan to seed style samples.

        If scan_all=True: try to read the entire history of every text channel
        the bot can see. Otherwise, only read up to history_messages_per_channel.
        """
        conf = await self.config.guild(guild).all()
        per_channel = int(conf.get("history_messages_per_channel", 400))

        if per_channel <= 0 and not scan_all:
            return

        log.info(
            "TLG AI: starting history observation for guild %s (%s), scan_all=%s",
            guild.id,
            guild.name,
            scan_all,
        )

        for channel in guild.text_channels:
            try:
                me = guild.me
            except AttributeError:
                me = guild.get_member(self.bot.user.id) if self.bot.user else None

            perms = channel.permissions_for(me) if me else None
            if not perms or not perms.read_message_history:
                continue

            limit = None if scan_all else per_channel

            try:
                async for msg in channel.history(
                    limit=limit,
                    oldest_first=True,
                ):
                    self._remember_style(msg)
            except discord.HTTPException as e:
                log.warning(
                    "History read failed in #%s (%s): %s",
                    channel.name,
                    guild.name,
                    e,
                )
                await asyncio.sleep(2.0)
                continue

            await asyncio.sleep(1.5)

        log.info("TLG AI: finished history observation for guild %s", guild.id)

    async def cog_load(self) -> None:
        # Kick off optional observation tasks for guilds that want it (no lockdown)
        for guild in self.bot.guilds:
            conf = await self.config.guild(guild).all()
            if conf.get("observe_history_on_start", False):
                if guild.id not in self._history_tasks:
                    self._history_tasks[guild.id] = self.bot.loop.create_task(
                        self._observe_history_for_guild(guild, scan_all=False)
                    )

    async def cog_unload(self) -> None:
        for task in self._history_tasks.values():
            task.cancel()

    # ---------------------------------------------------------------------- #
    # High-level chat generation
    # ---------------------------------------------------------------------- #

    async def generate_chat_reply(
        self,
        message: discord.Message,
        user_text: str,
    ) -> str:
        """
        Reply to a normal chat message in-character.
        Handles greetings and obvious small-talk without hitting the model when possible.
        """
        if self._is_simple_greeting(user_text):
            return self._random_greeting_reply()

        if self._looks_like_smalltalk(user_text):
            return self._fallback_smalltalk(user_text)

        guild = message.guild
        assert guild is not None

        # Grab some style samples
        user_samples = self._get_user_samples(guild.id, message.author.id, max_count=4)
        server_samples = self._get_server_samples(guild.id, exclude_user=message.author.id, max_count=6)

        persona_parts = [BASE_PERSONA_PROMPT.strip()]

        if user_samples:
            persona_parts.append(
                "Examples of how this person has talked before in this server:\n"
                + "\n".join(f"- {s}" for s in user_samples)
            )

        if server_samples:
            persona_parts.append(
                "A few random messages from other people in this server (for vibe):\n"
                + "\n".join(f"- {s}" for s in server_samples)
            )

        persona_parts.append(
            "Now you're replying to their latest message in this channel.\n"
            "Keep it to 1–2 short sentences, like a normal Discord chat reply."
        )

        prompt = "\n\n".join(persona_parts)
        prompt += f"\n\nLast message in chat:\n\"{user_text.strip()}\"\n\nYour reply:"

        raw = await self._llm_request(prompt, temperature=0.65)
        if not raw:
            return self._fallback_smalltalk(user_text)

        cleaned = self._cleanup_reply(raw)
        short = self._shorten_reply(cleaned)
        if "tlg ai" in short.lower():
            return self._fallback_smalltalk(user_text)
        return short

    async def generate_mod_reply(self, instruction: str, target_mention: str) -> str:
        """
        Ask the LLM to phrase a moderation message (for spam / invites / AI moderation) in character.
        """
        prompt = (
            BASE_PERSONA_PROMPT
            + "\n\nContext: something happened in chat and you need to react to it.\n"
            f"Instruction: {instruction}\n"
            f"Address this message directly to {target_mention}. "
            "Keep it to ONE short, casual sentence, maybe one emoji. "
            "Don't lecture, just friendly but firm.\n\n"
            "Your reply:"
        )
        raw = await self._llm_request(prompt, temperature=0.6)
        if not raw:
            return f"{target_mention} chill a bit, yeah? 😅"
        cleaned = self._cleanup_reply(raw)
        return self._shorten_reply(cleaned, max_sentences=1, max_chars=140)

    # ---------------------------------------------------------------------- #
    # Admin intent -> actions
    # ---------------------------------------------------------------------- #

    async def _plan_admin_action(self, message: discord.Message, user_text: str) -> Dict[str, Any]:
        """
        Ask the LLM to turn an admin's natural language request into a structured action.
        """
        guild = message.guild
        channel = message.channel

        prompt = (
            ADMIN_INTENT_PROMPT
            + "\n\n"
            f"Server name: {guild.name}\n"
            f"Channel: #{channel.name}\n"
            f"Admin display name: {message.author.display_name}\n"
            f"Admin raw message (after the mention): \"{user_text.strip()}\"\n\n"
            "Now respond with ONLY the JSON object as described."
        )

        raw = await self._llm_request(prompt, temperature=0.2, top_p=0.95)
        if not raw:
            return {"action": "none", "human_reply": "not totally sure what you want me to do there tbh 😅"}

        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            log.warning("Admin intent LLM output not JSON: %r", raw[:300])
            return {"action": "none", "human_reply": "I didn't quite parse that as a task, but I can still chat about it."}

        try:
            obj = json.loads(text[start : end + 1])
        except Exception as e:
            log.warning("Failed to parse admin intent JSON: %s | %r", e, raw[:300])
            return {"action": "none", "human_reply": "My brain scuffed that request, maybe try phrasing it a bit clearer?"}

        action = obj.get("action") or "none"
        if action == "clean_channel":
            msgs = int(obj.get("messages") or 0)
            obj["messages"] = max(1, min(msgs, 200))
        elif action == "timeout_user":
            secs = int(obj.get("timeout_seconds") or 0)
            obj["timeout_seconds"] = max(0, min(secs, 604800))  # up to 7 days
        elif action == "set_slowmode":
            secs = int(obj.get("seconds") or 0)
            obj["seconds"] = max(0, min(secs, 21600))  # up to 6 hours

        if "human_reply" not in obj or not isinstance(obj["human_reply"], str):
            obj["human_reply"] = "gotchu 👍"

        return obj

    async def _execute_admin_action(self, message: discord.Message, plan: Dict[str, Any]) -> None:
        """
        Execute a planned admin action (if possible) and send the human_reply.
        """
        guild = message.guild
        channel = message.channel
        me: Optional[discord.Member] = guild.me if guild else None

        action = plan.get("action") or "none"
        human_reply: str = plan.get("human_reply") or "done 👍"

        async def send_reply():
            try:
                await message.reply(human_reply)
            except discord.HTTPException:
                pass

        if action == "none":
            if human_reply:
                await send_reply()
            else:
                reply = await self.generate_chat_reply(message, plan.get("raw_text") or "")
                try:
                    await message.reply(reply)
                except discord.HTTPException:
                    pass
            return

        if not guild or not isinstance(channel, discord.TextChannel):
            await send_reply()
            return

        perms = channel.permissions_for(me) if me else None

        if action == "clean_channel":
            if not perms or not perms.manage_messages:
                await message.reply("I don't have permission to clean messages in here.")
                return

            limit = int(plan.get("messages") or 50)
            limit = max(1, min(limit + 1, 200))  # include their command

            try:
                await channel.purge(limit=limit)
            except discord.HTTPException as e:
                log.warning("Purge failed: %s", e)
                await message.reply("Tried to clean up but Discord bonked me.")
                return

            await send_reply()
            return

        if action == "timeout_user":
            if not guild or not me or not me.guild_permissions.moderate_members:
                await message.reply("I can't timeout people here (missing permissions).")
                return

            raw_target = str(plan.get("target") or "").strip()
            if not raw_target:
                await message.reply("You didn't really tell me who to timeout.")
                return

            timeout_seconds = int(plan.get("timeout_seconds") or 0)
            if timeout_seconds <= 0:
                await message.reply("Timeout duration needs to be more than zero seconds.")
                return

            target_member: Optional[discord.Member] = None

            if message.mentions:
                target_member = message.mentions[0]
            else:
                try:
                    uid = int(raw_target)
                    target_member = guild.get_member(uid)
                except ValueError:
                    pass

                if target_member is None:
                    lower = raw_target.lower()
                    for m in guild.members:
                        if lower in m.display_name.lower() or lower in m.name.lower():
                            target_member = m
                            break

            if not target_member:
                await message.reply("I couldn't find who you're talking about to timeout.")
                return

            until = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
            try:
                await target_member.edit(
                    timed_out_until=until,
                    reason=f"Requested by {message.author} via TLG AI.",
                )
            except discord.HTTPException as e:
                log.warning("Timeout failed: %s", e)
                await message.reply("Tried to timeout but Discord didn't let me.")
                return

            await send_reply()
            return

        if action == "set_slowmode":
            if not perms or not perms.manage_channels:
                await message.reply("I can't change slowmode here (need Manage Channel perms).")
                return

            seconds = int(plan.get("seconds") or 0)
            try:
                await channel.edit(slowmode_delay=seconds, reason=f"Requested by {message.author} via TLG AI.")
            except discord.HTTPException as e:
                log.warning("Slowmode edit failed: %s", e)
                await message.reply("Discord didn't let me change slowmode.")
                return

            await send_reply()
            return

        await send_reply()

    # ---------------------------------------------------------------------- #
    # Moderation helpers
    # ---------------------------------------------------------------------- #

    async def _ai_moderation_decision(self, message: discord.Message, guild_conf: dict) -> bool:
        """
        Optional AI moderation after cheap checks (invites / spam).
        Returns True if the message was moderated (deleted / warning / timeout).
        """
        if not guild_conf.get("ai_moderation", True):
            return False
        if not guild_conf.get("mod_enabled", True):
            return False
        if not message.content:
            return False

        if isinstance(message.author, discord.Member):
            perms = message.author.guild_permissions
            if perms.manage_messages or perms.administrator:
                return False

        context_snippets = self._get_server_samples(message.guild.id, exclude_user=message.author.id, max_count=5)
        prompt = MODERATION_CLASSIFIER_PROMPT + "\n\n"

        if context_snippets:
            prompt += "Some random example messages from this server for context (not necessarily related):\n"
            prompt += "\n".join(f"- {s}" for s in context_snippets)
            prompt += "\n\n"

        prompt += (
            f"Now evaluate this exact message from a user:\n"
            f"\"{message.content.strip()}\"\n\n"
            "Respond ONLY with the JSON object."
        )

        raw = await self._llm_request(prompt, temperature=0.1, top_p=0.9)
        if not raw:
            return False

        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            log.debug("Moderation LLM output not JSON: %r", raw[:300])
            return False

        try:
            data = json.loads(text[start : end + 1])
        except Exception as e:
            log.debug("Failed to parse moderation JSON: %s | %r", e, raw[:300])
            return False

        action = (data.get("action") or "allow").lower()
        reason = data.get("reason") or "unspecified"
        timeout_seconds = int(data.get("timeout_seconds") or 0)

        if action == "allow":
            return False

        if action == "warn":
            try:
                warn = await self.generate_mod_reply(
                    f"They said something borderline ({reason}). Give them a gentle warning.",
                    message.author.mention,
                )
                await message.channel.send(warn)
            except discord.HTTPException:
                pass
            return False

        if action in {"delete", "timeout"}:
            try:
                await message.delete()
            except discord.HTTPException:
                return False

        if action == "delete":
            try:
                warn = await self.generate_mod_reply(
                    f"Their message broke the rules ({reason}). Let them know it was removed.",
                    message.author.mention,
                )
                await message.channel.send(warn)
            except discord.HTTPException:
                pass
            return True

        if action == "timeout":
            if not isinstance(message.author, discord.Member) or not message.guild:
                return True

            me = message.guild.me
            if not me or not me.guild_permissions.moderate_members:
                return True

            timeout_seconds = max(60, min(timeout_seconds, 7 * 24 * 3600))
            until = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)

            try:
                await message.author.edit(
                    timed_out_until=until,
                    reason=f"AI moderation: {reason}",
                )
            except discord.HTTPException:
                return True

            try:
                warn = await self.generate_mod_reply(
                    f"They crossed a serious line ({reason}). Let them know they've been timed out.",
                    message.author.mention,
                )
                await message.channel.send(warn)
            except discord.HTTPException:
                pass

            return True

        return False

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

        content = message.content or ""
        matches = INVITE_REGEX.findall(content)
        if not matches:
            return False

        allowed_codes = {code.lower() for code in guild_conf.get("allowed_invite_codes", [])}
        timeout_seconds = int(guild_conf.get("invite_timeout_seconds", 0))

        for code in matches:
            if code.lower() not in allowed_codes:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

                try:
                    warn = await self.generate_mod_reply(
                        "They posted a Discord invite link that isn't allowed. "
                        "Tell them invites aren't allowed here.",
                        message.author.mention,
                    )
                    await message.channel.send(warn)
                except discord.HTTPException:
                    pass

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
                warn = await self.generate_mod_reply(
                    "They're sending messages too fast. "
                    "Tell them to slow down a bit without being harsh.",
                    message.author.mention,
                )
                await message.channel.send(warn)
            except discord.HTTPException:
                pass
            return True

        return False

    # ---------------------------------------------------------------------- #
    # Smalltalk / cleanup utilities
    # ---------------------------------------------------------------------- #

    def _is_simple_greeting(self, text: str) -> bool:
        """Detect very simple greetings we can reply to without the LLM."""
        t = text.strip().lower()
        t = re.sub(r"[!?.,]+$", "", t)

        for name in ["tlg ai", "tlgai", "tlg", "@tlg", "@tlg ai"]:
            t = t.replace(name, "")
        t = t.strip()

        if len(t) == 0:
            return True

        greetings = {"hi", "hey", "hello", "yo", "hiya", "heya", "sup"}
        return t in greetings

    def _looks_like_smalltalk(self, text: str) -> bool:
        """Quick check for 'hey how are you' style stuff."""
        t = text.lower()
        return (
            "how are you" in t
            or "how r u" in t
            or "hru" in t
            or "how's it going" in t
            or "hows it going" in t
        )

    def _fallback_smalltalk(self, text: str) -> str:
        """Fallback for 'hey, how are you' etc when we don't want the LLM to be cringe."""
        t = text.lower()
        if self._looks_like_smalltalk(t):
            options = [
                "pretty good, just vibing. you?",
                "tired but alive lmao, hbu?",
                "not bad at all, how about you?",
                "chillin as always, you good?",
            ]
            return random.choice(options)

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

    def _cleanup_reply(self, text: str) -> str:
        """
        Strip formal / weird stuff the model sometimes adds.
        """
        original = text
        t = text.strip()
        lower = t.lower()

        quote_pairs = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")]
        for ql, qr in quote_pairs:
            if t.startswith(ql) and t.endswith(qr) and len(t) > 2:
                t = t[1:-1].strip()
        t = t.lstrip("\"'“”‘’").rstrip()
        lower = t.lower()

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

    def _shorten_reply(
        self,
        text: str,
        *,
        max_sentences: int = 2,
        max_chars: int = 220,
    ) -> str:
        """Keep replies short and Discord-ish."""
        parts = re.split(r'(?<=[.!?])\s+', text)
        if len(parts) > max_sentences:
            text = " ".join(parts[:max_sentences])

        if len(text) > max_chars:
            cut = text.rfind(" ", 0, max_chars)
            if cut == -1:
                cut = max_chars
            text = text[:cut].rstrip()

        return text

    # ---------------------------------------------------------------------- #
    # Admin config commands
    # ---------------------------------------------------------------------- #

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
                "`aichat invitetimeout`, `aichat observeonstart`, "
                "`aichat historyscan`, `aichat aimod`"
            )

    # ----- AI channel config -----

    @aichat_group.command(name="channels")
    async def aichat_channels(self, ctx: commands.Context):
        """List all channels where TLG auto-chat is active."""
        ids = await self.config.guild(ctx.guild).chat_channels()
        if not ids:
            await ctx.send("I'm not auto-chatting in any channels.")
            return

        channels = []
        for cid in ids:
            chan = ctx.guild.get_channel(cid)
            if chan:
                channels.append(chan.mention)

        if not channels:
            await ctx.send("The configured channels no longer exist.")
        else:
            await ctx.send("I'll sometimes join conversations in: " + ", ".join(channels))

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
        await ctx.send(f"I'll now sometimes join conversations in {channel.mention}.")

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
        await ctx.send(f"I won't randomly chat in {channel.mention} anymore.")

    @aichat_group.command(name="toggle")
    async def aichat_toggle(self, ctx: commands.Context):
        """Toggle TLG auto-chat on/off for this server."""
        enabled = await self.config.guild(ctx.guild).enabled()
        enabled = not enabled
        await self.config.guild(ctx.guild).enabled.set(enabled)
        await ctx.send(f"Auto-chat is now **{'enabled' if enabled else 'disabled'}**.")

    @aichat_group.command(name="chance")
    async def aichat_chance(self, ctx: commands.Context, chance: float):
        """
        Set chance (0–1) that TLG responds to a message in active channels.
        """
        chance = max(0.0, min(1.0, chance))
        await self.config.guild(ctx.guild).auto_reply_chance.set(chance)
        await ctx.send(f"Auto-reply chance set to **{chance:.2f}**.")

    # ----- Observation / style learning -----

    @aichat_group.command(name="observeonstart")
    async def aichat_observeonstart(self, ctx: commands.Context, toggle: bool):
        """
        Toggle whether TLG should scan some message history on startup
        to learn how people talk.
        """
        await self.config.guild(ctx.guild).observe_history_on_start.set(toggle)
        await ctx.send(
            f"History observation on startup is now **{'enabled' if toggle else 'disabled'}**.\n"
            "Note: this only reads messages the bot can see and doesn't store full logs, "
            "just tiny style snippets."
        )

    @aichat_group.command(name="historyscan")
    async def aichat_historyscan(self, ctx: commands.Context):
        """
        Full history scan: read as many messages as Discord lets me see
        in every text channel, and temporarily lock down this cog.

        While the scan is running:
        - No auto-chat
        - No AI moderation
        - No invite/spam moderation
        - No admin-intent actions

        Other cogs/commands on the bot still work normally.
        """
        guild = ctx.guild
        gid = guild.id

        if gid in self._history_lockdown_guilds:
            await ctx.send("I'm already in full history-scan mode for this server.")
            return

        self._history_lockdown_guilds.add(gid)
        await ctx.send(
            "Alright, going into nerd mode and reading through the whole server history I can see. "
            "I'll stay quiet and not run automod/chat stuff until I'm done."
        )

        async def run_scan():
            try:
                await self._observe_history_for_guild(guild, scan_all=True)
            finally:
                self._history_lockdown_guilds.discard(gid)
                try:
                    await ctx.send("Done reading history, I'm back to normal behavior now.")
                except discord.HTTPException:
                    pass

        task = self.bot.loop.create_task(run_scan())
        self._history_tasks[gid] = task

    # ----- Moderation config -----

    @aichat_group.command(name="modtoggle")
    async def aichat_modtoggle(self, ctx: commands.Context):
        """Toggle TLG's automod features on/off."""
        conf = await self.config.guild(ctx.guild).all()
        current = conf.get("mod_enabled", True)
        new_val = not current
        await self.config.guild(ctx.guild).mod_enabled.set(new_val)
        await ctx.send(f"Automod is now **{'enabled' if new_val else 'disabled'}**.")

    @aichat_group.command(name="aimod")
    async def aichat_aimod(self, ctx: commands.Context, toggle: bool):
        """Enable or disable AI-based message moderation."""
        await self.config.guild(ctx.guild).ai_moderation.set(toggle)
        await ctx.send(
            f"AI moderation is now **{'enabled' if toggle else 'disabled'}**.\n"
            "Reminder: it's smart but not perfect, so keep an eye on it."
        )

    @aichat_group.command(name="blockinvites")
    async def aichat_blockinvites(self, ctx: commands.Context, toggle: bool):
        """Enable or disable blocking of Discord invite links."""
        await self.config.guild(ctx.guild).block_invites.set(toggle)
        await ctx.send(
            f"Blocking of Discord invites is now **{'enabled' if toggle else 'disabled'}**."
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

    # ---------------------------------------------------------------------- #
    # Admin-only direct AI command
    # ---------------------------------------------------------------------- #

    @commands.command(name="ai")
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def ai_command(self, ctx: commands.Context, *, message: str):
        """
        Talk directly to TLG AI (admin only).
        This just gives you a raw chat reply using the same style as mention replies.
        """
        async with ctx.typing():
            reply = await self.generate_chat_reply(ctx.message, message)

        await ctx.reply(reply)

    # ---------------------------------------------------------------------- #
    # Passive chat listener (mention + auto-chat + automod + admin requests)
    # ---------------------------------------------------------------------- #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        - Runs basic automod (invites + spam) and optional AI moderation.
        - Tracks style samples for everyone (when not locked down).
        - If someone mentions the bot in a message (anywhere) and it's not a command,
          TLG AI replies directly.
        - If the author is an admin, mention-requests can trigger simple management actions.
        - Otherwise, it may occasionally auto-reply in configured channels.
        """
        if message.author.bot:
            return
        if not message.guild:
            return

        # Full-history scan lockdown: cog ignores everything while scanning
        if message.guild.id in self._history_lockdown_guilds:
            return

        # Keep style memory updated
        self._remember_style(message)

        guild = message.guild
        guild_conf = await self.config.guild(guild).all()

        # --- Automod first (cheap checks) ---
        if await self.handle_invites(message, guild_conf):
            return
        if await self.handle_spam(message, guild_conf):
            return

        # Then AI moderation
        if await self._ai_moderation_decision(message, guild_conf):
            return

        content = message.content or ""
        content_stripped = content.lstrip()

        try:
            prefixes = await self.bot.get_valid_prefixes(guild)
        except TypeError:
            prefixes = await self.bot.get_valid_prefixes()
        is_command_like = any(content.startswith(p) for p in prefixes)

        bot_user = self.bot.user

        # --- Direct mention detection ---
        if bot_user and (bot_user.id in message.raw_mentions) and not is_command_like:
            mention_pattern = rf"<@!?{bot_user.id}>"
            user_text = re.sub(mention_pattern, "", content, count=1).strip()
            if not user_text:
                user_text = "hi"

            is_admin = isinstance(message.author, discord.Member) and (
                message.author.guild_permissions.administrator
                or message.author.guild_permissions.manage_guild
            )

            # If it's clearly just smalltalk, treat it like normal chat,
            # even if they're an admin.
            if is_admin and not self._is_simple_greeting(user_text) and not self._looks_like_smalltalk(user_text):
                async with message.channel.typing():
                    plan = await self._plan_admin_action(message, user_text)
                    plan["raw_text"] = user_text
                    await self._execute_admin_action(message, plan)
                return

            try:
                await message.channel.trigger_typing()
            except discord.HTTPException:
                pass

            reply = await self.generate_chat_reply(message, user_text)
            try:
                await message.reply(reply)
            except discord.HTTPException:
                pass

            return

        # --- Auto-chat behavior ---
        if not guild_conf.get("enabled", True):
            return

        chan_ids = guild_conf.get("chat_channels", [])
        if not chan_ids or message.channel.id not in chan_ids:
            return

        if is_command_like:
            return

        now = time.time()
        last = guild_conf.get("last_reply_ts", 0.0)
        cooldown = float(guild_conf.get("cooldown_seconds", 40))
        if now - last < cooldown:
            return

        chance = float(guild_conf.get("auto_reply_chance", 0.08))
        if chance <= 0 or random.random() > chance:
            return

        await self.config.guild(guild).last_reply_ts.set(now)

        user_text = content_stripped[:500]

        try:
            await message.channel.trigger_typing()
        except discord.HTTPException:
            pass

        reply = await self.generate_chat_reply(message, user_text)
        try:
            await message.reply(reply)
        except discord.HTTPException:
            pass
