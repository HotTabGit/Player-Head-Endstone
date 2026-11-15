import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time

from endstone import Player
from endstone.block import BlockFace
from endstone.event import (
    ActorExplodeEvent, BlockBreakEvent, BlockExplodeEvent, BlockPlaceEvent,
    BlockPistonExtendEvent, BlockPistonRetractEvent, EventPriority,
    PacketSendEvent, PlayerDeathEvent, PlayerQuitEvent, PlayerJoinEvent, PlayerSkinChangeEvent, event_handler,
)
from endstone.inventory import ItemStack
from endstone.level import Location
from endstone.permissions import PermissionDefault
from endstone.plugin import Plugin

from .config import ConfigError, Settings, load_settings
from .live_skin import BLOCK_ACTOR_DATA, PLAYER_LIST, PROTOCOL, LiveSkins, skull_rotation, wire_uuid
from .model import NBT_KEY, HeadData, make_head_data, make_head_tag, read_head_nbt, head_display_name, head_lore, should_drop
from .store import MAX_RECORDS, HeadStoreError, NbtHeadStore
from .skins import LIVE_HEAD, SkinCatalogue, custom_type, is_custom_type, capture_failure, write_atomic, json_bytes
from .worn_skin import WornSkins


_FACES = {
    BlockFace.DOWN: (0, -1, 0), BlockFace.UP: (0, 1, 0),
    BlockFace.NORTH: (0, 0, -1), BlockFace.SOUTH: (0, 0, 1),
    BlockFace.WEST: (-1, 0, 0), BlockFace.EAST: (1, 0, 0),
}
_OFFSETS = tuple(_FACES.values())
_SUPPORT_OFFSETS = {0: (0, 1, 0), 1: (0, -1, 0), 2: (0, 0, 1),
                    3: (0, 0, -1), 4: (1, 0, 0), 5: (-1, 0, 0)}
_HEAD_BLOCKS = frozenset({"minecraft:player_head", "minecraft:skull"})


def _support_offset(states):
    facing = states.get('facing_direction', {'down': 0, 'up': 1, 'north': 2,
        'south': 3, 'west': 4, 'east': 5}.get(states.get('minecraft:block_face'), 1))
    return _SUPPORT_OFFSETS.get(facing)


def _head_kind(kind):
    return kind in _HEAD_BLOCKS or kind == LIVE_HEAD or is_custom_type(kind)


def _type_name(value) -> str:
    kind = value.type
    return kind if isinstance(kind, str) else kind.id


def _position(block) -> tuple[str, int, int, int]:
    return (block.dimension.name, block.x, block.y, block.z)


def _near(position, offset, distance=1):
    dimension, x, y, z = position
    return (dimension, x + offset[0] * distance, y + offset[1] * distance,
            z + offset[2] * distance)


