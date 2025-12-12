"""Constant definitions for the Brawl Stars cog.

All static values such as API endpoints, emoji mappings or default
configuration live in this module. Centralising these definitions
improves readability and makes it trivial to reuse the same values in
multiple parts of the code without duplication.
"""

# Base URL for the official Brawl Stars API
BASE_URL: str = "https://api.brawlstars.com/v1"

# CDN for Icons and Badges (Brawlify mirrors official assets)
CDN_ICON_URL: str = "https://cdn.brawlify.com/profile-icons/regular/{}.png"
CDN_BADGE_URL: str = "https://cdn.brawlify.com/club-badges/regular/{}.png"

# Shared config identifier.  A hex makes it easy to avoid collisions
BSTOOLS_CONFIG_ID: int = 0xB5B5B5B5

# Default configuration for servers and users.  These dicts are used
# when registering configuration with Red's ``Config`` helper.
default_guild = {
    # clubs: mapping of club_tag -> {"tag": "#TAG", "name": "Club Name"}
    "clubs": {},
    # auto‑updating overview
    "overview_channel": None,   # Optional[int]
    "overview_message": None,   # Optional[int]
    # leadership role & applications channel
    "leadership_role": None,        # Optional[int]
    "applications_channel": None,   # Optional[int]
}

default_user = {
    # list of saved brawl stars tags (strings without '#')
    "brawlstars_accounts": [],
}

# Hardcoded custom emoji mapping.  If a brawler name isn't found the
# generic shield emoji will be used instead.
BRAWLER_EMOJIS = {
    "gigi": "<:gigi:1446711155874594816>",
    "ziggy": "<:ziggy:1446711159603593328>",
    "mina": "<:mina:1446711163252641898>",
    "trunk": "<:trunk:1446711166955946029>",
    "alli": "<:alli:1446711170059862270>",
    "kaze": "<:kaze:1446711172878307369>",
    "jaeyong": "<:jaeyong:1446711176204652585>",
    "finx": "<:finx:1446711179509629090>",
    "lumi": "<:lumi:1446711183183970508>",
    "ollie": "<:ollie:1446711186123915294>",
    "meeple": "<:meeple:1446711188993081344>",
    "buzzlightyear": "<:buzzlightyear:1446711192243667045>",
    "juju": "<:juju:1446711195406041182>",
    "shade": "<:shade:1446711198551638178>",
    "kenji": "<:kenji:1446711201789644822>",
    "moe": "<:moe:1446711205149540362>",
    "clancy": "<:clancy:1446711208647331920>",
    "berry": "<:berry:1446711212258754560>",
    "lily": "<:lily:1446711214850969722>",
    "draco": "<:draco:1446711218172858369>",
    "angelo": "<:angelo:1446711221402472579>",
    "melodie": "<:melodie:1446711224522899487>",
    "larrylawrie": "<:larrylawrie:1446711228188852305>",
    "kit": "<:kit:1446711231112024187>",
    "mico": "<:mico:1446711234106888299>",
    "charlie": "<:charlie:1446711237185634334>",
    "chuck": "<:chuck:1446711241161834718>",
    "pearl": "<:pearl:1446711245008011276>",
    "doug": "<:doug:1446711248212459622>",
    "cordelius": "<:cordelius:1446711250829705369>",
    "hank": "<:hank:1446711254269034707>",
    "maisie": "<:maisie:1446711257347526708>",
    "willow": "<:willow:1446720498854531145>",
    "rt": "<:rt:1446720502117695578>",
    "mandy": "<:mandy:1446720506064801972>",
    "gray": "<:gray:1446720509231239239>",
    "chester": "<:chester:1446720512100274268>",
    "buster": "<:buster:1446720515011252307>",
    "gus": "<:gus:1446720518437736652>",
    "sam": "<:sam:1446720521822801944>",
    "otis": "<:otis:1446720525220184145>",
    "bonnie": "<:bonnie:1446720530098028625>",
    "janet": "<:janet:1446720533096824956>",
    "eve": "<:eve:1446720536120922183>",
    "fang": "<:fang:1446720539384352848>",
    "lola": "<:lola:1446720542014046269>",
    "meg": "<:meg:1446720545482604716>",
    "ash": "<:ash:1446720548590850049>",
    "griff": "<:griff:1446720551937642608>",
    "buzz": "<:buzz:1446720555314315365>",
    "grom": "<:grom:1446720558267109376>",
    "squeak": "<:squeak:1446720561391603845>",
    "belle": "<:belle:1446720564600246463>",
    "stu": "<:stu:1446720568270389288>",
    "ruffs": "<:ruffs:1446720571566981261>",
    "edgar": "<:edgar:1446720574855450795>",
    "byron": "<:byron:1446720577736806480>",
    "lou": "<:lou:1446720581323067403>",
    "amber": "<:amber:1446720585081163956>",
    "colette": "<:colette:1446720588436607027>",
    "surge": "<:surge:1446720591921938544>",
    "sprout": "<:sprout:1446720595088769217>",
    "nani": "<:nani:1446720598242889759>",
    "gale": "<:gale:1446720601283629138>",
    "jacky": "<:jacky:1446720604387540993>",
    "max": "<:max:1446720607109779467>",
    "mrp": "<:mrp:1446720610888716288>",
    "emz": "<:emz:1446720614055542876>",
    "bea": "<:bea:1446720617062862998>",
    "sandy": "<:sandy:1446720620212650105>",
    "8bit": "<:8bit:1446720623530217594>",
    "bibi": "<:bibi:1446720626743185549>",
    "carl": "<:carl:1446720629889044560>",
    "rosa": "<:rosa:1446720633059807362>",
    "leon": "<:leon:1446720636306063451>",
    "tick": "<:tick:1446720646674645203>",
    "gene": "<:gene:1446720649925234789>",
    "frank": "<:frank:1446720652945129492>",
    "penny": "<:penny:1446720656136999015>",
    "darryl": "<:darryl:1446720659127537735>",
    "tara": "<:tara:1446720662147305493>",
    "pam": "<:pam:1446720665980895334>",
    "piper": "<:piper:1446735599531851858>",
    "bo": "<:bo:1446735602505613427>",
    "poco": "<:poco:1446735606238675075>",
    "crow": "<:crow:1446735610667729019>",
    "mortis": "<:mortis:1446735613746348122>",
    "elprimo": "<:elprimo:1446735616841744494>",
    "dynamike": "<:dynamike:1446735619798601799>",
    "nita": "<:nita:1446735623380537479>",
    "jessie": "<:jessie:1446735626182332557>",
    "barley": "<:barley:1446735629135380603>",
    "spike": "<:spike:1446735631697842269>",
    "rico": "<:rico:1446735635141497006>",
    "brock": "<:brock:1446735638123647169>",
    "bull": "<:bull:1446735641198071869>",
    "colt": "<:colt:1446735644901511308>",
    "shelly": "<:shelly:1446735648081051679>",
}

