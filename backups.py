import traceback
import xenon_worker as wkr


class Options:
    def __init__(self, **default):
        self.all = False
        self.options = default

    def update(self, **options):
        for key, value in options.items():
            if key == "*":
                self.all = value
                self.options.clear()

            else:
                self.options[key] = value

    def __getattr__(self, item):
        return self.get(item)

    def get(self, item):
        if item in self.options.keys():
            return bool(self.options[item])

        return self.all


class BackupSaver:
    def __init__(self, client, guild):
        self.client = client
        self.guild = guild
        self.data = guild.to_dict()

    async def _save_roles(self):
        self.data["roles"] = [
            r.to_dict()
            for r in self.guild.roles
            if not r.managed
        ]

    async def _save_bans(self):
        self.data["bans"] = [
            {
                "reason": ban["reason"],
                "id": ban["user"]["id"]
            }
            for ban in await self.client.fetch_bans(self.guild)
        ]

    async def save(self, **options):
        savers = {
            "roles": self._save_roles,
            "bans": self._save_bans
        }

        for _, saver in savers.items():
            await saver()


class BackupLoader:
    def __init__(self, client, guild, data, reason="Backup loaded"):
        self.client = client
        self.guild = guild
        self.data = data

        self.options = Options(
            settings=True,
            roles=True,
            delete_roles=True,
            channels=True,
            delete_channels=True,
            bans=True
        )
        self.id_translator = {}
        self.reason = reason

    async def _load_settings(self):
        self.data.pop("guild_id", None)
        await self.client.edit_guild(self.guild, **self.data, reason=self.reason)

    async def _load_roles(self):
        bot_member = await self.client.get_bot_member(self.guild.id)
        top_role = list(sorted(bot_member.roles_from_guild(self.guild), key=lambda r: r.position))[-1]

        existing = sorted(
            [r for r in filter(
                lambda r: not r.managed and not r.is_default() and r.position < top_role.position,
                self.guild.roles
            )],
            key=lambda r: r.position,
            reverse=True
        )
        remaining = list(sorted(self.data["roles"], key=lambda r: r["position"], reverse=True))
        for role in remaining:
            role.pop("guild_id", None)

            # Default role (@everyone)
            if role["position"] == 0:
                to_edit = self.guild.default_role
                if to_edit is not None:
                    try:
                        await self.client.edit_role(to_edit, **role, reason=self.reason)
                        self.id_translator[role["id"]] = to_edit.id

                    except Exception:
                        traceback.print_exc()

                continue

            if len(existing) > 0:
                try:
                    to_edit = existing.pop(0)
                    await self.client.edit_role(to_edit, **role, reason=self.reason)
                    self.id_translator[role["id"]] = to_edit.id
                    continue
                except Exception:
                    traceback.print_exc()

            new = await self.client.create_role(self.guild, **role, reason=self.reason)
            self.id_translator[role["id"]] = new.id

        for role in existing:
            await self.client.delete_role(role, reason=self.reason)

    async def _delete_channels(self):
        for channel in self.guild.channels:
            try:
                await self.client.delete_channel(channel, reason=self.reason)
            except:
                traceback.print_exc()

    async def _load_channels(self):
        def _tune_channel(channel):
            channel.pop("guild_id", None)

            # Bitrates over 96000 require special features or boosts
            # (boost advantages change a lot, so we just ignore them)
            if "bitrate" in channel.keys() and "VIP_REGIONS" not in self.guild.features:
                channel["bitrate"] = min(channel["bitrate"], 96000)

            # News and store channels require special features
            if (channel["type"] == wkr.ChannelType.GUILD_NEWS and "NEWS" not in self.guild.features) or \
                    (channel["type"] == wkr.ChannelType.GUILD_STORE and "COMMERCE" not in self.guild.features):
                channel["type"] = 0

            channel["type"] = 0 if channel["type"] > 4 else channel["type"]

            if "parent_id" in channel.keys():
                channel["parent_id"] = self.id_translator.get(channel["parent_id"], channel["parent_id"])

            overwrites = channel.get("permission_overwrites", [])
            for overwrite in overwrites:
                overwrite["id"] = self.id_translator.get(overwrite["id"], overwrite["id"])

            return channel

        no_parent = sorted(
            filter(lambda c: c.get("parent_id") is None, self.data["channels"]),
            key=lambda c: c.get("position")
        )
        for channel in no_parent:
            new = await self.client.create_channel(self.guild, **_tune_channel(channel), reason=self.reason)
            self.id_translator[channel["id"]] = new.id

        has_parent = sorted(
            filter(lambda c: c.get("parent_id") is not None, self.data["channels"]),
            key=lambda c: c["position"]
        )
        for channel in has_parent:
            new = await self.client.create_channel(self.guild, **_tune_channel(channel), reason=self.reason)
            self.id_translator[channel["id"]] = new.id

    async def _load_bans(self):
        for ban in self.data.get("bans", []):
            try:
                await self.client.ban_user(self.guild, wkr.Snowflake(ban["id"]), reason=ban["reason"])
            except Exception:
                pass

    async def load(self, **options):
        self.options.update(**options)
        loaders = (
            ("settings", self._load_settings),
            ("roles", self._load_roles),
            ("delete_channels", self._delete_channels),
            ("channels", self._load_channels),
            ("bans", self._load_bans)
        )

        for key, loader in loaders:
            if self.options.get(key):
                try:
                    await loader()
                except:
                    traceback.print_exc()