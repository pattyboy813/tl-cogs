import discord

from .helper import Letter, Symbol

class LegendMenu:
    def __init__(self, welcome):
        embed = welcome.embed
        self.menu = {
            "main": {
                "embed": embed(title="Welcome", color=discord.Color.orange(),
                               description="Welcome to the **Legend Clash Royale** Server, {0.mention}! "
                                           "We are one of the oldest and biggest families in "
                                           "Clash Royale with our 700 members and 14 clans! "
                                           "<a:goblinstab:468708996153475072>\n\n"
                                           "We are glad you joined us, can we ask a few questions "
                                           "to customize your experience?"),
                "thumbnail": "https://i.imgur.com/8SRsdQz.png",
                "options": [
                    {
                        "name": "Yes please!",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "refferal_menu"
                        }
                    },
                    {
                        "name": "Skip it, and talk to our friendly staff.",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "leave_alone"
                        }
                    }
                ],
                "go_back": False
            },
            "refferal_menu": {
                "embed": embed(title="How did you get here?", color=discord.Color.orange(),
                               description="We know you are from the interwebz. "
                                           "But where exactly did you find us?"),
                "options": [
                    {
                        "name": "Legend Website",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "location_menu"
                        }
                    },
                    {
                        "name": "RoyaleAPI Website",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "location_menu"
                        }
                    },
                    {
                        "name": "Reddit",
                        "emoji": Letter.c,
                        "execute": {
                            "menu": "location_menu"
                        }
                    },
                    {
                        "name": "Discord",
                        "emoji": Letter.d,
                        "execute": {
                            "menu": "location_menu"
                        }
                    },
                    {
                        "name": "Twitter",
                        "emoji": Letter.e,
                        "execute": {
                            "menu": "location_menu"
                        }
                    },
                    {
                        "name": "From in-game",
                        "emoji": Letter.f,
                        "execute": {
                            "menu": "location_menu"
                        }
                    },
                    {
                        "name": "Friend or Family",
                        "emoji": Letter.g,
                        "execute": {
                            "menu": "location_menu"
                        }
                    },
                    {
                        "name": "Other",
                        "emoji": Letter.h,
                        "execute": {
                            "menu": "location_menu"
                        }
                    }
                ],
                "go_back": True,
                "track": True
            },
            "location_menu": {
                "embed": embed(title="What part of the world do you come from?", color=discord.Color.orange(),
                               description="To better serve you, "
                                           "pick the region you currently live in."),
                "options": [
                    {
                        "name": "North America",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "South America",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "Northern Africa",
                        "emoji": Letter.c,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "Southern Africa",
                        "emoji": Letter.d,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "Europe",
                        "emoji": Letter.e,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "Middle East",
                        "emoji": Letter.f,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "Asia",
                        "emoji": Letter.g,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "Southeast Asia",
                        "emoji": Letter.h,
                        "execute": {
                            "menu": "age_menu"
                        }
                    },
                    {
                        "name": "Australia",
                        "emoji": Letter.i,
                        "execute": {
                            "menu": "age_menu"
                        }
                    }
                ],
                "go_back": True,
                "track": True
            },
            "age_menu": {
                "embed": embed(title="How old are you?", color=discord.Color.orange(),
                               description="Everyone is welcome! "
                                           "However, some clans do require you to be of a"
                                           " certain age group. Please pick one."),
                "options": [
                    {
                        "name": "Under 16",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    },
                    {
                        "name": "16-20",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    },
                    {
                        "name": "21-30",
                        "emoji": Letter.c,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    },
                    {
                        "name": "31-40",
                        "emoji": Letter.d,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    },
                    {
                        "name": "41-50",
                        "emoji": Letter.e,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    },
                    {
                        "name": "51-60",
                        "emoji": Letter.f,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    },
                    {
                        "name": "61 or Above",
                        "emoji": Letter.g,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    },
                    {
                        "name": "Prefer Not to Answer",
                        "emoji": Letter.h,
                        "execute": {
                            "menu": "save_tag_menu"
                        }
                    }
                ],
                "go_back": True,
                "track": True
            },
            "save_tag_menu": {
                "embed": embed(title="What is your Clash Royale player tag?", color=discord.Color.orange(),
                               description="Before we let you talk in the server, we need to take a look at your stats. "
                                           "To do that, we need your Clash Royale player tag.\n\n"),
                "options": [
                    {
                        "name": "Continue",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "save_tag"
                        }
                    },
                    {
                        "name": "I don't play Clash Royale",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "other_game"
                        }
                    }
                ],
                "go_back": True
            },
            "save_tag": {
                "embed": embed(title="Type in your tag", color=discord.Color.orange(),
                               description="Please type **!savetag #YOURTAG** below to submit your ID.\n\n"
                                           "You can find your player tag in your profile in game."),
                "image": "https://legendclans.com/wp-content/uploads/2017/11/profile_screen3.png",
                "options": [],
                "go_back": True
            },
            "choose_path": {
                "embed": embed(title="So, why are you here?", color=discord.Color.orange(),
                               description="Please select your path "
                                           "below to get started."),
                "options": [
                    {
                        "name": "I am just visiting.",
                        "emoji": Letter.a,
                        "execute": {
                            "function": "guest"
                        }
                    },
                    {
                        "name": "I want to join a clan.",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "academy_coaching"
                        }
                    },
                    {
                        "name": "I am already in one of your clans.",
                        "emoji": Letter.c,
                        "execute": {
                            "function": "verify_membership"
                        }
                    }
                ],
                "go_back": False,
                "track": True
            },
            "academy_coaching": {
                "embed": embed(title="Are you interested in coaching?", color=discord.Color.orange(),
                               description="We provide all of our members "
                                           "with free coaching and training."),
                "options": [
                    {
                        "name": "I am interested in coaching.",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "join_clan"
                        }
                    },
                    {
                        "name": "I want to coach people.",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "join_clan"
                        }
                    },
                    {
                        "name": "Not interested.",
                        "emoji": Letter.c,
                        "execute": {
                            "menu": "join_clan"
                        }
                    }
                ],
                "go_back": True,
                "track": True
            },
            "join_clan": {
                "embed": embed(title="Legend Family Clans", color=discord.Color.orange(),
                               description="Here are all our clans, which clan do you prefer?"),
                "dynamic_options": "clans_options",
                "options": [],
                "go_back": True,
                "track": True
            },
            "end_member": {
                "embed": embed(title="That was it", color=discord.Color.orange(),
                               description="Your chosen clan has been informed. "
                                           " Please wait in #welcome-gate channel "
                                           "while a discord officer comes to approve you.\n\n"
                                           " Please do not join any clans without talking to an officer.\n\n"
                                           "**Enjoy your stay!**"),
                "options": [
                    {
                        "name": "Go to #welcome-gate",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "welcome_gate"
                        }
                    }
                ],
                "go_back": False,
                "finished": True
            },
            "end_human": {
                "embed": embed(title="Requesting assistance", color=discord.Color.orange(),
                               description="We have notified our officers about your information."
                                           " Please wait in #welcome-gate "
                                           "channel while an officer comes and helps you.\n\n"
                                           " Please do not join any clans without talking to an officer.\n\n"
                                           "**Enjoy your stay!**"),
                "options": [
                    {
                        "name": "Go to #welcome-gate",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "welcome_gate"
                        }
                    }
                ],
                "go_back": False,
                "finished": True
            },
            "end_guest": {
                "embed": embed(title="Enjoy your stay", color=discord.Color.orange(),
                               description="Welcome to the **Legend Family** Discord server. "
                               "As a guest, you agree to the following rules:\n\n"
                               "• Respect others' opinions. If you disagree, please do so "
                               "in a constructive manner.\n• This is an English only server, "
                               "please use any other languages in a private message.\n"
                               "• Do not spam, and avoid ever using @clanname without "
                               "permission from clan managers or deputies.\n"
                               "• No advertisement of any kind, e.g. clans, websites, "
                               "discord invites, etc.\n• Use #bot-spam for bot features, "
                               "e.g. !deck or !payday.\n• Respect and do not subvert "
                               "moderators or managers.\n• A good rule is to talk to "
                               "people as if you were talking to them face to face.\n\n"
                               "Failure to follow these rules will get you kicked from the server. "
                               "Repeat offenders will be banned.\n\nYou can chat with "
                               "family members and guests in `#global-chat`. "
                               "For games, you can check out `#heist` `#duels` "
                               "and `#challenges`.\n\nIf you would like to invite "
                               "your friends to join this server, you may use this "
                               "Discord invite: <https://discord.gg/yhD84nK>\n\n"
                               "Additional help and information: https://legendclans.com\n\n"
                               "Thanks + enjoy!\n"),
                "options": [
                    {
                        "name": "Go to #global-chat",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "global_chat"
                        }
                    }
                ],
                "go_back": False,
                "finished": True
            },
            "give_tags": {
                "embed": embed(title="Membership verified", color=discord.Color.orange(),
                               description="We have unlocked all member channels for you, enjoy your stay!"),
                "options": [
                    {
                        "name": "Go to #global-chat",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "global_chat"
                        }
                    }
                ],
                "go_back": False,
                "finished": True
            },
            "other_game": {
                "embed": embed(title="Any other game?", color=discord.Color.orange(),
                               description="It's okay, but we play Brawl Stars, do you?"),
                "options": [
                    {
                        "name": "I play Brawl Stars!",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "brawl_stars"
                        }
                    },
                    {
                        "name": "I don't play that either.",
                        "emoji": Letter.b,
                        "execute": {
                            "menu": "leave_alone"
                        }
                    }
                ],
                "go_back": False,
                "finished": True
            },
            "brawl_stars": {
                "embed": embed(title="Legend Brawl Stars", color=discord.Color.orange(),
                               description="Guess what, we have just the right server for you.\n\n"
                                           "Click here to join: https://discord.gg/5ww5D3q"),
                "options": [
                    {
                        "name": "Done",
                        "emoji": Symbol.white_check_mark,
                        "execute": {}
                    }
                ],
                "go_back": False,
                "hide_options": True
            },
            "leave_alone": {
                "embed": embed(title="Enjoy your stay", color=discord.Color.orange(),
                               description="We look forward to welcoming "
                                           "you into the Legend Clan Family!\n\n"
                                           "You can go talk to an officer in #welcome-gate. "),
                "options": [
                    {
                        "name": "Go to #welcome-gate",
                        "emoji": Letter.a,
                        "execute": {
                            "menu": "welcome_gate"
                        }
                    }
                ],
                "go_back": False,
                "finished": True
            },
            "global_chat": {
                "embed": embed(title="#global-chat", color=discord.Color.orange(),
                               description="Click here: https://discord.gg/T7XdjFS"),
                "options": [
                    {
                        "name": "Done",
                        "emoji": Symbol.white_check_mark,
                        "execute": {}
                    }
                ],
                "go_back": False,
                "hide_options": True
            },
            "welcome_gate": {
                "embed": embed(title="#welcome-gate", color=discord.Color.orange(),
                               description="Click here: https://discord.gg/yhD84nK"),
                "options": [
                    {
                        "name": "Done",
                        "emoji": Symbol.white_check_mark,
                        "execute": {}
                    }
                ],
                "go_back": False,
                "hide_options": True
            }
        }
