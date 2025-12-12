# bstools/api.py
from typing import Optional, Dict

import aiohttp
from redbot.core.bot import Red

from .constants import BASE_URL
from .tags import format_tag


class BrawlStarsAPI:
    """Handles all requests to the Brawl Stars API using the shared Red API token."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.token: Optional[str] = None

    async def start(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

        tokens = await self.bot.get_shared_api_tokens("brawlstars")
        self.token = tokens.get("token")

        if not self.token:
            raise RuntimeError(
                "No Brawl Stars API token set.\n"
                "Use: `[p]set api brawlstars token,YOUR_TOKEN_HERE`"
            )

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def request(self, endpoint: str) -> Dict:
        if self.session is None:
            await self.start()

        url = BASE_URL + endpoint
        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 404:
                    return None

                text = await resp.text()
                raise RuntimeError(f"API error {resp.status}: {text}")
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Network error contacting Brawl Stars API: {e}")

    async def get_player(self, tag: str) -> Optional[Dict]:
        tag = format_tag(tag)
        return await self.request(f"/players/%23{tag}")

    async def get_club(self, tag: str) -> Optional[Dict]:
        clean_tag = format_tag(tag)
        return await self.request(f"/clubs/%23{clean_tag}")
