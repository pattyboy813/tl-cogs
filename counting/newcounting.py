from __future__ import annotations
import asyncio
from typing import Optional, Dict, List
import re
import ast
import operator
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import commands, Config

CONF_ID = 347209384723923874 # random string

class Counting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier = CONF_ID, force_registration = True)

        guild_default = {
            "channel_id": None,
            "last_number": 0,
            "last_user_id": None,
            "high_score": 0,
            "allow_bots": False,
        }
        guild_