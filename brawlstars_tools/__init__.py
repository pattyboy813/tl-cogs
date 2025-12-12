"""Top‑level package for Brawl Stars tools.

This package exposes the main cog class that registers user and admin
commands for interacting with Supercell's Brawl Stars API. It
pulls smaller pieces from the submodules within this package so you
can keep the code base organised and easy to maintain. Breaking the
original single file into multiple modules helps you reason about the
behaviour of each component and makes it clear where to look when you
need to make changes.

Usage:
    from brawlstars_tools import BrawlStarsTools
    bot.add_cog(BrawlStarsTools(bot))
"""

from .cog import BrawlStarsTools  # noqa: F401