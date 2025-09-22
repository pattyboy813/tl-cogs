import asyncpg
import asyncio
from typing import Optional, Mapping, Any

_POOL: Optional[asyncpg.Pool] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS modlogv2_events (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    event TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS modlogv2_settings (
    guild_id BIGINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    log_channel_id BIGINT,
    use_embeds BOOLEAN NOT NULL DEFAULT TRUE,
    events JSONB NOT NULL
);
"""

DEFAULT_EVENTS = {
    "message_delete": True,
    "message_edit": True,
    "message_bulk_delete": True,
    "member_join": True,
    "member_remove": True,
    "member_update": True,
    "role_changes": True,
    "channel_changes": True,
    "emoji_changes": True,
    "sticker_changes": True,
    "thread_changes": True,
    "timeouts": True,
    "integrations": True,
    "webhooks": True,
    "automod_rules": True,
    "automod_action_execution": True,
}

async def init_pool(dsn: str):
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
        async with _POOL.acquire() as con:
            await con.execute(SCHEMA)

def pool() -> asyncpg.Pool:
    if _POOL is None:
        raise RuntimeError("DB pool not initialized. Run [p]modlog db setdsn ...")
    return _POOL

async def write_event(guild_id: int, event: str, payload: Mapping[str, Any]):
    async with pool().acquire() as con:
        await con.execute(
            "INSERT INTO modlogv2_events (guild_id, event, payload) VALUES ($1, $2, $3::jsonb)",
            guild_id, event, payload
        )

async def load_settings(guild_id: int):
    async with pool().acquire() as con:
        row = await con.fetchrow(
            "SELECT enabled, log_channel_id, use_embeds, events FROM modlogv2_settings WHERE guild_id=$1",
            guild_id
        )
    if not row:
        return dict(enabled=True, log_channel_id=None, use_embeds=True, events=DEFAULT_EVENTS.copy())
    data = dict(row)
    events = DEFAULT_EVENTS.copy()
    if data["events"]:
        events.update({k: bool(v) for k, v in data["events"].items()})
    data["events"] = events
    return data

async def save_settings(guild_id: int, data: Mapping[str, Any]):
    async with pool().acquire() as con:
        await con.execute(
            """
            INSERT INTO modlogv2_settings (guild_id, enabled, log_channel_id, use_embeds, events)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (guild_id) DO UPDATE SET
              enabled=EXCLUDED.enabled,
              log_channel_id=EXCLUDED.log_channel_id,
              use_embeds=EXCLUDED.use_embeds,
              events=EXCLUDED.events
            """,
            guild_id, bool(data["enabled"]), data.get("log_channel_id"),
            bool(data["use_embeds"]), data.get("events") or DEFAULT_EVENTS
        )
