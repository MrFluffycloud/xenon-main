import xenon_worker as wkr
import utils
import asyncio
import pymongo
from datetime import datetime, timedelta
import random
import checks

from backups import BackupSaver, BackupLoader

MAX_BACKUPS = 15


class BackupListMenu(wkr.ListMenu):
    embed_kwargs = {"title": "Your Backups"}

    async def get_items(self):
        args = {
            "limit": 10,
            "skip": self.page * 10,
            "sort": [("timestamp", pymongo.DESCENDING)],
            "filter": {
                "creator": self.ctx.author.id,
            }
        }
        backups = self.ctx.bot.db.backups.find(**args)
        items = []
        async for backup in backups:
            items.append((
                backup["_id"] + (" ⏲️" if backup.get("interval") else ""),
                f"{backup['data']['name']} (`{utils.datetime_to_string(backup['timestamp'])} UTC`)"
            ))

        return items


class Backups(wkr.Module):
    @wkr.Module.listener()
    async def on_load(self, *_, **__):
        await self.bot.db.backups.create_index([("creator", pymongo.ASCENDING)])
        await self.bot.db.backups.create_index([("timestamp", pymongo.ASCENDING)])
        await self.bot.db.backups.create_index([("data.id", pymongo.ASCENDING)])
        await self.bot.db.backups.create_index([("msg_retention", pymongo.ASCENDING)])

    @wkr.Module.task(hours=24)
    async def message_retention(self):
        await self.bot.db.update_many(
            {
                "msg_retention": True,
                "timestamp": {
                    "$lte": datetime.utcnow() - timedelta(days=30)
                }
            },
            {
                "$unset": "data.messages"
            }
        )

    @wkr.Module.command(aliases=("backups", "bu"))
    async def backup(self, ctx):
        """
        Create & load private backups of your servers
        """
        await ctx.invoke("help backup")

    @backup.command(aliases=("c",))
    @wkr.guild_only
    @wkr.has_permissions(administrator=True)
    @wkr.bot_has_permissions(administrator=True)
    @checks.is_premium()
    @wkr.cooldown(1, 10, bucket=wkr.CooldownType.GUILD)
    async def create(self, ctx, chatlog: int = 0):
        """
        Create a backup
        
        Get more help on the [wiki](https://wiki.xenon.bot/backups#creating-a-backup).


        __Examples__

        No chatlog: ```{b.prefix}backup create```
        50 messages per channel: ```{b.prefix}backup create 50```
        """
        max_backups = MAX_BACKUPS
        if ctx.premium == checks.PremiumLevel.ONE:
            max_backups = 50
            chatlog = min(chatlog, 50)

        elif ctx.premium == checks.PremiumLevel.TWO:
            max_backups = 100
            chatlog = min(chatlog, 100)

        elif ctx.premium == checks.PremiumLevel.THREE:
            max_backups = 250
            chatlog = min(chatlog, 250)

        backup_count = await ctx.bot.db.backups.count_documents({"creator": ctx.author.id})
        if backup_count >= max_backups:
            raise ctx.f.ERROR(
                f"You have **exceeded the maximum count** of backups. (`{backup_count}/{max_backups}`)\n"
                f"You need to **delete old backups** with `{ctx.bot.prefix}backup delete <id>` or **buy "
                f"[Xenon Premium](https://www.patreon.com/merlinfuchs)** to create new backups.."
            )

        status_msg = await ctx.f_send("**Creating Backup** ...", f=ctx.f.WORKING)
        guild = await ctx.get_full_guild()
        backup = BackupSaver(ctx.client, guild)
        await backup.save(chatlog)

        backup_id = utils.unique_id()
        await ctx.bot.db.backups.insert_one({
            "_id": backup_id,
            "msg_retention": True,
            "creator": ctx.author.id,
            "timestamp": datetime.utcnow(),
            "data": backup.data
        })

        embed = ctx.f.format(f"Successfully **created backup** with the id `{backup_id}`.", f=ctx.f.SUCCESS)["embed"]
        embed.setdefault("fields", []).append({
            "name": "Usage",
            "value": f"```{ctx.bot.prefix}backup load {backup_id}```\n"
                     f"```{ctx.bot.prefix}backup info {backup_id}```"
        })
        await ctx.client.edit_message(status_msg, embed=embed)

    @backup.command(aliases=("l",))
    @wkr.guild_only
    @wkr.has_permissions(administrator=True)
    @wkr.bot_has_permissions(administrator=True)
    @checks.is_premium()
    @wkr.cooldown(1, 60, bucket=wkr.CooldownType.GUILD)
    async def load(self, ctx, backup_id, chatlog: int = 0, *options):
        """
        Load a backup
        
        Get more help on the [wiki](https://wiki.xenon.bot/backups#loading-a-backup).


        __Arguments__

        **backup_id**: The id of the backup or the guild id of the latest automated backup
        **chatlog**: The count of messages to load per channel
        **options**: A list of options (See examples)


        __Examples__

        Default options: ```{b.prefix}backup load oj1xky11871fzrbu```
        Only roles: ```{b.prefix}backup load oj1xky11871fzrbu !* roles```
        Everything but bans: ```{b.prefix}backup load oj1xky11871fzrbu !bans```
        """
        backup_d = await ctx.client.db.backups.find_one({"_id": backup_id, "creator": ctx.author.id})
        if backup_d is None:
            raise ctx.f.ERROR(f"You have **no backup** with the id `{backup_id}`.")

        warning_msg = await ctx.f_send("Are you sure that you want to load this backup?\n"
                                       f"Please put the managed role called `{ctx.bot.user.name}` above all other "
                                       f"roles before clicking the ✅ reaction.\n\n"
                                       "__**All channels and roles will get replaced!**__\n\n"
                                       "*Also keep in mind that you can only load up to 250 roles per day.*", f=ctx.f.WARNING)
        reactions = ("✅", "❌")
        for reaction in reactions:
            await ctx.client.add_reaction(warning_msg, reaction)

        try:
            data, = await ctx.client.wait_for(
                "message_reaction_add",
                ctx.shard_id,
                check=lambda d: d["message_id"] == warning_msg.id and
                                d["user_id"] == ctx.author.id and
                                d["emoji"]["name"] in reactions,
                timeout=60
            )
        except asyncio.TimeoutError:
            await ctx.client.delete_message(warning_msg)
            return

        await ctx.client.delete_message(warning_msg)
        if data["emoji"]["name"] != "✅":
            return

        guild = await ctx.get_full_guild()
        backup = BackupLoader(ctx.client, guild, backup_d["data"], reason="Backup loaded by " + str(ctx.author))
        await backup.load(chatlog, **utils.backup_options(options))

    @backup.command(aliases=("del", "remove", "rm"))
    @wkr.cooldown(5, 30)
    async def delete(self, ctx, backup_id):
        """
        Delete one of your backups
        
        Get more help on the [wiki](https://wiki.xenon.bot/backups#deleting-a-backup).
        __**This cannot be undone**__


        __Examples__

        ```{b.prefix}backup delete 3zpssue46g```
        """
        result = await ctx.client.db.backups.delete_one({"_id": backup_id, "creator": ctx.author.id})
        if result.deleted_count > 0:
            raise ctx.f.SUCCESS("Successfully **deleted backup**.")

        else:
            raise ctx.f.ERROR(f"You have **no backup** with the id `{backup_id}`.")

    @backup.command(aliases=("clear",))
    @wkr.cooldown(1, 60, bucket=wkr.CooldownType.GUILD)
    async def purge(self, ctx):
        """
        Delete all your backups
        __**This cannot be undone**__


        __Examples__

        ```{b.prefix}backup purge```
        """
        warning_msg = await ctx.f_send("Are you sure that you want to delete all your backups?\n"
                                       "__**This cannot be undone!**__", f=ctx.f.WARNING)
        reactions = ("✅", "❌")
        for reaction in reactions:
            await ctx.client.add_reaction(warning_msg, reaction)

        try:
            data, = await ctx.client.wait_for(
                "message_reaction_add",
                ctx.shard_id,
                check=lambda d: d["message_id"] == warning_msg.id and
                                d["user_id"] == ctx.author.id and
                                d["emoji"]["name"] in reactions,
                timeout=60
            )
        except asyncio.TimeoutError:
            await ctx.client.delete_message(warning_msg)
            return

        await ctx.client.delete_message(warning_msg)
        if data["emoji"]["name"] != "✅":
            return

        await ctx.client.db.backups.delete_many({"creator": ctx.author.id})
        raise ctx.f.SUCCESS("Successfully **deleted all your backups**.")

    @backup.command(aliases=("ls",))
    @wkr.cooldown(1, 10)
    async def list(self, ctx):
        """
        Get a list of your backups


        __Examples__

        ```{b.prefix}backup list```
        """
        menu = BackupListMenu(ctx)
        await menu.start()

    @backup.command(aliases=("i",))
    @wkr.cooldown(5, 30)
    async def info(self, ctx, backup_id):
        """
        Get information about a backup


        __Arguments__

        **backup_id**: The id of the backup or the guild id to for latest automated backup


        __Examples__

        ```{b.prefix}backup info 3zpssue46g```
        """
        backup = await ctx.client.db.backups.find_one({"_id": backup_id, "creator": ctx.author.id})
        if backup is None:
            raise ctx.f.ERROR(f"You have **no backup** with the id `{backup_id}`.")

        backup["data"].pop("members", None)
        guild = wkr.Guild(backup["data"])

        channels = utils.channel_tree(guild.channels)
        if len(channels) > 1024:
            channels = channels[:1000] + "\n...\n```"

        roles = "```{}```".format("\n".join([
            r.name for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        ]))
        if len(roles) > 1024:
            roles = roles[:1000] + "\n...\n```"

        raise ctx.f.DEFAULT(embed={
            "title": guild.name,
            "fields": [
                {
                    "name": "Created At",
                    "value": utils.datetime_to_string(backup["timestamp"]) + " UTC",
                    "inline": False
                },
                {
                    "name": "Channels",
                    "value": channels,
                    "inline": True
                },
                {
                    "name": "Roles",
                    "value": roles,
                    "inline": True
                }
            ]
        })

    @backup.command(aliases=("iv",))
    @wkr.guild_only
    @wkr.has_permissions(administrator=True)
    @wkr.bot_has_permissions(administrator=True)
    @wkr.cooldown(1, 10, bucket=wkr.CooldownType.GUILD)
    async def interval(self, ctx, *interval):
        """
        Manage automated backups
        
        Get more help on the [wiki](https://wiki.xenon.bot/en/backups#automated-backups-interval).


        __Arguments__

        **interval**: The time between every backup or "off". (min 24h)
                    Supported units: hours(h), days(d), weeks(w)
                    Example: 1d 12h


        __Examples__

        ```{b.prefix}backup interval 24h```
        """
        if len(interval) > 0:
            await ctx.invoke("backup interval on " + " ".join(interval))
            return

        interval = await ctx.bot.db.intervals.find_one({"_id": ctx.guild_id})
        if interval is None:
            raise ctx.f.INFO("The **backup interval is** currently turned **off**.\n"
                             f"Turn it on with `{ctx.bot.prefix}backup interval on 24h`.")

        else:
            raise ctx.f.INFO(embed={
                "author": {
                    "name": "Backup Interval"
                },
                "fields": [
                    {
                        "name": "Interval",
                        "value": utils.timedelta_to_string(timedelta(hours=interval["interval"])),
                        "inline": True
                    },
                    {
                        "name": "Next Backup",
                        "value": utils.datetime_to_string(interval["next"]),
                        "inline": True
                    },
                    {
                        "name": "Keep",
                        "value": interval.get("keep", 1),
                        "inline": "False"
                    }
                ]
            })

    @interval.command(aliases=["enable"])
    @checks.is_premium()
    @wkr.cooldown(1, 10, bucket=wkr.CooldownType.GUILD)
    async def on(self, ctx, *interval):
        """
        Turn on automated backups


        __Arguments__

        **interval**: The time between every backup. (min 24h)
                    Supported units: hours(h), days(d), weeks(w)
                    Example: 1d 12h


        __Examples__

        ```{b.prefix}backup interval on 24h```
        """
        units = {
            "h": 1,
            "d": 24,
            "w": 24 * 7
        }

        hours = 0
        chatlog = 0
        for arg in interval:
            try:
                chatlog = int(arg)
                continue
            except ValueError:
                pass

            try:
                count, unit = int(arg[:-1]), arg[-1]
            except (ValueError, IndexError):
                continue

            multiplier = units.get(unit.lower(), 1)
            hours += count * multiplier

        if ctx.premium == checks.PremiumLevel.ONE:
            chatlog = min(chatlog, 50)
            hours = max(hours, 12)
            keep = 2

        elif ctx.premium == checks.PremiumLevel.TWO:
            chatlog = min(chatlog, 100)
            hours = max(hours, 8)
            keep = 4

        elif ctx.premium == checks.PremiumLevel.THREE:
            chatlog = min(chatlog, 250)
            hours = max(hours, 4)
            keep = 8

        else:
            chatlog = min(chatlog, 0)
            hours = max(hours, 24)
            keep = 1

        now = datetime.utcnow()
        td = timedelta(hours=hours)
        await ctx.bot.db.intervals.update_one({"_id": ctx.guild_id}, {"$set": {
            "_id": ctx.guild_id,
            "last": now,
            "next": now,
            "keep": keep,
            "interval": hours,
            "chatlog": chatlog
        }}, upsert=True)

        raise ctx.f.SUCCESS("Successful **enabled the backup interval**.\nThe first backup will be created in "
                            f"`{utils.timedelta_to_string(td)}` "
                            f"at `{utils.datetime_to_string(datetime.utcnow() + td)} UTC`.")

    @interval.command(aliases=["disable"])
    @checks.is_premium()
    @wkr.cooldown(1, 10, bucket=wkr.CooldownType.GUILD)
    async def off(self, ctx):
        """
        Turn off automated backups


        __Examples__

        ```{b.prefix}backup interval off```
        """
        result = await ctx.bot.db.intervals.delete_one({"_id": ctx.guild_id})
        if result.deleted_count > 0:
            raise ctx.f.SUCCESS("Successfully **disabled the backup interval**.")

        else:
            raise ctx.f.ERROR(f"The backup interval is not enabled.")

    async def run_interval_backup(self, guild_id, keep=1, chatlog=0):
        guild = await self.bot.get_full_guild(guild_id)
        if guild is None:
            return

        existing = self.bot.db.backups.find({"data.id": guild_id, "interval": True},
                                            sort=[("timestamp", pymongo.DESCENDING)])
        counter = 0
        async for backup in existing:
            counter += 1
            if counter >= keep:
                await self.bot.db.backups.delete_one({"_id": backup["_id"]})

        backup = BackupSaver(self.bot, guild)
        await backup.save(chatlog=chatlog)

        await self.bot.db.backups.insert_one({
            "_id": utils.unique_id(),
            "creator": guild.owner_id,
            "timestamp": datetime.utcnow(),
            "interval": True,
            "data": backup.data
        })

    @wkr.Module.task(minutes=random.randint(5, 15))
    async def interval_task(self):
        to_backup = self.bot.db.intervals.find({"next": {"$lt": datetime.utcnow()}})
        async for interval in to_backup:
            guild_id = interval["_id"]
            self.bot.schedule(self.run_interval_backup(
                guild_id,
                keep=interval.get("keep", 1),
                chatlog=interval.get("chatlog", 0)
            ))
            await self.bot.db.intervals.update_one({"_id": guild_id}, {"$set": {
                "next": interval["next"] + timedelta(hours=interval["interval"])
            }})
