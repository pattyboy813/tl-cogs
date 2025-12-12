# bstools/cog.py
import asyncio
import io
from typing import List, Optional, Union, Dict, Tuple

import discord
from discord.ext import tasks
from redbot.core import commands, checks
from redbot.core.bot import Red

from .constants import bstools_config, CLUB_ROLE_CONFIG
from .tags import (
    TagStore,
    format_tag,
    verify_tag,
    InvalidTag,
    TagAlreadySaved,
    TagAlreadyExists,
    InvalidArgument,
)
from .api import BrawlStarsAPI
from .embeds import (
    build_save_embed,
    build_accounts_embed,
    build_player_embed,
    build_club_embed,
    build_brawlers_embed,
    build_addclub_embed,
    build_delclub_embed,
    build_listclubs_embed,
    build_refreshclubs_embed,
    build_overview_embed,
    build_clubs_stats_embed,
)


class BrawlStarsTools(commands.Cog):
    """Unified Brawl Stars tools for players, brawlers, clubs, admin management & ticketing."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.api = BrawlStarsAPI(bot)
        self.tags = TagStore(bstools_config)
        self._ready = False
        self.overview_update_loop.start()

    async def cog_load(self):
        await self.api.start()
        self._ready = True

    def cog_unload(self):
        self.overview_update_loop.cancel()
        asyncio.create_task(self.api.close())

    # ------------------------- helpers -------------------------

    async def _get_player(self, tag: str):
        return await self.api.get_player(tag)

    async def _get_club(self, tag: str):
        return await self.api.get_club(tag)

    async def _resolve_player_tag(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]],
    ) -> Optional[str]:
        if isinstance(target, str):
            clean = format_tag(target)
            if verify_tag(clean):
                return clean
            await ctx.send("Invalid tag format.")
            return None

        if isinstance(target, (discord.Member, discord.User)):
            tags = await self.tags.get_all_tags(target.id)
            if not tags:
                await ctx.send(f"⚠️ {target.display_name} has no saved accounts.")
                return None
            return tags[0]

        tags = await self.tags.get_all_tags(ctx.author.id)
        if not tags:
            await ctx.send("⚠️ You have no saved accounts. Use `bs save #TAG`.")
            return None
        return tags[0]

    async def _ensure_in_applications_channel(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return False

        guild_conf = bstools_config.guild(ctx.guild)
        applications_channel_id = await guild_conf.applications_channel()
        if not applications_channel_id:
            await ctx.send(
                "⚠️ Applications channel is not configured. "
                "An admin must run `bs admin setapplicationschannel`."
            )
            return False

        if isinstance(ctx.channel, discord.Thread):
            parent_id = ctx.channel.parent_id
        else:
            parent_id = ctx.channel.id

        if parent_id != applications_channel_id:
            await ctx.send("❌ This command can only be used in the applications channel (or its threads).")
            return False

        return True

    async def _ensure_leadership(self, ctx: commands.Context) -> Optional[discord.Role]:
        guild_conf = bstools_config.guild(ctx.guild)
        role_id = await guild_conf.leadership_role()
        if not role_id:
            await ctx.send("⚠️ Leadership role is not configured. Use `bs admin setleadershiprole <role>`.")
            return None

        role = ctx.guild.get_role(role_id)
        if not role:
            await ctx.send("⚠️ The configured leadership role no longer exists. Reconfigure it.")
            return None

        if role not in ctx.author.roles:
            await ctx.send("❌ You need the leadership role to use this command.")
            return None

        return role

    async def _find_club_by_name(self, guild: discord.Guild, name: str) -> Optional[Dict]:
        clubs = await bstools_config.guild(guild).clubs()
        for club_data in clubs.values():
            if club_data.get("name", "").lower() == name.lower():
                return club_data
        return None

    async def _assign_member_to_club(
        self,
        ctx: commands.Context,
        member: discord.Member,
        club_key: str,
    ):
        lead_role = await self._ensure_leadership(ctx)
        if not lead_role:
            return

        if member.bot:
            await ctx.send("❌ Bots can't be assigned to clubs.")
            return

        club_conf = CLUB_ROLE_CONFIG.get(club_key)
        if not club_conf:
            await ctx.send("⚠️ This club is not configured in CLUB_ROLE_CONFIG.")
            return

        display_name: str = club_conf.get("display_name", club_key.title())
        bs_name: str = club_conf.get("bs_name", display_name)

        club_data = await self._find_club_by_name(ctx.guild, bs_name)
        if not club_data:
            await ctx.send(
                f"❌ I couldn't find a tracked club named **{bs_name}**.\n"
                f"Use `bs admin addclub #TAG` to add it first."
            )
            return

        club_tag = club_data.get("tag")
        if not club_tag:
            await ctx.send("⚠️ This club entry has no tag saved. Re-add it with `bs admin addclub`.")
            return

        tags = await self.tags.get_all_tags(member.id)
        if not tags:
            await ctx.send(f"❌ {member.mention} has no saved Brawl Stars account. They must run `bs save #TAG`.")
            return

        main_tag = tags[0]

        try:
            player = await self._get_player(main_tag)
        except RuntimeError as e:
            await ctx.send(f"❌ Error contacting Brawl Stars API: `{e}`")
            return

        if not player:
            await ctx.send("❌ I couldn't find that player's account from the API.")
            return

        player_club = player.get("club")
        if not player_club:
            await ctx.send(f"❌ {member.mention} is not in any club in-game.")
            return

        player_club_tag = player_club.get("tag")
        if player_club_tag != club_tag:
            await ctx.send(
                f"❌ {member.mention} is not in **{bs_name}** in-game.\n"
                f"Their current club appears to be **{player_club.get('name', 'Unknown')}** ({player_club_tag})."
            )
            return

        ign = player.get("name", "Unknown")

        new_nick = f"{ign} | {display_name}"
        try:
            await member.edit(nick=new_nick, reason=f"Assigned to {bs_name} ({display_name}) by {ctx.author}")
        except discord.Forbidden:
            await ctx.send("⚠️ I don't have permission to change that member's nickname.")
        except discord.HTTPException:
            await ctx.send("⚠️ Failed to change nickname, continuing with roles.")

        roles_to_add_ids = club_conf.get("add", [])
        roles_to_remove_ids = club_conf.get("remove", [])

        roles_to_add = [ctx.guild.get_role(rid) for rid in roles_to_add_ids if ctx.guild.get_role(rid)]
        roles_to_remove = [ctx.guild.get_role(rid) for rid in roles_to_remove_ids if ctx.guild.get_role(rid)]

        try:
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason=f"Assigned to {bs_name}")
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason=f"Assigned to {bs_name}")
        except discord.Forbidden:
            await ctx.send("⚠️ I don't have permission to modify one or more roles for that member.")
        except discord.HTTPException:
            await ctx.send("⚠️ Something went wrong while updating roles.")

        await ctx.send(
            f"✅ {member.mention} has been assigned to **{bs_name}**.\n"
            f"Nickname set to `{new_nick}` and roles updated."
        )

        if isinstance(ctx.channel, discord.Thread):
            thread: discord.Thread = ctx.channel
            try:
                await thread.edit(
                    locked=True,
                    archived=True,
                    reason=f"Application resolved: assigned to {bs_name}",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ------------------------- command groups -------------------------

    @commands.group(name="bs")
    async def bs_group(self, ctx: commands.Context):
        """Brawl Stars tools and player commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @bs_group.group(name="admin")
    @checks.admin_or_permissions(manage_guild=True)
    async def bs_admin_group(self, ctx: commands.Context):
        """Admin commands for managing clubs + data."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    # ------------------------- USER COMMANDS -------------------------

    @bs_group.command(name="save")
    async def bs_save(self, ctx: commands.Context, tag: str):
        clean = format_tag(tag)
        if not verify_tag(clean):
            await ctx.send("Invalid tag.")
            return

        try:
            player = await self._get_player(clean)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found. Double-check the tag.")
            return

        name = player.get("name", "Unknown Player")
        icon_id = player.get("icon", {}).get("id")

        try:
            idx = await self.tags.save_tag(ctx.author.id, clean)
        except TagAlreadySaved:
            await ctx.send("You already saved this tag.")
            return
        except TagAlreadyExists as e:
            other = ctx.guild.get_member(e.user_id) or f"User ID {e.user_id}"
            await ctx.send(f"This tag is already saved by **{other}**.")
            return
        except InvalidTag:
            await ctx.send("Invalid tag.")
            return

        embed = build_save_embed(ctx.author, name, clean, idx, icon_id)
        await ctx.send(embed=embed)

    @bs_group.command(name="accounts")
    async def bs_accounts(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        user = user or ctx.author
        tags = await self.tags.get_all_tags(user.id)
        embed = await build_accounts_embed(self._get_player, user, tags)
        await ctx.send(embed=embed)

    @bs_group.command(name="switch")
    async def bs_switch(self, ctx: commands.Context, account1: int, account2: int):
        try:
            await self.tags.switch_place(ctx.author.id, account1, account2)
        except InvalidArgument:
            await ctx.send("Invalid account positions.")
            return

        await ctx.send("✅ **Success:** Accounts reordered.")
        tags = await self.tags.get_all_tags(ctx.author.id)
        embed = await build_accounts_embed(self._get_player, ctx.author, tags)
        await ctx.send(embed=embed)

    @bs_group.command(name="unsave")
    async def bs_unsave(self, ctx: commands.Context, account: int):
        try:
            await self.tags.unlink_tag(ctx.author.id, account)
        except InvalidArgument:
            await ctx.send("Invalid account number.")
            return

        await ctx.send("✅ **Success:** Account removed.")
        tags = await self.tags.get_all_tags(ctx.author.id)
        embed = await build_accounts_embed(self._get_player, ctx.author, tags)
        await ctx.send(embed=embed)

    @bs_group.command(name="player")
    async def bs_player(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        tag = await self._resolve_player_tag(ctx, target)
        if not tag:
            return

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found.")
            return

        embed = build_player_embed(self.bot.user, player)
        await ctx.send(embed=embed)

    @bs_group.command(name="club")
    async def bs_club(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        tag = await self._resolve_player_tag(ctx, target)
        if not tag:
            return

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found.")
            return

        club = player.get("club")
        if not club:
            await ctx.send(f"**{player.get('name')}** is not in a club.")
            return

        club_tag = club.get("tag")

        try:
            data = await self._get_club(club_tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not data:
            await ctx.send("Club data not found.")
            return

        embed = build_club_embed(data)
        await ctx.send(embed=embed)

    @bs_group.command(name="brawlers")
    async def bs_brawlers(
        self,
        ctx: commands.Context,
        target: Optional[Union[discord.Member, discord.User, str]] = None,
    ):
        tag = await self._resolve_player_tag(ctx, target)
        if not tag:
            return

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not player:
            await ctx.send("Player not found.")
            return

        embed = build_brawlers_embed(player)
        await ctx.send(embed=embed)

    # ------------------------- ADMIN COMMANDS -------------------------

    @bs_admin_group.command(name="addclub")
    async def bs_add_club(self, ctx: commands.Context, tag: str):
        clean = format_tag(tag)
        if not verify_tag(clean):
            await ctx.send("Invalid club tag.")
            return

        club_tag = f"#{clean}"

        try:
            data = await self._get_club(club_tag)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        if not data:
            await ctx.send("Club not found. Double-check the tag.")
            return

        club_name = data.get("name", "Unknown Club")
        badge_id = data.get("badgeId")

        async with bstools_config.guild(ctx.guild).clubs() as clubs:
            clubs[club_tag] = {"tag": club_tag, "name": club_name}

        embed = build_addclub_embed(club_name, club_tag, badge_id)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="delclub")
    async def bs_del_club(self, ctx: commands.Context, tag: str):
        clean = format_tag(tag)
        club_tag = f"#{clean}"

        async with bstools_config.guild(ctx.guild).clubs() as clubs:
            if club_tag not in clubs:
                await ctx.send("That club tag is not currently tracked.")
                return
            removed = clubs.pop(club_tag)

        name = removed.get("name", "Unknown Club")
        embed = build_delclub_embed(name, club_tag)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="listclubs")
    async def bs_list_clubs(self, ctx: commands.Context):
        clubs = await bstools_config.guild(ctx.guild).clubs()
        embed = build_listclubs_embed(clubs)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="refreshclubs")
    async def bs_refresh_clubs(self, ctx: commands.Context):
        clubs = await bstools_config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `bs admin addclub #TAG` first.")
            return

        updated = 0
        failed = 0

        async with bstools_config.guild(ctx.guild).clubs() as clubs_conf:
            for club_tag, club_data in list(clubs_conf.items()):
                tag = club_data.get("tag") or club_tag
                if not tag:
                    failed += 1
                    continue

                try:
                    data = await self._get_club(tag)
                except RuntimeError:
                    failed += 1
                    continue

                if not data:
                    failed += 1
                    continue

                new_name = data.get("name") or club_data.get("name", "Unknown Club")
                if new_name != club_data.get("name"):
                    clubs_conf[club_tag]["name"] = new_name
                    updated += 1

        embed = build_refreshclubs_embed(updated, failed)
        await ctx.send(embed=embed)

    @bs_admin_group.command(name="clubs")
    async def bs_admin_clubs(self, ctx: commands.Context):
        clubs = await bstools_config.guild(ctx.guild).clubs()
        if not clubs:
            await ctx.send("No clubs tracked yet. Use `bs admin addclub #TAG` first.")
            return

        tasks_list: List[asyncio.Task] = []
        club_meta: List[Tuple[str, str]] = []

        for club in clubs.values():
            tag = club.get("tag")
            name = club.get("name", "Unknown Club")
            if not tag:
                continue
            club_meta.append((name, tag))
            tasks_list.append(asyncio.create_task(self._get_club(tag)))

        if not tasks_list:
            await ctx.send("No valid club entries found.")
            return

        try:
            results = await asyncio.gather(*tasks_list, return_exceptions=True)
        except RuntimeError as e:
            await ctx.send(str(e))
            return

        collected: List[Tuple[str, str, Dict]] = []
        for (name, tag), result in zip(club_meta, results):
            if isinstance(result, Exception) or not result:
                continue
            collected.append((name, tag, result))

        if not collected:
            await ctx.send("Could not fetch data for any clubs.")
            return

        overview_embed = build_overview_embed(collected)
        detail_embed = build_clubs_stats_embed(collected)

        await ctx.send(embed=overview_embed)
        await ctx.send(embed=detail_embed)

    @bs_admin_group.command(name="setoverviewchannel")
    async def bs_set_overview_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        await bstools_config.guild(ctx.guild).overview_channel.set(channel.id)
        await bstools_config.guild(ctx.guild).overview_message.set(None)
        await ctx.send(f"📡 Overview updates will now be posted in {channel.mention}.")

    @bs_admin_group.command(name="setleadershiprole")
    async def bs_set_leadership_role(self, ctx: commands.Context, role: discord.Role):
        await bstools_config.guild(ctx.guild).leadership_role.set(role.id)
        await ctx.send(f"✅ Leadership role set to {role.mention}.")

    @bs_admin_group.command(name="setapplicationschannel")
    async def bs_set_applications_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        await bstools_config.guild(ctx.guild).applications_channel.set(channel.id)
        await ctx.send(f"✅ Applications will create threads in {channel.mention}.")

    # ------------------------- CLUB APPLY -------------------------

    @commands.command(name="clubapply")
    @commands.guild_only()
    async def club_apply(self, ctx: commands.Context):
        if not await self._ensure_in_applications_channel(ctx):
            return

        guild_conf = bstools_config.guild(ctx.guild)
        applications_channel_id = await guild_conf.applications_channel()
        if not applications_channel_id:
            await ctx.send(
                "⚠️ Applications channel is not configured. An admin must run `bs admin setapplicationschannel`."
            )
            return

        applications_channel = ctx.guild.get_channel(applications_channel_id)
        if not isinstance(applications_channel, discord.TextChannel):
            await ctx.send("⚠️ The configured applications channel is invalid.")
            return

        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            notice = await ctx.send(f"{ctx.author.mention} Check your DMs! 📬")
            await asyncio.sleep(8)
            await notice.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            dm = await ctx.author.create_dm()
            await dm.send(
                "👋 Hey! Let's set up your club application.\n\n"
                "First, send a **clear screenshot** of your Brawl Stars profile (not the club screen)."
            )
        except discord.Forbidden:
            await ctx.send(
                f"{ctx.author.mention} ❌ I can't DM you. "
                "Please enable DMs from server members and try again."
            )
            return

        def dm_check(m: discord.Message):
            return m.author.id == ctx.author.id and m.channel == dm

        try:
            screenshot_msg = await self.bot.wait_for("message", check=dm_check, timeout=300)
        except asyncio.TimeoutError:
            await dm.send("⏰ Timed out waiting for a screenshot. Start again in the server with `clubapply`.")
            return

        if not screenshot_msg.attachments:
            await dm.send("❌ I didn't see any attachments. Please restart the process and attach a screenshot.")
            return

        screenshot = screenshot_msg.attachments[0]

        tags = await self.tags.get_all_tags(ctx.author.id)
        if tags:
            tag = tags[0]
            await dm.send(f"✅ I found your saved main account: `#{tag}`.\nI'll use this for your application.")
        else:
            await dm.send(
                "Now, please send your **player tag** as text (e.g. `#9L0P0ABC`).\n"
                "I'll use this to pull your IGN and trophies from the official API."
            )
            try:
                tag_msg = await self.bot.wait_for("message", check=dm_check, timeout=300)
            except asyncio.TimeoutError:
                await dm.send("⏰ Timed out waiting for your tag. Start again in the server with `clubapply`.")
                return

            raw_tag = tag_msg.content.strip()
            tag = format_tag(raw_tag)
            if not verify_tag(tag):
                await dm.send("❌ That doesn't look like a valid Brawl Stars tag. Please restart and double-check.")
                return

        try:
            player = await self._get_player(tag)
        except RuntimeError as e:
            await dm.send(f"❌ I couldn't reach the Brawl Stars API:\n`{e}`")
            return

        if not player:
            await dm.send("❌ I couldn't find a player with that tag. Double-check your in-game tag and restart.")
            return

        ign = player.get("name", "Unknown")
        trophies = player.get("trophies", 0)
        club_info = player.get("club")
        club_text = "No club" if not club_info else f"{club_info.get('name', 'Unknown')} ({club_info.get('tag', '???')})"

        class ConfirmView(discord.ui.View):
            def __init__(self, author: discord.User, timeout: float = 180):
                super().__init__(timeout=timeout)
                self.author = author
                self.value: Optional[bool] = None

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                if interaction.user.id != self.author.id:
                    await interaction.response.send_message("These buttons aren't for you.", ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
            async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.value = True
                await interaction.response.defer()
                self.stop()

            @discord.ui.button(label="No", style=discord.ButtonStyle.red)
            async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.value = False
                await interaction.response.defer()
                self.stop()

        confirm_embed = discord.Embed(
            title="Confirm your Brawl Stars details",
            color=discord.Color.green(),
        )
        confirm_embed.add_field(name="IGN", value=f"**{ign}**", inline=True)
        confirm_embed.add_field(name="Tag", value=f"`#{format_tag(tag)}`", inline=True)
        confirm_embed.add_field(name="Trophies", value=f"{trophies:,}", inline=True)
        confirm_embed.add_field(name="Current Club", value=club_text, inline=False)
        confirm_embed.set_footer(text="Is this information correct?")

        view = ConfirmView(ctx.author)
        await dm.send(embed=confirm_embed, view=view)
        await view.wait()

        if view.value is None:
            await dm.send("⏰ You didn't press a button in time. Start again with `clubapply`.")
            return
        if view.value is False:
            await dm.send("❌ Application cancelled. You can restart with `clubapply` in the server.")
            return

        try:
            await self.tags.save_tag(ctx.author.id, tag)
        except TagAlreadySaved:
            pass
        except TagAlreadyExists:
            await dm.send(
                "⚠️ This tag is already linked to another Discord user in the database. "
                "A staff member may need to resolve this manually."
            )
        except InvalidTag:
            await dm.send("⚠️ Somehow that tag became invalid. Staff will need to handle this manually.")

        await dm.send(
            "✅ Details confirmed!\n\n"
            "Finally, please give a **brief description** of what you're looking for in a club "
            "(e.g. casual / competitive, language, active time, etc.)."
        )
        try:
            description_msg = await self.bot.wait_for("message", check=dm_check, timeout=600)
        except asyncio.TimeoutError:
            await dm.send("⏰ Timed out waiting for your description. Start again with `clubapply`.")
            return

        description_text = description_msg.content[:1000]

        try:
            thread = await applications_channel.create_thread(
                name=f"{ign} | Club Application",
                type=discord.ChannelType.private_thread,
            )
        except discord.Forbidden:
            await dm.send("❌ I couldn't create a thread in the applications channel. Staff will need to fix my permissions.")
            return

        try:
            await thread.add_user(ctx.author)
        except discord.Forbidden:
            pass

        lead_role_id = await guild_conf.leadership_role()
        lead_role = ctx.guild.get_role(lead_role_id) if lead_role_id else None
        if lead_role:
            for member in lead_role.members:
                try:
                    await thread.add_user(member)
                except discord.Forbidden:
                    continue

        screenshot_bytes = await screenshot.read()
        file = discord.File(io.BytesIO(screenshot_bytes), filename=screenshot.filename or "profile.png")

        profile_link = f"https://brawlstats.com/profile/{format_tag(tag)}"
        thread_embed = discord.Embed(
            title=f"New Club Application: {ign}",
            color=discord.Color.blurple(),
        )
        thread_embed.add_field(name="Applicant", value=f"{ctx.author.mention} ({ctx.author.id})", inline=False)
        thread_embed.add_field(name="IGN", value=f"**{ign}**", inline=True)
        thread_embed.add_field(name="Tag", value=f"`#{format_tag(tag)}`", inline=True)
        thread_embed.add_field(name="Trophies", value=f"{trophies:,}", inline=True)
        thread_embed.add_field(name="Current Club", value=club_text, inline=False)
        thread_embed.add_field(name="What they want", value=description_text, inline=False)
        thread_embed.add_field(name="Brawlstats Link", value=profile_link, inline=False)
        thread_embed.set_footer(text="Use this thread to handle the application.")

        content = lead_role.mention if lead_role else ""
        await thread.send(content=content, embed=thread_embed, file=file)

        await dm.send("🎉 Your application has been submitted! Club leadership will review it in their private thread.")

    # ------------------------- CLUB ASSIGN COMMANDS -------------------------

    @commands.command(name="revolt")
    @commands.guild_only()
    async def revolt_command(self, ctx: commands.Context, member: discord.Member):
        await self._assign_member_to_club(ctx, member, "revolt")

    @commands.command(name="tempest")
    @commands.guild_only()
    async def tempest_command(self, ctx: commands.Context, member: discord.Member):
        await self._assign_member_to_club(ctx, member, "tempest")

    @commands.command(name="dynamite")
    @commands.guild_only()
    async def dynamite_command(self, ctx: commands.Context, member: discord.Member):
        await self._assign_member_to_club(ctx, member, "dynamite")

    @commands.command(name="troopers")
    @commands.guild_only()
    async def troopers_command(self, ctx: commands.Context, member: discord.Member):
        await self._assign_member_to_club(ctx, member, "troopers")

    # ------------------------- BACKGROUND TASK -------------------------

    @tasks.loop(minutes=10)
    async def overview_update_loop(self):
        await self.bot.wait_until_red_ready()

        for guild in self.bot.guilds:
            conf = bstools_config.guild(guild)

            channel_id = await conf.overview_channel()
            if not channel_id:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            clubs = await conf.clubs()
            if not clubs:
                continue

            club_meta: List[Tuple[str, str]] = []
            tasks_list: List[asyncio.Task] = []
            for club in clubs.values():
                tag = club.get("tag")
                name = club.get("name", "Unknown Club")
                if not tag:
                    continue
                club_meta.append((name, tag))
                tasks_list.append(asyncio.create_task(self._get_club(tag)))

            if not tasks_list:
                continue

            results = await asyncio.gather(*tasks_list, return_exceptions=True)

            collected: List[Tuple[str, str, Dict]] = []
            for (name, tag), result in zip(club_meta, results):
                if isinstance(result, Exception) or not result:
                    continue
                collected.append((name, tag, result))

            if not collected:
                continue

            overview_embed = build_overview_embed(collected)

            msg_id = await conf.overview_message()
            message: Optional[discord.Message] = None

            if msg_id:
                try:
                    message = await channel.fetch_message(msg_id)
                except discord.NotFound:
                    message = None

            if message:
                await message.edit(embed=overview_embed)
            else:
                new_msg = await channel.send(embed=overview_embed)
                await conf.overview_message.set(new_msg.id)

    @overview_update_loop.error
    async def overview_update_loop_error(self, error):
        print(f"[BrawlStarsTools] overview_update_loop error: {error}")
