from __future__ import annotations
from copy import deepcopy
from typing import Dict
from .models import Event, EventSettings, GuildSettings

DEFAULTS: Dict[Event, EventSettings] = {
    Event.MESSAGE_EDIT:   EventSettings(emoji="📝", embed=True, bots=False),
    Event.MESSAGE_DELETE: EventSettings(emoji="🗑️", embed=True, bots=False, bulk_enabled=False, bulk_individual=False, cached_only=True),
    Event.USER_CHANGE:    EventSettings(emoji="👨‍🔧", embed=True, bots=True, nicknames=True),
    Event.ROLE_CHANGE:    EventSettings(emoji="🏳️", embed=True),
    Event.ROLE_CREATE:    EventSettings(emoji="🏳️", embed=True),
    Event.ROLE_DELETE:    EventSettings(emoji="🏳️", embed=True),
    Event.VOICE_CHANGE:   EventSettings(emoji="🎤", embed=True),
    Event.USER_JOIN:      EventSettings(emoji="📥", embed=True),
    Event.USER_LEFT:      EventSettings(emoji="📤", embed=True),
    Event.CHANNEL_CHANGE: EventSettings(emoji="🧩", embed=True),
    Event.CHANNEL_CREATE: EventSettings(emoji="🧩", embed=True),
    Event.CHANNEL_DELETE: EventSettings(emoji="🧩", embed=True),
    Event.GUILD_CHANGE:   EventSettings(emoji="🛠️", embed=True),
    Event.EMOJI_CHANGE:   EventSettings(emoji="😶‍🌫️", embed=True),
    Event.COMMANDS_USED:  EventSettings(emoji="🤖", embed=True, privs=["MOD","ADMIN","BOT_OWNER","GUILD_OWNER"]),
    Event.INVITE_CREATED: EventSettings(emoji="🔗", embed=True),
    Event.INVITE_DELETED: EventSettings(emoji="⛓️", embed=True),
    # AutoMod v2
    Event.AUTOMOD_RULE_CREATE: EventSettings(emoji="🧱", embed=True),
    Event.AUTOMOD_RULE_UPDATE: EventSettings(emoji="🧱", embed=True),
    Event.AUTOMOD_RULE_DELETE: EventSettings(emoji="🧱", embed=True),
    Event.AUTOMOD_ACTION:      EventSettings(emoji="🛡️", embed=True),
}

def initial_guild_settings() -> GuildSettings:
    return GuildSettings(events=deepcopy(DEFAULTS))