def get_brawler_emoji(name: str) -> str:
    """Look up a brawler's custom emoji.

    The names used by the API may include spaces or punctuation, so we normalise
    the input by stripping spaces and dots and lower‑casing the result before
    looking it up in ``BRAWLER_EMOJIS``.  If no matching key is found the
    generic shield emoji is returned.
    """
    clean_name = name.lower().replace(" ", "").replace(".", "")
    return BRAWLER_EMOJIS.get(clean_name, "🛡️")


# Club role configuration (fill with real IDs).  Each entry describes how a
# tracked club maps to roles that should be added or removed when a member
# applies to that club.  The keys correspond to command names used by the cog.
CLUB_ROLE_CONFIG = {
    "revolt": {
        "bs_name": "TLG Revolt",
        "display_name": "Revolt",
        "add": [111111111111111111, 222222222222222222],
        "remove": [333333333333333333, 444444444444444444],
    },
    "tempest": {
        "bs_name": "TLG Tempest",
        "display_name": "Tempest",
        "add": [555555555555555555],
        "remove": [666666666666666666],
    },
    "dynamite": {
        "bs_name": "TLG Dynamite",
        "display_name": "Dynamite",
        "add": [777777777777777777],
        "remove": [888888888888888888],
    },
    "troopers": {
        "bs_name": "TLG Troopers",
        "display_name": "Troopers",
        "add": [999999999999999999],
        "remove": [101010101010101010],
    },
}

# Valid characters for Brawl Stars tags.  Used by ``verify_tag`` in the
# ``utils`` module.
_VALID_TAG_CHARS: set = set("PYLQGRJCUV0289")