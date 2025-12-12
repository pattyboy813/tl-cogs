"""HTTP client for the official Brawl Stars API.

This module wraps asynchronous requests to the Supercell API, using a
shared API token configured through Redbot.  All API interactions are
centralised here so that error handling and session management are easy
to reason about.  See https://developer.brawlstars.com/ for API
documentation.
"""

from typing import Optional, Dict

import aiohttp
from redbot.core.bot import Red

from .constants import BASE_URL


class BrawlStarsAPI:
    """Handles all requests to the Brawl Stars API using a shared token."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.token: Optional[str] = None

    async def start(self):
        """Initialise the HTTP session and retrieve the API token from Red."""
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
        """Close the HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None

    async def request(self, endpoint: str) -> Optional[Dict]:
        """Send a GET request to the specified endpoint and return the JSON data.

        If a 404 status is returned the method returns ``None`` instead of
        raising an error so callers can handle missing data gracefully.
        """
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
        """Fetch a player object by tag (with or without the '#')."""
        from .utils import format_tag  # local import to avoid circular imports
        clean = format_tag(tag)
        return await self.request(f"/players/%23{clean}")

    async def get_club(self, tag: str) -> Optional[Dict]:
        """Fetch a club object by tag (with or without the '#')."""
        from .utils import format_tag
        clean = format_tag(tag)
        return await self.request(f"/clubs/%23{clean}")