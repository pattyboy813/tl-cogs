from __future__ import annotations
import enum
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

Priv = Literal["MOD", "ADMIN", "BOT_OWNER", "GUILD_OWNER", "NONE"]

class Event(enum.StrEnum):
    MESSAGE_EDIT = "message_edit"
    MESSAGE_DELETE = "message_delete"
    USER_CHANGE = "user_change"
    ROLE_CHANGE = "role_change"
    ROLE_CREATE = "role_create"
    ROLE_DELETE = "role_delete"
    VOICE_CHANGE = "voice_change"
    USER_JOIN = "user_join"
    USER_LEFT = "user_left"
    CHANNEL_CHANGE = "channel_change"
    CHANNEL_CREATE = "channel_create"
    CHANNEL_DELETE = "channel_delete"
    GUILD_CHANGE = "guild_change"
    EMOJI_CHANGE = "emoji_change"
    COMMANDS_USED = "commands_used"
    INVITE_CREATED = "invite_created"
    INVITE_DELETED = "invite_deleted"
    # AutoMod v2
    AUTOMOD_RULE_CREATE = "automod_rule_create"
    AUTOMOD_RULE_UPDATE = "automod_rule_update"
    AUTOMOD_RULE_DELETE = "automod_rule_delete"
    AUTOMOD_ACTION = "automod_action"

    @classmethod
    def from_str(cls, value: str) -> "Event":
        try:
            return cls(value.lower())
        except Exception:
            raise ValueError(f"Unknown event '{value}'")

@dataclass
class EventSettings:
    enabled: bool = False
    channel: Optional[int] = None
    colour: Optional[int] = None
    emoji: str = ""
    embed: bool = True
    # per-event flags (used by various listeners)
    bots: Optional[bool] = None
    bulk_enabled: Optional[bool] = None
    bulk_individual: Optional[bool] = None
    cached_only: Optional[bool] = None
    privs: Optional[List[Priv]] = None
    nicknames: Optional[bool] = None

@dataclass
class GuildSettings:
    events: Dict[Event, EventSettings] = field(default_factory=dict)
    ignored_channels: List[int] = field(default_factory=list)
    invite_links: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "events": {e.value: vars(s) for e, s in self.events.items()},
            "ignored_channels": self.ignored_channels,
            "invite_links": self.invite_links,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GuildSettings":
        events = {
            Event.from_str(k): EventSettings(**v) for k, v in data.get("events", {}).items()
        }
        return cls(
            events=events,
            ignored_channels=list(data.get("ignored_channels", [])),
            invite_links=dict(data.get("invite_links", {})),
        )