class PlayerHeadsPlugin(Plugin):
    api_version = "0.11"
    description = "Головы игроков за убийства, с сохранением имени через NBT"
    commands = {
        "heads": {
            "description": "Выдать голову игрока, посмотреть состояние или перечитать конфиг",
            "usages": ["/heads [status|reload]", "/heads give [player: player]"],
            "permissions": ["player_heads.admin"],
        },
    }
    permissions = {
        "player_heads.admin": {"description": "Управление головами игроков",
                               "default": PermissionDefault.OP},
    }

    def __init__(self):
        super().__init__()
        self.ready = False
        self._settings = Settings()
        self._live_skins = LiveSkins()
        self._worn_skins = WornSkins()
        self._key = None
        self._store = None
        self._undelivered = None
        self._main_thread = None
        self._tasks = []
        self._pending_placements = {}
        self._pending_explosions = {}
        self._placement_approvals = {}
        self._reservations = {}
        self._reserved_bytes = 0
        self._reserved_count = 0
        self._cooldowns = {}
        self._notices = {}
        self._tick = 0
        self._maintenance_iterator = None
        self._last_error = -60.0
        self._drops = 0
        self._skins = None
        self._skin_types = frozenset()
        self._live_item_registered = False
        self._skin_diagnostics = {'attempts': 0, 'successes': 0, 'failures': {}}
        self._skin_diag_dirty = False
        self._skin_diag_last_write = -60.0
        self._skin_retries = {}
        self._skin_last_retryable = False

    def on_load(self):
        try:
            folder = Path(self.data_folder).absolute()
            if folder.parent.name != 'plugins':
                raise ValueError('unexpected_player_heads_folder')
            self._skins = SkinCatalogue(folder)
            self._skin_pack_version = self._skins.build_packs(folder.parent.parent)
        except Exception as exc:
            self._skins = None
            self.logger.warning('PlayerHeads skins: preparation failed (' + type(exc).__name__ + '); signed heads remain available.')

    def _capture_skin(self, player, head_id=None, skin=None, context='other'):
        self._skin_last_retryable = False
        if self._skins is None:
            return False
        image = None
        try:
            if not self.server.online_mode:
                return False
            self._skin_diagnostics['attempts'] += 1
            current = player.skin if skin is None else skin
            image = current.image
            self._skins.capture(str(player.xuid), image, head_id)
            self._skin_diagnostics['successes'] += 1
            self._skin_diag_dirty = True
            return True
        except Exception as error:
            detail = capture_failure(error, image, context)
            signature = json.dumps(detail, sort_keys=True)
            records = self._skin_diagnostics['failures']
            if signature in records:
                records[signature]['count'] += 1
            elif len(records) < 32:
                records[signature] = {**detail, 'count': 1}
            self._skin_diag_dirty = True
            self._skin_last_retryable = (detail['shape'] is None or 0 in detail['shape']
                                         or isinstance(error, (OSError, RuntimeError)))
            self._warn('скин не сохранён: ' + json.dumps(detail, ensure_ascii=True, sort_keys=True))
            self._flush_skin_diagnostics()
            return False

    def _flush_skin_diagnostics(self, force=False):
        if not self._skin_diag_dirty:
            return
        now = time.monotonic()
        if not force and now - self._skin_diag_last_write < 60:
            return
        self._skin_diag_last_write = now
        try:
            write_atomic(Path(self.data_folder) / 'skin-diagnostics.json', json_bytes(self._skin_diagnostics))
            self._skin_diag_dirty = False
        except Exception:
            pass

    def _queue_skin_retry(self, player):
        if len(self._skin_retries) >= 128:
            return
        now = time.monotonic()
        try:
            key = (str(player.unique_id), int(player.runtime_id))
        except Exception:
            return
        self._skin_retries[key] = {'xuid': str(player.xuid), 'after': now + 2,
                                   'deadline': now + 20, 'attempts': 0}

    def _retry_skin_captures(self):
        if not self._skin_retries:
            return
        now = time.monotonic()
        players = {}
        for player in self.server.online_players:
            try:
                players[(str(player.unique_id), int(player.runtime_id))] = player
            except Exception:
                continue
        budget = 4
        for key, request in tuple(self._skin_retries.items()):
            player = players.get(key)
            if player is None or str(player.xuid) != request['xuid'] or now >= request['deadline']:
                self._skin_retries.pop(key, None)
                continue
            if now < request['after'] or budget <= 0:
                continue
            budget -= 1
            request['attempts'] += 1
            if self._capture_skin(player, context='join_retry') or not self._skin_last_retryable or request['attempts'] >= 3:
                self._skin_retries.pop(key, None)
            else:
                request['after'] = now + 4

    def _skin_type(self, data):
        if self._skins is None:
            return None
        key = self._skins.key_for(data)
        kind = custom_type(key) if key is not None else None
        return kind if kind in self._skin_types else None

    def _upgrade_inventory_heads(self):
        if not self._skin_types and not self._live_item_registered:
            return
        players = list(self.server.online_players)
        if not players:
            return
        player = players[(self._tick // 100) % len(players)]
        try:
            from endstone_inventoryui.manager.player_manager import find_session
        except ImportError:
            find_session = lambda value: None
        if find_session(player) is not None:
            return
        inventory = player.inventory
        def replacement_for(item):
            data = self._item_data(item)
            if data is None or item.amount != 1:
                return None
            kind = _type_name(item)
            target = self._skin_type(data)
            if kind == LIVE_HEAD:
                target = target or self._settings.item_type
            if target is None or target == kind:
                return None
            replacement = ItemStack(target, item.amount, self._settings.item_data if target == self._settings.item_type else 0)
            replacement.nbt = item.nbt
            if not replacement.set_item_meta(item.item_meta):
                raise RuntimeError('skin_upgrade_metadata_rejected')
            if self._item_data(replacement) != data:
                raise RuntimeError('skin_upgrade_identity_changed')
            return replacement

        for slot in range(min(inventory.size, 64)):
            replacement = replacement_for(inventory.get_item(slot))
            if replacement is not None:
                inventory.set_item(slot, replacement)
        for equipment_slot in ('helmet', 'item_in_off_hand'):
            item = getattr(inventory, equipment_slot, None)
            if item is not None and _type_name(item) == LIVE_HEAD:
                replacement = replacement_for(item)
                if replacement is not None:
                    setattr(inventory, equipment_slot, replacement)

    def _upgrade_placed_head(self, block, data):
        if self._settings.live_placed_skins and getattr(self.server, "protocol_version", None) == PROTOCOL:
            return
        target = self._skin_type(data)
        if target is None or _type_name(block) == target:
            return
        old = block.capture_state()
        old_states = block.data.block_states
        face = old_states.get('minecraft:block_face')
        if face is None:
            face = {0: 'down', 1: 'up', 2: 'north', 3: 'south', 4: 'west', 5: 'east'}.get(old_states.get('facing_direction', 1), 'up')
        facing = old_states.get('minecraft:cardinal_direction', {2: 'north', 3: 'south', 4: 'west', 5: 'east'}.get(old_states.get('facing_direction', 1), 'north'))
        try:
            block.set_data(self.server.create_block_data(target, {'minecraft:block_face': face,
                            'minecraft:cardinal_direction': facing}), apply_physics=False)
            if _type_name(block) != target:
                raise RuntimeError('skin_block_conversion_rejected')
        except Exception:
            if not old.update(force=True, apply_physics=False):
                self.ready = False
            raise

    @event_handler(priority=EventPriority.MONITOR)
    def on_skin_join(self, event: PlayerJoinEvent):
        if self.ready and self._main():
            if not self._capture_skin(event.player, context='join') and self._skin_last_retryable:
                self._queue_skin_retry(event.player)

    @event_handler(priority=EventPriority.MONITOR, ignore_cancelled=True)
    def on_skin_change(self, event: PlayerSkinChangeEvent):
        if self.ready and self._main() and not event.is_cancelled:
            self._capture_skin(event.player, skin=event.new_skin, context='skin_change')
            if getattr(self.server, "protocol_version", None) == PROTOCOL:
                self._worn_skins.skin_changed(event.player)

    def _main(self):
        return threading.get_ident() == self._main_thread

    def _read_key(self, folder: Path) -> bytes:
        path = folder / "signing.key"
        marker = folder / "key.required"
        if path.is_symlink():
            raise ValueError("signing_key_symlink")
        if not path.exists():
            if marker.exists() or any(folder.glob("placed-*.nbt")):
                raise ValueError("signing_key_missing_for_existing_store")
            key = secrets.token_bytes(32)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    target.write(key)
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                raise
        if path.stat().st_size != 32:
            raise ValueError("invalid_signing_key_size")
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("invalid_signing_key_size")
        os.chmod(path, 0o600)
        if not marker.exists():
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(b"player-heads-key-required-v1\n")
                target.flush()
                os.fsync(target.fileno())
        return key

    @staticmethod
    def _validate_item(settings):
        allowed = ((settings.item_type == "minecraft:player_head" and settings.item_data == 0)
                   or (settings.item_type == "minecraft:skull" and settings.item_data == 3))
        if not allowed:
            raise ConfigError("Для головы игрока укажи minecraft:player_head и item_data = 0.")
        ItemStack(settings.item_type, 1, settings.item_data)

    def on_enable(self):
        self._main_thread = threading.get_ident()
        self.save_default_config()
        try:
            folder = Path(self.data_folder)
            folder.mkdir(parents=True, exist_ok=True)
            self._settings = load_settings(folder / "config.toml")
            self._validate_item(self._settings)
            self._key = self._read_key(folder)
            world_name = self.server.level.name
            world_hash = hashlib.sha256(world_name.encode("utf-8")).hexdigest()[:24]
            store = NbtHeadStore(folder / f"placed-{world_hash}.nbt", world_name, self._key)
            undelivered = NbtHeadStore(folder / f"undelivered-{world_hash}.nbt", world_name, self._key)
            self._store, self._undelivered = store, undelivered
            self.ready = True
            if self._skins is not None:
                try:
                    from endstone.inventory import ItemType
                    registry = self.server.get_registry(ItemType)
                    self._live_item_registered = registry.get(LIVE_HEAD) is not None
                    self._skin_types = frozenset(custom_type(key) for key in self._skins.startup_keys
                                                 if registry.get(custom_type(key)) is not None)
                except Exception as exc:
                    self._skin_types = frozenset()
                    self.logger.warning(f"PlayerHeads: скины голов недоступны ({type(exc).__name__}); обычные головы работают.")
        except Exception as exc:
            self.logger.error(f"PlayerHeads отключён: конфиг, ключ или NBT-хранилище не прошли проверку ({type(exc).__name__}).")
        self.register_events(self)
        self._tasks.append(self.server.scheduler.run_task(self, self._maintenance, delay=1, period=20))
        if self.ready:
            self.logger.info(f"PlayerHeads ready; PvP={self._settings.pvp_only}, chance={self._settings.drop_chance_percent:g}%, placed={self._store.count}.")
            if self._skins is not None:
                self.logger.info(f'PlayerHeads: загружено скинов голов: {len(self._skin_types)}; новые появятся после перезапуска.')

    def on_disable(self):
        self._refresh_live_skins(clear=True)
        for position, pending in tuple(self._pending_explosions.items()):
            self._finish_exploded_head(position, pending, retry=False)
        self.ready = False
        self._flush_skin_diagnostics(force=True)
        self._skin_retries.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._pending_placements.clear()
        self._placement_approvals.clear()
        self._reservations.clear()
        self._reserved_bytes = self._reserved_count = 0
        if self._store is not None:
            try:
                self._store.flush()
            except Exception as exc:
                self.logger.error(f"PlayerHeads: не удалось сохранить NBT-хранилище ({type(exc).__name__}).")
        if self._undelivered is not None:
            try:
                self._undelivered.flush()
            except Exception as exc:
                self.logger.error(f"PlayerHeads: не удалось сохранить очередь выдачи ({type(exc).__name__}).")
        self._cooldowns.clear()
        self._notices.clear()
        self._maintenance_iterator = None

    def _warn(self, reason):
        now = time.monotonic()
        if now - self._last_error >= 10:
            self._last_error = now
            self.logger.warning(f"PlayerHeads: {reason}.")

    def _notice(self, player, message):
        key = str(player.xuid)
        now = time.monotonic()
        if now - self._notices.get(key, -60.0) >= 2:
            self._notices[key] = now
            player.send_message(message)

    def make_item(self, data: HeadData) -> ItemStack:
        if self._key is None:
            raise RuntimeError("signing_key_unavailable")
        skin_type = self._skin_type(data)
        item = ItemStack(skin_type or self._settings.item_type, 1, 0 if skin_type else self._settings.item_data)
        nbt = item.nbt
        nbt[NBT_KEY] = make_head_tag(data, self._key)
        item.nbt = nbt
        meta = item.item_meta
        meta.display_name = head_display_name(data)
        meta.lore = head_lore(data)
        if not item.set_item_meta(meta):
            raise RuntimeError("item_meta_rejected")
        return item

    def _item_data(self, item):
        if item is None or self._key is None or not _head_kind(_type_name(item)):
            return None
        return read_head_nbt(item.nbt, self._key)

    @staticmethod
    def _spawn_valid(spawned):
        if spawned is None:
            return False
        return bool(getattr(spawned, "is_valid", True))

    @event_handler(priority=EventPriority.MONITOR)
    def on_death(self, event: PlayerDeathEvent):
        if not self.ready or not self._main() or not self._settings.enabled:
            return
        try:
            victim = event.player
            source = event.damage_source
            killer = source.actor if source is not None else None
            killer_is_player = isinstance(killer, Player) and str(killer.xuid) != str(victim.xuid)
            if self._settings.pvp_only and not killer_is_player:
                return
            pair = (str(killer.xuid) if killer_is_player else "", str(victim.xuid))
            now = time.monotonic()
            if now - self._cooldowns.get(pair, -1e15) < self._settings.pair_cooldown_seconds:
                return
            if not should_drop(self._settings.drop_chance_percent):
                return
            data = make_head_data(victim.xuid, victim.name,
                                  killer.xuid if killer_is_player else "",
                                  killer.name if killer_is_player else "")
            self._capture_skin(victim, data.head_id, context='death')
            drop = victim.dimension.drop_item(victim.location, self.make_item(data))
            if not self._spawn_valid(drop):
                raise RuntimeError("head_drop_rejected")
            self._cooldowns[pair] = now
            self._drops += 1
        except Exception:
            self._warn("не удалось выдать голову после смерти")

    def _release_reservation(self, position, token):
        reservation = self._reservations.get(position)
        if reservation is not None and reservation[0] is token:
            _, byte_count, new_record = self._reservations.pop(position)
            self._reserved_bytes -= byte_count
            self._reserved_count -= int(new_record)
        approval = self._placement_approvals.get(position)
        if approval is not None and approval[0] is token:
            self._placement_approvals.pop(position, None)

    @event_handler(priority=EventPriority.HIGHEST, ignore_cancelled=True)
    def on_place_guard(self, event: BlockPlaceEvent):
        if not self._main() or event.is_cancelled:
            return
        position = _position(event.block)
        if position in self._pending_explosions:
            event.cancel()
            self._notice(event.player, "§eПодожди: здесь ещё завершается взрыв.")
            return
        try:
            kind = _type_name(event.block_placed_state)
            if not _head_kind(kind):
                return
            item = event.player.inventory.item_in_main_hand
            data = self._item_data(item)
            if data is None:
                previous = self._placement_approvals.get(position)
                if previous is not None:
                    self._release_reservation(position, previous[0])
                    self._pending_placements.pop(position, None)
                if not self.ready and item is not None and NBT_KEY in item.nbt:
                    event.cancel()
                    self._notice(event.player, "§cУстановка голов временно недоступна: администратору нужно проверить NBT-хранилище.")
                return
            if not self.ready or self._store is None:
                event.cancel()
                self._notice(event.player, "§cУстановка голов временно недоступна: администратору нужно проверить NBT-хранилище.")
                return
            self._store.can_set(position, data)
            old = self._store.get(position)
            new_record = old is None
            byte_count = max(0, self._store.entry_size(position, data)
                             - self._store.existing_entry_size(position))
            previous = self._reservations.get(position)
            old_bytes = previous[1] if previous is not None else 0
            old_count = int(previous[2]) if previous is not None else 0
            if (self._store.count + self._reserved_count - old_count + int(new_record) > MAX_RECORDS
                    or self._reserved_bytes - old_bytes + byte_count > self._store.remaining_bytes):
                event.cancel()
                self._notice(event.player, "§cХранилище установленных голов заполнено. Сначала сними старые головы.")
                return
            if previous is not None:
                self._release_reservation(position, previous[0])
            token = object()
            self._reservations[position] = (token, byte_count, new_record)
            self._reserved_bytes += byte_count
            self._reserved_count += int(new_record)
            self._placement_approvals[position] = (token, data, str(event.player.xuid))

            def release_cancelled():
                if self._pending_placements.get(position) is not token:
                    self._release_reservation(position, token)

            self.server.scheduler.run_task(self, release_cancelled, delay=1)
        except HeadStoreError as exc:
            event.cancel()
            message = ("§cХранилище установленных голов заполнено. Сначала сними старые головы."
                       if exc.args and exc.args[0] in ("too_many_records", "store_file_too_large")
                       else "§cНе удалось проверить NBT-хранилище. Голова осталась в инвентаре.")
            self._notice(event.player, message)
            self._warn("установка головы отменена: не пройдена проверка NBT-хранилища")
        except Exception:
            event.cancel()
            self._notice(event.player, "§cНе удалось проверить NBT-хранилище. Голова осталась в инвентаре.")
            self._warn("установка головы отменена: не пройдена проверка NBT-хранилища")

    @event_handler(priority=EventPriority.MONITOR, ignore_cancelled=True)
    def on_place(self, event: BlockPlaceEvent):
        if not self.ready or not self._main() or event.is_cancelled:
            return
        position = _position(event.block)
        try:
            kind = _type_name(event.block_placed_state)
            approval = self._placement_approvals.get(position)
            observed = self._item_data(event.player.inventory.item_in_main_hand) if _head_kind(kind) else None
            if _head_kind(kind) and (approval is None or observed is None
                                        or approval[1].head_id != observed.head_id
                                        or approval[2] != str(event.player.xuid)):
                self.on_place_guard(event)
                if event.is_cancelled:
                    return
                approval = self._placement_approvals.get(position)
            data = approval[1] if approval is not None and approval[2] == str(event.player.xuid) else None
            if data is None and self._store.get(position) is None:
                return
            token = approval[0] if approval is not None else object()
            self._pending_placements[position] = token
            dimension = event.block.dimension
            yaw = getattr(event.player.location, "yaw", 0.0)
            rotation = (math.floor(yaw / 22.5 + .5) % 16) * 22.5

            def finish():
                if not self.ready or self._pending_placements.get(position) is not token:
                    self._release_reservation(position, token)
                    return
                self._pending_placements.pop(position, None)
                try:
                    block = dimension.get_block_at(*position[1:])
                    if data is not None and _head_kind(_type_name(block)):
                        self._store.set(position, data)
                        previous = self._live_skins.rotations.get(position)
                        if previous is None or previous[0] != data.head_id:
                            self._live_skins.rotations[position] = (data.head_id, rotation)
                    elif self._store.get(position) is not None:
                        self._store.pop(position)
                except Exception:
                    self.ready = False
                    self._warn("не удалось зарегистрировать установленную голову")
                finally:
                    self._release_reservation(position, token)

            self.server.scheduler.run_task(self, finish, delay=1)
        except Exception:
            token = self._pending_placements.pop(position, None)
            if token is not None:
                self._release_reservation(position, token)
            self._warn("не удалось проверить установку головы")

    def _loaded(self, dimension):
        return {(chunk.x, chunk.z) for chunk in dimension.loaded_chunks}

    def _head_attached_to(self, dimension, support_position, loaded=None):
        if self._store is None:
            return False
        for offset in _OFFSETS:
            head_position = _near(support_position, offset)
            if self._store.get(head_position) is None:
                continue
            if loaded is None:
                loaded = self._loaded(dimension)
            if (head_position[1] >> 4, head_position[3] >> 4) not in loaded:
                return True
            block = dimension.get_block_at(*head_position[1:])
            if not _head_kind(_type_name(block)):
                continue
            states = block.data.block_states
            support_offset = _support_offset(states)
            if support_offset is None or _near(head_position, support_offset) == support_position:
                return True
        return False

    @event_handler(priority=EventPriority.HIGHEST, ignore_cancelled=True)
    def on_break(self, event: BlockBreakEvent):
        if not self._main() or event.is_cancelled or self._store is None:
            return
        position = _position(event.block)
        if position in self._pending_explosions:
            event.cancel()
            return
        data = self._store.get(position)
        if data is None:
            try:
                if self._head_attached_to(event.block.dimension, position):
                    event.cancel()
                    self._notice(event.player, "§eСначала сними голову, установленную на этом блоке.")
            except Exception:
                self._warn("не удалось проверить опору головы")
            return
        if not _head_kind(_type_name(event.block)):
            self._store.pop(position)
            return
        event.cancel()
        if not self.ready:
            self._notice(event.player, "§cГоловы временно недоступны: администратору нужно проверить NBT-хранилище.")
            return
        spawned = None
        try:
            block = event.block
            location = Location(block.dimension, block.x + .5, block.y + .5, block.z + .5)
            spawned = block.dimension.drop_item(location, self.make_item(data))
            if not self._spawn_valid(spawned):
                raise RuntimeError("head_drop_rejected")
            block.set_type("minecraft:air", apply_physics=False)
            if _type_name(block) != "minecraft:air":
                raise RuntimeError("head_removal_rejected")
            self._store.pop(position)
        except Exception:
            if spawned is not None and _type_name(event.block) == "minecraft:air":
                try:
                    self._store.pop(position)
                except Exception:
                    self.ready = False
                    self._warn("снятая голова требует проверки NBT-хранилища")
                return
            if spawned is not None and self._spawn_valid(spawned):
                try:
                    spawned.remove()
                except Exception:
                    self.ready = False
                    self._warn("восстановление головы требует проверки администратора")
            self._notice(event.player, "§cНе удалось снять голову. Повтори попытку; если ошибка осталась — сообщи администратору.")
            self._warn("не удалось снять установленную голову")

    def _piston(self, event, extending):
        if not self._main() or event.is_cancelled or self._store is None or self._store.count == 0:
            return
        try:
            facing = _FACES[event.direction]
            if not extending:
                facing = tuple(-value for value in facing)
            origin, dimension = _position(event.block), event.block.dimension
            loaded = self._loaded(dimension)
            for distance in range(1, 14):
                position = _near(origin, facing, distance)
                if self._store.get(position) is not None or self._head_attached_to(dimension, position, loaded):
                    event.cancel()
                    return
        except Exception:
            event.cancel()
            self._warn("движение поршня рядом с головой отменено из-за ошибки проверки")

    @event_handler(priority=EventPriority.HIGHEST, ignore_cancelled=True)
    def on_extend(self, event: BlockPistonExtendEvent):
        self._piston(event, True)

    @event_handler(priority=EventPriority.HIGHEST, ignore_cancelled=True)
    def on_retract(self, event: BlockPistonRetractEvent):
        self._piston(event, False)

    def _restore_exploded_head(self, position, pending):
        dimension, data, _item, state = pending
        if self._pending_explosions.get(position) is not pending:
            return
        if self._store.get(position) != data:
            self._pending_explosions.pop(position, None)
            return
        block = dimension.get_block_at(*position[1:])
        if state is None:
            return
        if _type_name(block) == "minecraft:air":
            if not state.update(force=True, apply_physics=False):
                raise RuntimeError("head_restore_rejected")
        if _head_kind(_type_name(block)):
            self._pending_explosions.pop(position, None)

    def _journal_delivery(self, position, data):
        self._store.flush()
        self._undelivered.set(position, data)
        self._undelivered.flush()

    def _recover_undelivered(self):
        loaded = {}
        for position, data in self._undelivered.items():
            if position in self._pending_explosions:
                continue
            if self._store.get(position) != data:
                self._undelivered.pop(position)
                continue
            dimension = self.server.level.get_dimension(position[0])
            if dimension is None:
                continue
            if position[0] not in loaded:
                loaded[position[0]] = self._loaded(dimension)
            if (position[1] >> 4, position[3] >> 4) not in loaded[position[0]]:
                continue
            block = dimension.get_block_at(*position[1:])
            if _head_kind(_type_name(block)):
                self._undelivered.pop(position)
            else:
                self._pending_explosions[position] = (dimension, data, self.make_item(data), None)
        self._undelivered.flush()

    def _finish_exploded_head(self, position, pending, retry=True):
        if self._pending_explosions.get(position) is not pending:
            return
        dimension, data, item, state = pending
        try:
            if self._store.get(position) != data:
                self._pending_explosions.pop(position, None)
                return
            block = dimension.get_block_at(*position[1:])
            if _head_kind(_type_name(block)):
                self._pending_explosions.pop(position, None)
                return
            if not self.ready:
                self._restore_exploded_head(position, pending)
                return
            drop = dimension.drop_item(Location(dimension, position[1] + .5,
                                                position[2] + .5, position[3] + .5), item)
            if not self._spawn_valid(drop):
                raise RuntimeError("head_drop_rejected")
            self._pending_explosions.pop(position, None)
            self._store.pop(position)
            self._undelivered.pop(position)
            self._undelivered.flush()
        except Exception:
            if self._pending_explosions.get(position) is not pending:
                self.ready = False
                self._warn("предмет головы выдан, но NBT-хранилище требует проверки")
                return
            try:
                support = (_support_offset(state.data.block_states)
                           if state is not None else None)
                if state is None or (support is not None and _type_name(dimension.get_block_at(*_near(position, support)[1:])) == "minecraft:air"):
                    self._journal_delivery(position, data)
                    if retry:
                        self.server.scheduler.run_task(self, lambda: self._finish_exploded_head(
                            position, pending, retry=False), delay=1)
                else:
                    self._restore_exploded_head(position, pending)
            except Exception:
                self.ready = False
            self._warn("не удалось выдать голову после взрыва; сохранённая запись требует проверки")

    def _explosion_data(self, position):
        approval = self._placement_approvals.get(position)
        if approval is not None and self._pending_placements.get(position) is approval[0]:
            return approval[1]
        return self._store.get(position)

    def _explosion(self, event):
        if (not self._main() or event.is_cancelled or self._store is None
                or (self._store.count == 0 and not self._pending_placements)):
            return
        prepared = {}
        try:
            affected = {}
            for block in event.block_list:
                affected[_position(block)] = block.dimension
            loaded_by_dimension = {}
            candidates = dict(affected)
            for position, dimension in affected.items():
                if dimension.name not in loaded_by_dimension:
                    loaded_by_dimension[dimension.name] = self._loaded(dimension)
                for offset in _OFFSETS:
                    neighbour = _near(position, offset)
                    if neighbour in candidates or self._explosion_data(neighbour) is None:
                        continue
                    if (neighbour[1] >> 4, neighbour[3] >> 4) not in loaded_by_dimension[dimension.name]:
                        continue
                    head = dimension.get_block_at(*neighbour[1:])
                    if not _head_kind(_type_name(head)):
                        continue
                    support = _support_offset(head.data.block_states)
                    if support is None or _near(neighbour, support) == position:
                        candidates[neighbour] = dimension
            for position, dimension in candidates.items():
                data = self._explosion_data(position)
                if data is None or position in self._pending_explosions:
                    continue
                head = dimension.get_block_at(*position[1:])
                if not _head_kind(_type_name(head)):
                    continue
                if not self.ready:
                    event.cancel()
                    return
                prepared[position] = (dimension, data, self.make_item(data), head.capture_state())
            if not prepared:
                return
            def finish():
                for position, pending in prepared.items():
                    self._finish_exploded_head(position, pending)
            self.server.scheduler.run_task(self, finish, delay=1)
            for position, pending in prepared.items():
                self._store.set(position, pending[1])
                token = self._pending_placements.pop(position, None)
                if token is not None:
                    self._release_reservation(position, token)
                self._pending_explosions[position] = pending
                head = pending[0].get_block_at(*position[1:])
                try:
                    head.set_type("minecraft:air", apply_physics=False)
                except Exception:
                    if _type_name(head) != "minecraft:air":
                        raise
                if _type_name(head) != "minecraft:air":
                    raise RuntimeError("head_removal_rejected")
        except Exception:
            event.cancel()
            for position, pending in prepared.items():
                try:
                    self._restore_exploded_head(position, pending)
                except Exception:
                    self.ready = False
            self._warn("взрыв отменён: не удалось безопасно подготовить выпадение голов")

    @event_handler(priority=EventPriority.MONITOR, ignore_cancelled=True)
    def on_actor_explode(self, event: ActorExplodeEvent):
        self._explosion(event)

    @event_handler(priority=EventPriority.MONITOR, ignore_cancelled=True)
    def on_block_explode(self, event: BlockExplodeEvent):
        self._explosion(event)

    def _refresh_live_skins(self, clear=False):
        try:
            if self._live_skins.visible and (clear or not self.ready or not self._settings.live_placed_skins):
                for player in self.server.online_players:
                    self._live_skins.clear(player)
            elif not clear and self.ready and self._settings.live_placed_skins and self._skins is not None:
                self._live_skins.update(self, _HEAD_BLOCKS, _type_name)
        except Exception:
            self._warn("не удалось обновить визуальные скины голов")
        if getattr(self.server, "protocol_version", None) == PROTOCOL:
            try:
                self._worn_skins.update(self, not clear and self.ready and self._skins is not None
                                        and self._settings.live_placed_skins)
            except (AttributeError, OSError, RuntimeError, ValueError):
                self._warn("не удалось обновить скин надетой головы")

    @event_handler(priority=EventPriority.MONITOR, ignore_cancelled=True)
    def on_head_packet(self, event: PacketSendEvent):
        if event.packet_id not in (BLOCK_ACTOR_DATA, PLAYER_LIST) or not self._main() or not self.ready:
            return
        if self.server.protocol_version != PROTOCOL or event.player is None:
            return
        try:
            if event.packet_id == PLAYER_LIST:
                self._worn_skins.observe(wire_uuid(event.player.unique_id), event.payload)
                return
            result = skull_rotation(event.payload)
            if result is None:
                return
            coordinates, rotation = result
            position = (event.player.dimension.name, *coordinates)
            approval = self._placement_approvals.get(position)
            data = approval[1] if approval is not None else self._store.get(position)
            if data is not None:
                self._live_skins.rotations[position] = (data.head_id, rotation)
        except (RuntimeError, ValueError):
            return

    def _maintenance(self):
        if not self._main() or self._store is None:
            return
        self._tick += 20
        self._flush_skin_diagnostics()
        try:
            if self.ready:
                self._retry_skin_captures()
                self._recover_undelivered()
                for position, pending in tuple(self._pending_explosions.items()):
                    self._finish_exploded_head(position, pending, retry=False)
            if self._tick % 100 == 0 and self.ready:
                self._upgrade_inventory_heads()
                if self._maintenance_iterator is None:
                    self._maintenance_iterator = iter(self._store.items())
                loaded_by_dimension = {}
                for _ in range(32):
                    try:
                        position, _data = next(self._maintenance_iterator)
                    except StopIteration:
                        self._maintenance_iterator = None
                        break
                    except RuntimeError:
                        self._maintenance_iterator = None
                        break
                    if position in self._pending_explosions:
                        continue
                    dimension = self.server.level.get_dimension(position[0])
                    if dimension is None:
                        continue
                    if position[0] not in loaded_by_dimension:
                        loaded_by_dimension[position[0]] = self._loaded(dimension)
                    if (position[1] >> 4, position[3] >> 4) not in loaded_by_dimension[position[0]]:
                        continue
                    block = dimension.get_block_at(*position[1:])
                    if not _head_kind(_type_name(block)):
                        self._store.pop(position)
                    else:
                        self._upgrade_placed_head(block, _data)
                now = time.monotonic()
                ttl = max(300, self._settings.pair_cooldown_seconds)
                self._cooldowns = {pair: timestamp for pair, timestamp in self._cooldowns.items()
                                   if now - timestamp < ttl}
            self._store.flush()
        except Exception:
            self.ready = False
            self._warn("работа приостановлена: не удалось сохранить или проверить NBT-хранилище")
        self._refresh_live_skins()

    @event_handler
    def on_quit(self, event: PlayerQuitEvent):
        self._live_skins.forget(event.player)
        if getattr(self.server, "protocol_version", None) == PROTOCOL:
            self._worn_skins.forget(event.player)
        self._notices.pop(str(event.player.xuid), None)
        for key, request in tuple(self._skin_retries.items()):
            if request['xuid'] == str(event.player.xuid):
                self._skin_retries.pop(key, None)

    def _give(self, sender, victim):
        receiver = sender if isinstance(sender, Player) else victim
        data = make_head_data(victim.xuid, victim.name)
        self._capture_skin(victim, data.head_id, context='give')
        item = self.make_item(data)
        leftovers = receiver.inventory.add_item(item)
        for remaining in leftovers.values():
            drop = receiver.dimension.drop_item(receiver.location, remaining)
            if not self._spawn_valid(drop):
                sender.send_message("§cИнвентарь заполнен, и сервер не смог положить голову рядом.")
                return
        sender.send_message(f"§aВыдана голова {data.victim_name}." + (" Предмет выпал рядом." if leftovers else ""))

    def on_command(self, sender, command, args):
        if command.name != "heads":
            return False
        if not sender.has_permission("player_heads.admin"):
            sender.send_message("§cНедостаточно прав.")
            return True
        action = args[0].lower() if args else "status"
        if action == "status" and len(args) <= 1:
            count = self._store.count if self._store is not None else 0
            sender.send_message(f"§aГоловы: {'готово' if self.ready else 'нужна проверка администратора'}. "
                                f"Выпадение: {'вкл' if self._settings.enabled else 'выкл'}, "
                                f"шанс: {self._settings.drop_chance_percent:g}%, установлено: {count}.")
            sender.send_message(f'§7Скины: зарегистрировано {len(self._skin_types)}; новые снимки применяются после перезапуска.')
            sender.send_message(f'§7Надетые головы: отправлено обновлений скина {self._worn_skins.updates}.')
            if self._settings.live_placed_skins and getattr(self.server, "protocol_version", None) == PROTOCOL:
                sender.send_message(f'§7Предмет для свежих скинов: {"зарегистрирован" if self._live_item_registered else "недоступен"}.')
            return True
        if action == "reload" and len(args) == 1:
            try:
                settings = load_settings(Path(self.data_folder) / "config.toml")
                self._validate_item(settings)
                if self._store is None or self._undelivered is None or self._key is None:
                    sender.send_message("§cСначала исправь ключ или NBT-хранилище и перезапусти сервер.")
                    return True
                self._store.flush()
                self._undelivered.flush()
                self._settings = settings
                self.ready = True
                sender.send_message("§aНастройки голов обновлены. Уже выданные головы сохранены.")
            except Exception:
                sender.send_message("§cКонфиг не принят. Проверь значения; предыдущие настройки сохранены.")
            return True
        if action == "give" and len(args) <= 2:
            if not self.ready:
                sender.send_message("§cВыдача голов недоступна: проверь /heads status и лог сервера.")
                return True
            if len(args) == 1:
                victim = sender if isinstance(sender, Player) else None
            else:
                name = args[1].strip('"')
                victim = next((player for player in self.server.online_players
                               if player.name.casefold() == name.casefold()), None)
            if victim is None:
                sender.send_message("§cУкажи ник игрока, который сейчас на сервере: /heads give <ник>.")
                return True
            try:
                self._give(sender, victim)
            except Exception:
                sender.send_message("§cНе удалось выдать голову. Проверь место в инвентаре и повтори попытку.")
                self._warn("не удалось выполнить выдачу головы")
            return True
        sender.send_message("§e/heads status — состояние\n/heads reload — перечитать конфиг\n/heads give [ник] — получить голову игрока в сети")
        return True
