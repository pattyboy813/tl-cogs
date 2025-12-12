"""Utility functions and classes for Brawl Stars tags and storage.

This module provides helper functions to validate and normalise player or
club tags, and defines a ``TagStore`` class which wraps Red's ``Config``
object to manage per‑user saved tags.  It also defines a few custom
exceptions to represent common error conditions encountered when dealing
with tags.
"""

from typing import List, Dict

from redbot.core import Config

from .constants import _VALID_TAG_CHARS


class InvalidTag(Exception):
    """Raised when a provided tag doesn't match the expected format."""


class TagAlreadySaved(Exception):
    """Raised when a user attempts to save a tag that is already in their list."""


class TagAlreadyExists(Exception):
    """Raised when a tag is saved under a different user."""

    def __init__(self, user_id: int, message: str):
        self.user_id = user_id
        super().__init__(message)


class MainAlreadySaved(Exception):
    """Raised when trying to move accounts to a user who already has a main."""


class InvalidArgument(Exception):
    """Raised when an argument is out of bounds or otherwise invalid."""


def format_tag(tag: str) -> str:
    """Strip ``#`` and replace ``O`` with ``0`` while uppercasing the tag."""
    return tag.strip("#").upper().replace("O", "0")


def verify_tag(tag: str) -> bool:
    """Return ``True`` if the tag contains only valid characters and is <= 15 chars."""
    if len(tag) > 15:
        return False
    return all(ch in _VALID_TAG_CHARS for ch in tag)


class TagStore:
    """Per‑user Brawl Stars tag storage backed by Red's ``Config``.

    This class is responsible for persisting a list of saved tags per user and
    ensuring that each tag is unique across all users.  It normalises tags
    before storage and provides helper methods to add, remove or reorder
    accounts.
    """

    def __init__(self, config: Config):
        self.config = config

    async def _get_accounts(self, user_id: int) -> List[str]:
        return await self.config.user_from_id(user_id).brawlstars_accounts()

    async def _set_accounts(self, user_id: int, accounts: List[str]):
        await self.config.user_from_id(user_id).brawlstars_accounts.set(accounts)

    async def account_count(self, user_id: int) -> int:
        accounts = await self._get_accounts(user_id)
        return len(accounts)

    async def get_all_tags(self, user_id: int) -> List[str]:
        return await self._get_accounts(user_id)

    async def save_tag(self, user_id: int, tag: str) -> int:
        """Normalise and store a tag for the given user.

        Returns the 1‑based index of the newly saved tag.  Raises
        ``InvalidTag``, ``TagAlreadySaved`` or ``TagAlreadyExists`` if there
        are problems with the tag or if it already belongs to someone else.
        """
        tag = format_tag(tag)
        if not verify_tag(tag):
            raise InvalidTag

        accounts = await self._get_accounts(user_id)
        if tag in accounts:
            raise TagAlreadySaved

        # Check if another user has this tag
        all_users: Dict[str, dict] = await self.config.all_users()
        for uid_str, data in all_users.items():
            uid = int(uid_str)
            other_accounts = data.get("brawlstars_accounts", [])
            if tag in [format_tag(t) for t in other_accounts]:
                if uid != user_id:
                    raise TagAlreadyExists(uid, f"Tag is saved under another user: {uid}")

        accounts.append(tag)
        await self._set_accounts(user_id, accounts)
        return len(accounts)

    async def unlink_tag(self, user_id: int, account: int):
        accounts = await self._get_accounts(user_id)
        if account < 1 or account > len(accounts):
            raise InvalidArgument
        del accounts[account - 1]
        await self._set_accounts(user_id, accounts)

    async def switch_place(self, user_id: int, account1: int, account2: int):
        accounts = await self._get_accounts(user_id)
        n = len(accounts)
        if account1 < 1 or account1 > n or account2 < 1 or account2 > n:
            raise InvalidArgument
        accounts[account1 - 1], accounts[account2 - 1] = (
            accounts[account2 - 1],
            accounts[account1 - 1],
        )
        await self._set_accounts(user_id, accounts)

    async def move_user_id(self, old_user_id: int, new_user_id: int):
        old_accounts = await self._get_accounts(old_user_id)
        new_accounts = await self._get_accounts(new_user_id)
        if new_accounts:
            raise MainAlreadySaved
        await self._set_accounts(new_user_id, old_accounts)
        await self._set_accounts(old_user_id, [])