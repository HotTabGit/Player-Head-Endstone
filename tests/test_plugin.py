import importlib.util
import inspect
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch, PropertyMock
from enum import Enum

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from endstone_player_heads.config import Settings
from endstone_player_heads.model import make_head_data, read_head_nbt
from endstone_player_heads.store import NbtHeadStore


class Scalar:
    def __init__(self, value):
        self.value = value


class FakeItem:
    def __init__(self, kind, amount=1, data=0):
        self.type = types.SimpleNamespace(id=kind)
        self.amount, self.data = amount, data
        self.nbt = {}
        self.item_meta = types.SimpleNamespace(display_name=None, lore=[])

    def set_item_meta(self, meta):
        self.item_meta = meta
        self.nbt["display"] = {"Name": Scalar(meta.display_name), "Lore": meta.lore}
        return True


class FakeDrop:
    def __init__(self, dimension, item, valid=True):
        self.dimension, self.item, self.is_valid = dimension, item, valid
        self.removed = False

    def remove(self):
        self.removed = True
        self.is_valid = False
        self.dimension.trace.append("rollback_drop")


class FakeBlock:
    def __init__(self, dimension, x, y, z, kind="minecraft:air", facing=1):
        self.dimension, self.x, self.y, self.z, self.type = dimension, x, y, z, kind
        self.data = types.SimpleNamespace(block_states={"facing_direction": facing})
        self.fail_remove = False
        self.throw_after_remove = False
        self.fail_capture = False
        self.fail_restore = False
        self.restore_calls = []

    def set_type(self, kind, apply_physics=True):
        self.dimension.trace.append("set_air")
        if self.fail_remove:
            raise RuntimeError("injected_remove_failure")
        self.type = kind
        if self.throw_after_remove:
            raise RuntimeError("injected_post_remove_failure")

    def capture_state(self):
        if self.fail_capture:
            raise RuntimeError("injected_capture_failure")
        return FakeState(self)


class FakeState:
    def __init__(self, block):
        self.block = block
        self.type = block.type
        self.block_states = dict(block.data.block_states)
        self.data = types.SimpleNamespace(block_states=self.block_states)

    def update(self, force=False, apply_physics=True):
        self.block.restore_calls.append((force, apply_physics))
        self.block.dimension.trace.append("restore_state")
        if self.block.fail_restore:
            return False
        self.block.type = self.type
        self.block.data.block_states = dict(self.block_states)
        return True


class FakeDimension:
    def __init__(self):
        self.name = "Overworld"
        self.loaded_chunks = [types.SimpleNamespace(x=0, z=0)]
        self.blocks = {}
        self.drops = []
        self.trace = []
        self.drop_valid = True
        self.drop_none = False
        self.reads = 0

    def get_block_at(self, x, y, z):
        self.reads += 1
        return self.blocks.setdefault((x, y, z), FakeBlock(self, x, y, z))

    def drop_item(self, location, item):
        self.trace.append("drop")
        if self.drop_none:
            return None
        drop = FakeDrop(self, item, self.drop_valid)
        self.drops.append(drop)
        return drop


class FakePlayer:
    def __init__(self, dimension, xuid="123", name="Victim"):
        self.dimension, self.xuid, self.name = dimension, xuid, name
        self.location = types.SimpleNamespace(dimension=dimension, x=0, y=64, z=0)
        self.inventory = types.SimpleNamespace(item_in_main_hand=None)
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)

    def has_permission(self, permission):
        return True


class FakeScheduler:
    def __init__(self):
        self.pending = []

    def run_task(self, plugin, callback, delay=0, period=0):
        self.pending.append(callback)
        return types.SimpleNamespace(cancel=lambda: None)

    def next_tick(self):
        callbacks, self.pending = self.pending, []
        for callback in callbacks:
            callback()


class FakeEvent:
    def __init__(self, **kwargs):
        self.is_cancelled = False
        self.__dict__.update(kwargs)

    def cancel(self):
        self.is_cancelled = True


class NativeSetterWouldCrash(BaseException):
    pass


class GuardedExplosionEvent(FakeEvent):
    def __init__(self, blocks, *, is_cancelled=False, setter_error=None):
        super().__init__(is_cancelled=is_cancelled)
        self._block_list = blocks
        self.block_list_writes = 0
        self.setter_error = setter_error

    @property
    def block_list(self):
        return self._block_list

    @block_list.setter
    def block_list(self, value):
        self.block_list_writes += 1
        if self.setter_error is not None:
            raise self.setter_error("native borrowed-block mutation must never be attempted")
        self._block_list = value


class Face(Enum):
    DOWN = 0
    UP = 1
    NORTH = 2
    SOUTH = 3
    WEST = 4
    EAST = 5


def event_handler(function=None, **kwargs):
    def decorate(callback):
        parameters = list(inspect.signature(callback).parameters.values())
        annotation = parameters[1].annotation
        if not isinstance(annotation, type):
            raise TypeError("Endstone requires an actual event class")
        callback._event_options = kwargs
        return callback
    return decorate(function) if function is not None else decorate


class FakePlugin:
    def __init__(self):
        self.logger = types.SimpleNamespace(warning=lambda text: None,
                                            error=lambda text: None, info=lambda text: None)


def load_adapter():
    names = ["ActorExplodeEvent", "BlockBreakEvent", "BlockExplodeEvent", "BlockPlaceEvent",
             "BlockPistonExtendEvent", "BlockPistonRetractEvent", "PlayerDeathEvent", "PlayerQuitEvent", "PlayerJoinEvent", "PlayerSkinChangeEvent", "PacketSendEvent"]
    events = types.ModuleType("endstone.event")
    for name in names:
        setattr(events, name, type(name, (), {}))
    events.EventPriority = types.SimpleNamespace(MONITOR=5, HIGHEST=4)
    events.event_handler = event_handler
    stubs = {
        "endstone": types.SimpleNamespace(Player=FakePlayer),
        "endstone.block": types.SimpleNamespace(BlockFace=Face),
        "endstone.event": events,
        "endstone.inventory": types.SimpleNamespace(ItemStack=FakeItem),
        "endstone.level": types.SimpleNamespace(Location=lambda dim, x, y, z: types.SimpleNamespace(dimension=dim, x=x, y=y, z=z)),
        "endstone.permissions": types.SimpleNamespace(PermissionDefault=types.SimpleNamespace(OP="op")),
        "endstone.plugin": types.SimpleNamespace(Plugin=FakePlugin),
        "endstone.nbt": types.SimpleNamespace(CompoundTag=dict, IntTag=Scalar, LongTag=Scalar, StringTag=Scalar),
    }
    spec = importlib.util.spec_from_file_location("endstone_player_heads._tested_plugin",
                                                SRC / "endstone_player_heads" / "plugin.py")
    adapter = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(adapter)
    return adapter, stubs


ADAPTER, STUBS = load_adapter()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.key = b"k" * 32
        self.plugin = ADAPTER.PlayerHeadsPlugin()
        self.dimension = FakeDimension()
        self.scheduler = FakeScheduler()
        self.plugin.server = types.SimpleNamespace(
            scheduler=self.scheduler,
            level=types.SimpleNamespace(get_dimension=lambda name: self.dimension, name="test"),
            online_players=[],
        )
        self.plugin.data_folder = Path(self.temporary.name)
        self.plugin._main_thread = threading.get_ident()
        self.plugin._key = self.key
        self.plugin._settings = Settings()
        self.plugin._store = NbtHeadStore(self.plugin.data_folder / "heads.nbt", "test", self.key)
        self.plugin._undelivered = NbtHeadStore(self.plugin.data_folder / "undelivered.nbt", "test", self.key)
        self.plugin.ready = True
        self.player = FakePlayer(self.dimension)
        self.data = make_head_data(self.player.xuid, self.player.name)
        self.nbt_patch = patch.dict(sys.modules, STUBS)
        self.nbt_patch.start()
        self.addCleanup(self.nbt_patch.stop)

    def head(self, x=1, y=64, z=1):
        block = self.dimension.get_block_at(x, y, z)
        block.type = "minecraft:player_head"
        self.dimension.get_block_at(x, y - 1, z).type = "minecraft:stone"
        self.plugin._store.set(("Overworld", x, y, z), self.data)
        return block

    def place(self, x=1, y=64, z=1):
        block = self.dimension.get_block_at(x, y, z)
        self.player.inventory.item_in_main_hand = self.plugin.make_item(self.data)
        return FakeEvent(block=block, block_placed_state=types.SimpleNamespace(type="minecraft:player_head"), player=self.player)

    def restart_adapter_from_stores(self):
        restarted = ADAPTER.PlayerHeadsPlugin()
        restarted.server = types.SimpleNamespace(
            scheduler=FakeScheduler(), level=self.plugin.server.level, online_players=[])
        restarted.data_folder = self.plugin.data_folder
        restarted._main_thread = threading.get_ident()
        restarted._key = self.key
        restarted._settings = Settings()
        restarted._store = NbtHeadStore(self.plugin.data_folder / "heads.nbt", "test", self.key)
        restarted._undelivered = NbtHeadStore(self.plugin.data_folder / "undelivered.nbt", "test", self.key)
        restarted.ready = True
        return restarted

    def leave_undelivered_head_on_shutdown(self):
        block = self.head()
        self.dimension.drop_none = True
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.dimension.get_block_at(1, 63, 1).type = "minecraft:air"
        self.scheduler.next_tick()
        self.plugin.on_disable()
        return block

    def test_handlers_have_resolved_native_event_classes(self):
        for name in ("on_death", "on_place_guard", "on_place", "on_break", "on_extend", "on_retract",
                     "on_actor_explode", "on_block_explode", "on_quit"):
            annotation = inspect.signature(getattr(ADAPTER.PlayerHeadsPlugin, name)).parameters["event"].annotation
            self.assertIsInstance(annotation, type, name)

    def test_make_item_preserves_signature_when_name_and_lore_applied(self):
        item = self.plugin.make_item(self.data)
        self.assertEqual(read_head_nbt(item.nbt, self.key), self.data)
        self.assertIn(self.player.name, item.item_meta.display_name)
        self.assertEqual(item.type.id, "minecraft:player_head")

    def test_custom_head_preserves_authenticated_identity_on_break(self):
        from endstone_player_heads.skins import custom_type
        kind = custom_type('a' * 64)
        self.plugin._skins = types.SimpleNamespace(key_for=lambda data: 'a' * 64)
        self.plugin._skin_types = frozenset([kind])
        block = self.head()
        block.type = kind
        self.plugin.on_break(FakeEvent(block=block, player=self.player))
        self.assertEqual(self.dimension.drops[0].item.type.id, kind)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(block.type, 'minecraft:air')
        self.assertEqual(self.plugin._store.count, 0)

    def test_custom_wall_head_support_is_protected_and_explosion_keeps_identity(self):
        from endstone_player_heads.skins import custom_type
        kind = custom_type('a' * 64)
        self.plugin._skins = types.SimpleNamespace(key_for=lambda data: 'a' * 64)
        self.plugin._skin_types = frozenset([kind])
        block = self.head()
        block.type = kind
        block.data.block_states = {'minecraft:block_face': 'north'}
        support = self.dimension.get_block_at(1, 64, 2)
        support.type = 'minecraft:stone'
        event = FakeEvent(block=support, player=self.player)
        self.plugin.on_break(event)
        self.assertTrue(event.is_cancelled)
        explosion = GuardedExplosionEvent([support])
        self.plugin.on_actor_explode(explosion)
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(self.dimension.drops[0].item.type.id, kind)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)

    def test_inventory_upgrade_skips_active_ui_session(self):
        kind = 'kartexheads:h_' + 'a' * 64
        self.plugin._skin_types = frozenset([kind])
        self.plugin.server.online_players = [self.player]
        name = 'endstone_inventoryui.manager.player_manager'
        with patch.dict(sys.modules, {name: types.SimpleNamespace(find_session=lambda player: object())}):
            self.plugin._upgrade_inventory_heads()

    def test_beta6_head_recovers_without_changing_signed_identity(self):
        from endstone_player_heads.skins import LIVE_HEAD
        item = self.plugin.make_item(self.data)
        item.type.id = LIVE_HEAD
        slots = [item]
        self.player.inventory = types.SimpleNamespace(
            size=1, helmet=None, item_in_off_hand=None, get_item=slots.__getitem__,
            set_item=slots.__setitem__)
        self.plugin.server.online_players = [self.player]
        self.plugin._live_item_registered = True
        self.plugin._upgrade_inventory_heads()
        self.assertEqual(slots[0].type.id, 'minecraft:player_head')
        self.assertEqual(self.plugin._item_data(slots[0]), self.data)

    def test_only_authenticated_online_session_supplies_skin(self):
        calls = []
        self.plugin._skins = types.SimpleNamespace(capture=lambda *args: calls.append(args))
        self.player.skin = types.SimpleNamespace(image='actual image')
        self.plugin.server.online_mode = False
        self.plugin._capture_skin(self.player)
        self.assertEqual(calls, [])
        self.plugin.server.online_mode = True
        self.plugin._capture_skin(self.player, self.data.head_id)
        self.assertEqual(calls, [('123', 'actual image', self.data.head_id)])

    def test_cancelled_break_does_not_emit_or_remove_anything(self):
        block = self.head()
        event = FakeEvent(block=block, player=self.player, is_cancelled=True)
        self.plugin.on_break(event)
        self.assertEqual(self.dimension.trace, [])
        self.assertEqual(self.plugin._store.count, 1)

    def test_break_drops_exactly_once_before_air_and_pops_record(self):
        block = self.head()
        event = FakeEvent(block=block, player=self.player)
        self.plugin.on_break(event)
        self.assertTrue(event.is_cancelled)
        self.assertEqual(self.dimension.trace, ["drop", "set_air"])
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(block.type, "minecraft:air")
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.plugin.on_break(FakeEvent(block=block, player=self.player))
        self.assertEqual(len(self.dimension.drops), 1)

    def test_invalid_spawn_keeps_world_head_and_record(self):
        block = self.head()
        self.dimension.drop_valid = False
        self.plugin.on_break(FakeEvent(block=block, player=self.player))
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(self.dimension.trace, ["drop"])
        self.assertTrue(self.plugin.ready)

    def test_missing_spawn_keeps_world_head_and_record(self):
        block = self.head()
        self.dimension.drop_none = True
        self.plugin.on_break(FakeEvent(block=block, player=self.player))
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(self.dimension.trace, ["drop"])

    def test_failed_block_removal_rolls_back_drop_and_keeps_record(self):
        block = self.head()
        block.fail_remove = True
        self.plugin.on_break(FakeEvent(block=block, player=self.player))
        self.assertEqual(self.dimension.trace, ["drop", "set_air", "rollback_drop"])
        self.assertTrue(self.dimension.drops[0].removed)
        self.assertEqual(self.plugin._store.count, 1)

    def test_post_mutation_exception_does_not_delete_successful_drop(self):
        block = self.head()
        block.throw_after_remove = True
        self.plugin.on_break(FakeEvent(block=block, player=self.player))
        self.assertEqual(self.dimension.trace, ["drop", "set_air"])
        self.assertFalse(self.dimension.drops[0].removed)
        self.assertEqual(self.plugin._store.count, 0)

    def test_disabled_new_drops_still_restore_existing_heads(self):
        self.plugin._settings = Settings(enabled=False)
        self.plugin.on_break(FakeEvent(block=self.head(), player=self.player))
        self.assertEqual(len(self.dimension.drops), 1)

    def test_not_ready_protects_registered_head_without_new_drop(self):
        block = self.head()
        self.plugin.ready = False
        event = FakeEvent(block=block, player=self.player)
        self.plugin.on_break(event)
        self.assertTrue(event.is_cancelled)
        self.assertFalse(self.dimension.drops)

    def test_destroying_support_is_blocked_but_unrelated_block_is_allowed(self):
        self.head()
        support = self.dimension.get_block_at(1, 63, 1)
        support.type = "minecraft:stone"
        event = FakeEvent(block=support, player=self.player)
        self.plugin.on_break(event)
        self.assertTrue(event.is_cancelled)
        self.assertIn("Сначала", self.player.messages[-1])
        event = FakeEvent(block=self.dimension.get_block_at(4, 63, 1), player=self.player)
        self.plugin.on_break(event)
        self.assertFalse(event.is_cancelled)

    def test_wall_attachment_uses_orientation(self):
        block = self.head()
        block.data.block_states["facing_direction"] = 2
        wall = self.dimension.get_block_at(1, 64, 2)
        event = FakeEvent(block=wall, player=self.player)
        self.plugin.on_break(event)
        self.assertTrue(event.is_cancelled)
        floor = self.dimension.get_block_at(1, 63, 1)
        event = FakeEvent(block=floor, player=self.player)
        self.plugin.on_break(event)
        self.assertFalse(event.is_cancelled)

    def test_piston_protects_head_and_its_support(self):
        self.head(2, 65, 1)
        piston = self.dimension.get_block_at(0, 64, 1)
        event = FakeEvent(block=piston, direction=Face.EAST)
        self.plugin.on_extend(event)
        self.assertTrue(event.is_cancelled)
        cancelled = FakeEvent(block=piston, direction=Face.EAST, is_cancelled=True)
        before = self.dimension.reads
        self.plugin.on_extend(cancelled)
        self.assertEqual(self.dimension.reads, before)

    def test_affected_explosion_removes_head_and_defers_one_signed_drop(self):
        block = self.head()
        support = self.dimension.get_block_at(1, 63, 1)
        other = self.dimension.get_block_at(2, 63, 1)
        blocks = [block, support, other]
        event = GuardedExplosionEvent(blocks)
        support.type = other.type = "minecraft:stone"
        self.plugin.on_actor_explode(event)
        self.assertFalse(event.is_cancelled)
        self.assertIs(event.block_list, blocks)
        self.assertEqual(event.block_list, [block, support, other])
        self.assertEqual(event.block_list_writes, 0)
        self.assertEqual(block.type, "minecraft:air")
        self.assertEqual(support.type, "minecraft:stone")
        self.assertEqual(other.type, "minecraft:stone")
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(self.dimension.trace, ["set_air"])
        self.assertEqual(self.dimension.drops, [])
        support.type = other.type = "minecraft:air"
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertIn(self.player.name, self.dimension.drops[0].item.item_meta.display_name)
        self.assertEqual(self.dimension.trace, ["set_air", "drop"])
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)

    def test_explosion_of_only_support_also_drops_attached_signed_head(self):
        block = self.head()
        support = self.dimension.get_block_at(1, 63, 1)
        blocks = [support]
        event = GuardedExplosionEvent(blocks)
        self.plugin.on_block_explode(event)
        self.assertFalse(event.is_cancelled)
        self.assertIs(event.block_list, blocks)
        self.assertEqual(event.block_list_writes, 0)
        self.assertEqual(self.plugin._store.get(("Overworld", 1, 64, 1)), self.data)
        self.assertEqual(block.type, "minecraft:air")
        self.assertEqual(self.dimension.trace, ["set_air"])
        self.assertEqual(self.dimension.drops, [])
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(self.plugin._store.count, 0)

    def test_readonly_eventproperty_must_never_be_assigned(self):
        for index, (callback, setter_error) in enumerate((
            (self.plugin.on_actor_explode, RuntimeError),
            (self.plugin.on_actor_explode, NativeSetterWouldCrash),
            (self.plugin.on_block_explode, RuntimeError),
            (self.plugin.on_block_explode, NativeSetterWouldCrash),
        ), 1):
            with self.subTest(callback=callback.__name__, setter_error=setter_error.__name__):
                block = self.head(index)
                blocks = [block]
                event = GuardedExplosionEvent(blocks, setter_error=setter_error)
                callback(event)
                self.assertFalse(event.is_cancelled)
                self.assertEqual(event.block_list_writes, 0)
                self.assertIs(event.block_list, blocks)
                self.assertEqual(block.type, "minecraft:air")
                self.scheduler.next_tick()
                self.assertEqual(self.plugin._pending_explosions, {})
                self.assertEqual(len(self.dimension.drops), index)

    def test_unprotected_explosion_is_allowed_unchanged_with_registered_head_elsewhere(self):
        block = self.head()
        other = self.dimension.get_block_at(8, 63, 8)
        blocks = [other]
        event = GuardedExplosionEvent(blocks, setter_error=NativeSetterWouldCrash)
        self.plugin.on_block_explode(event)
        self.assertFalse(event.is_cancelled)
        self.assertIs(event.block_list, blocks)
        self.assertEqual(event.block_list_writes, 0)
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.dimension.trace, [])
        self.assertEqual(self.dimension.drops, [])

    def test_unprotected_explosion_without_any_registered_heads_is_allowed_unchanged(self):
        other = self.dimension.get_block_at(8, 63, 8)
        blocks = [other]
        event = GuardedExplosionEvent(blocks, setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(event)
        self.assertFalse(event.is_cancelled)
        self.assertIs(event.block_list, blocks)
        self.assertEqual(event.block_list_writes, 0)
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.dimension.trace, [])
        self.assertEqual(self.dimension.drops, [])

    def test_cancelled_explosion_does_not_mutate_list(self):
        block = self.head()
        blocks = [block]
        event = GuardedExplosionEvent(blocks, is_cancelled=True, setter_error=NativeSetterWouldCrash)
        before_reads = self.dimension.reads
        self.plugin.on_block_explode(event)
        self.assertTrue(event.is_cancelled)
        self.assertIs(event.block_list, blocks)
        self.assertEqual(event.block_list, [block])
        self.assertEqual(event.block_list_writes, 0)
        self.assertEqual(self.dimension.reads, before_reads)
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(self.dimension.trace, [])
        self.assertEqual(self.dimension.drops, [])

    def test_two_distinct_heads_produce_exactly_one_signed_drop_each(self):
        first, second = self.head(1), self.head(3)
        second_data = make_head_data("456", "SecondPlayer")
        self.plugin._store.set(("Overworld", 3, 64, 1), second_data)
        event = GuardedExplosionEvent([first, second], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(event)
        self.assertFalse(event.is_cancelled)
        self.assertEqual(self.dimension.drops, [])
        self.assertEqual(len(self.plugin._pending_explosions), 2)
        self.scheduler.next_tick()
        drops = [read_head_nbt(drop.item.nbt, self.key) for drop in self.dimension.drops]
        self.assertEqual(len(drops), 2)
        self.assertEqual({drop.head_id for drop in drops}, {self.data.head_id, second_data.head_id})
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._pending_explosions, {})

    def test_overlapping_explosions_and_repeated_finish_cannot_duplicate_head(self):
        block = self.head()
        event = GuardedExplosionEvent([block], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(event)
        position = ("Overworld", 1, 64, 1)
        pending = self.plugin._pending_explosions[position]
        overlapping = GuardedExplosionEvent([block], setter_error=NativeSetterWouldCrash)
        self.plugin.on_block_explode(overlapping)
        self.assertFalse(overlapping.is_cancelled)
        self.assertEqual(len(self.scheduler.pending), 1)
        self.scheduler.next_tick()
        self.plugin._finish_exploded_head(position, pending)
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 0)

    def test_wall_head_is_collected_only_when_its_actual_support_explodes(self):
        block = self.head()
        block.data.block_states["facing_direction"] = 2
        floor = self.dimension.get_block_at(1, 63, 1)
        floor_event = GuardedExplosionEvent([floor], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(floor_event)
        self.assertFalse(floor_event.is_cancelled)
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.scheduler.pending, [])
        wall = self.dimension.get_block_at(1, 64, 2)
        wall_event = GuardedExplosionEvent([wall], setter_error=NativeSetterWouldCrash)
        self.plugin.on_block_explode(wall_event)
        self.assertFalse(wall_event.is_cancelled)
        self.assertEqual(block.type, "minecraft:air")
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)

    def test_capture_failure_cancels_blast_before_world_changes(self):
        block = self.head()
        block.fail_capture = True
        event = GuardedExplosionEvent([block], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(event)
        self.assertTrue(event.is_cancelled)
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.dimension.trace, [])
        self.assertEqual(self.dimension.drops, [])
        self.assertEqual(self.scheduler.pending, [])
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 1)

    def test_explosion_scheduler_failure_cancels_without_removing_head(self):
        block = self.head()
        event = GuardedExplosionEvent([block], setter_error=NativeSetterWouldCrash)
        with patch.object(self.scheduler, "run_task", side_effect=RuntimeError("injected")):
            self.plugin.on_actor_explode(event)
        self.assertTrue(event.is_cancelled)
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.dimension.trace, [])
        self.assertEqual(self.dimension.drops, [])
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 1)

    def test_explosion_removal_failure_cancels_and_cleans_scheduled_transfer(self):
        block = self.head()
        block.fail_remove = True
        event = GuardedExplosionEvent([block], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(event)
        self.assertTrue(event.is_cancelled)
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.plugin._pending_explosions, {})
        self.scheduler.next_tick()
        self.assertEqual(self.dimension.drops, [])
        self.assertEqual(self.plugin._store.count, 1)

    def test_partial_multihead_removal_failure_restores_prior_removed_head(self):
        first, second = self.head(1), self.head(3)
        second.fail_remove = True
        event = GuardedExplosionEvent([first, second], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(event)
        self.assertTrue(event.is_cancelled)
        self.assertEqual(first.type, "minecraft:player_head")
        self.assertEqual(second.type, "minecraft:player_head")
        self.assertEqual(first.restore_calls, [(True, False)])
        self.assertEqual(self.plugin._pending_explosions, {})
        self.scheduler.next_tick()
        self.assertEqual(self.dimension.drops, [])
        self.assertEqual(self.plugin._store.count, 2)

    def test_post_mutation_explosion_removal_exception_still_drops_once(self):
        block = self.head()
        block.throw_after_remove = True
        event = GuardedExplosionEvent([block], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(event)
        self.assertFalse(event.is_cancelled)
        self.assertEqual(block.type, "minecraft:air")
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._pending_explosions, {})

    def test_invalid_explosion_spawn_restores_original_head_and_signed_record(self):
        block = self.head()
        block.data.block_states["facing_direction"] = 2
        self.dimension.get_block_at(1, 64, 2).type = "minecraft:stone"
        self.dimension.drop_valid = False
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.assertEqual(block.type, "minecraft:air")
        self.scheduler.next_tick()
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(block.data.block_states["facing_direction"], 2)
        self.assertEqual(block.restore_calls, [(True, False)])
        self.assertEqual(self.plugin._store.get(("Overworld", 1, 64, 1)), self.data)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertFalse(any(drop.is_valid for drop in self.dimension.drops))
        self.assertTrue(self.plugin.ready)

    def test_missing_explosion_spawn_restores_without_creating_item(self):
        block = self.head()
        self.dimension.drop_none = True
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.scheduler.next_tick()
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(block.restore_calls, [(True, False)])
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.dimension.drops, [])

    def test_failed_explosion_restore_keeps_pending_record_and_stops_issuance(self):
        block = self.head()
        self.dimension.drop_none = True
        block.fail_restore = True
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.scheduler.next_tick()
        self.assertFalse(self.plugin.ready)
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(len(self.plugin._pending_explosions), 1)
        self.assertEqual(self.dimension.drops, [])

    def test_new_store_identity_is_not_consumed_by_older_explosion_transfer(self):
        block = self.head()
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        replacement = make_head_data("789", "Replacement")
        position = ("Overworld", 1, 64, 1)
        self.plugin._store.set(position, replacement)
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._store.get(position), replacement)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.dimension.drops, [])

    def test_pending_explosion_blocks_any_placement_and_break_until_transfer_finishes(self):
        block = self.head()
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        placement = FakeEvent(block=block, player=self.player,
                              block_placed_state=types.SimpleNamespace(type="minecraft:stone"))
        self.plugin.on_place_guard(placement)
        self.assertTrue(placement.is_cancelled)
        breaking = FakeEvent(block=block, player=self.player)
        self.plugin.on_break(breaking)
        self.assertTrue(breaking.is_cancelled)
        self.assertEqual(self.dimension.drops, [])
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(self.plugin._pending_explosions, {})

    def test_maintenance_cannot_retire_air_record_during_pending_explosion(self):
        block = self.head()
        self.dimension.get_block_at(1, 63, 1).type = "minecraft:air"
        self.dimension.drop_none = True
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.plugin._tick = 80
        self.plugin._maintenance()
        self.assertEqual(self.plugin._store.get(("Overworld", 1, 64, 1)), self.data)
        self.assertEqual(len(self.plugin._pending_explosions), 1)
        self.dimension.drop_none = False
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(self.plugin._store.count, 0)

    def test_failed_spawn_with_destroyed_support_retries_without_restoring_floating_head(self):
        block = self.head()
        self.dimension.drop_none = True
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.dimension.get_block_at(1, 63, 1).type = "minecraft:air"
        self.scheduler.next_tick()
        self.assertEqual(block.type, "minecraft:air")
        self.assertEqual(block.restore_calls, [])
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(len(self.plugin._pending_explosions), 1)
        self.assertEqual(len(self.scheduler.pending), 1)
        self.assertTrue(self.plugin.ready)
        self.dimension.drop_none = False
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 0)

    def test_persistent_spawn_failure_retries_via_maintenance_without_task_flood(self):
        block = self.head()
        self.dimension.drop_none = True
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.dimension.get_block_at(1, 63, 1).type = "minecraft:air"
        self.scheduler.next_tick()
        self.scheduler.next_tick()
        self.assertEqual(self.scheduler.pending, [])
        self.assertEqual(block.restore_calls, [])
        self.plugin._maintenance()
        self.assertEqual(self.scheduler.pending, [])
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(len(self.plugin._pending_explosions), 1)
        self.dimension.drop_none = False
        self.plugin._maintenance()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 0)

    def test_store_retirement_failure_after_valid_spawn_never_reissues_or_restores_head(self):
        block = self.head()
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        position = ("Overworld", 1, 64, 1)
        pending = self.plugin._pending_explosions[position]
        with patch.object(self.plugin._store, "pop", side_effect=RuntimeError("injected")):
            self.scheduler.next_tick()
        self.assertFalse(self.plugin.ready)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(block.type, "minecraft:air")
        self.assertEqual(block.restore_calls, [])
        self.assertEqual(len(self.dimension.drops), 1)
        self.plugin._finish_exploded_head(position, pending)
        self.plugin._maintenance()
        self.assertEqual(len(self.dimension.drops), 1)

    def test_same_tick_new_placement_is_promoted_before_explosion_and_old_callback_cannot_erase_it(self):
        placement = self.place()
        self.plugin.on_place(placement)
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._reserved_count, 1)
        placement.block.type = "minecraft:player_head"
        explosion = GuardedExplosionEvent([placement.block], setter_error=NativeSetterWouldCrash)
        self.plugin.on_actor_explode(explosion)
        self.assertFalse(explosion.is_cancelled)
        self.assertEqual(placement.block.type, "minecraft:air")
        self.assertEqual(self.plugin._store.get(("Overworld", 1, 64, 1)), self.data)
        self.assertEqual(self.plugin._reserved_count, 0)
        self.assertEqual(self.plugin._pending_placements, {})
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._pending_explosions, {})

    def test_same_tick_new_placement_is_detected_when_only_support_explodes(self):
        placement = self.place()
        self.plugin.on_place(placement)
        placement.block.type = "minecraft:player_head"
        support = self.dimension.get_block_at(1, 63, 1)
        support.type = "minecraft:stone"
        explosion = GuardedExplosionEvent([support], setter_error=NativeSetterWouldCrash)
        self.plugin.on_block_explode(explosion)
        self.assertFalse(explosion.is_cancelled)
        self.assertEqual(placement.block.type, "minecraft:air")
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._pending_explosions, {})

    def test_disable_finishes_pending_explosion_once_before_persisting(self):
        block = self.head()
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.plugin.on_disable()
        self.assertFalse(self.plugin.ready)
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 0)
        self.scheduler.next_tick()
        self.assertEqual(len(self.dimension.drops), 1)

    def test_disable_with_failed_spawn_restores_pending_head_before_stopping(self):
        block = self.head()
        self.dimension.drop_none = True
        self.plugin.on_actor_explode(GuardedExplosionEvent([block]))
        self.plugin.on_disable()
        self.assertFalse(self.plugin.ready)
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.plugin._pending_explosions, {})
        self.assertEqual(self.plugin._store.count, 1)
        self.assertEqual(self.dimension.drops, [])

    def test_shutdown_with_destroyed_support_persists_and_recovers_one_signed_drop(self):
        block = self.leave_undelivered_head_on_shutdown()
        position = ("Overworld", 1, 64, 1)
        self.assertEqual(block.type, "minecraft:air")
        self.assertEqual(self.dimension.drops, [])
        self.assertEqual(self.plugin._undelivered.get(position), self.data)
        restarted = self.restart_adapter_from_stores()
        self.assertEqual(restarted._store.get(position), self.data)
        self.assertEqual(restarted._undelivered.get(position), self.data)
        self.dimension.drop_none = False
        restarted._maintenance()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(restarted._store.count, 0)
        self.assertEqual(restarted._undelivered.count, 0)
        self.assertEqual(restarted._pending_explosions, {})
        restarted._maintenance()
        restarted.on_disable()
        self.assertEqual(len(self.dimension.drops), 1)
        again = self.restart_adapter_from_stores()
        again._maintenance()
        self.assertEqual(len(self.dimension.drops), 1)

    def test_recovery_discards_stale_journal_when_new_head_identity_occupies_position(self):
        block = self.leave_undelivered_head_on_shutdown()
        position = ("Overworld", 1, 64, 1)
        replacement = make_head_data("789", "Replacement")
        self.plugin._store.set(position, replacement)
        self.plugin._store.flush()
        block.type = "minecraft:player_head"
        self.dimension.drop_none = False
        restarted = self.restart_adapter_from_stores()
        restarted._maintenance()
        self.assertEqual(restarted._store.get(position), replacement)
        self.assertEqual(restarted._undelivered.count, 0)
        self.assertEqual(restarted._pending_explosions, {})
        self.assertEqual(block.type, "minecraft:player_head")
        self.assertEqual(self.dimension.drops, [])

    def test_recovery_waits_for_loaded_chunk_before_delivering_saved_head(self):
        self.leave_undelivered_head_on_shutdown()
        position = ("Overworld", 1, 64, 1)
        self.dimension.drop_none = False
        self.dimension.loaded_chunks = []
        restarted = self.restart_adapter_from_stores()
        before_reads = self.dimension.reads
        restarted._maintenance()
        self.assertEqual(self.dimension.reads, before_reads)
        self.assertEqual(self.dimension.drops, [])
        self.assertEqual(restarted._undelivered.get(position), self.data)
        self.assertEqual(restarted._store.get(position), self.data)
        self.dimension.loaded_chunks = [types.SimpleNamespace(x=0, z=0)]
        restarted._maintenance()
        self.assertEqual(len(self.dimension.drops), 1)
        self.assertEqual(read_head_nbt(self.dimension.drops[0].item.nbt, self.key), self.data)
        self.assertEqual(restarted._undelivered.count, 0)

    def test_placement_registered_only_after_actual_world_change(self):
        event = self.place()
        self.plugin.on_place(event)
        self.assertEqual(self.plugin._store.count, 0)
        event.block.type = "minecraft:player_head"
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._store.get(("Overworld", 1, 64, 1)), self.data)
        self.assertEqual(self.plugin._reserved_count, 0)
        self.assertEqual(self.plugin._reserved_bytes, 0)

    def test_failed_placement_releases_reservation_and_does_not_register(self):
        event = self.place()
        self.plugin.on_place(event)
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._reserved_count, 0)
        self.assertEqual(self.plugin._reserved_bytes, 0)

    def test_cancel_after_guard_releases_reservation_without_monitor(self):
        event = self.place()
        self.plugin.on_place_guard(event)
        event.cancel()
        self.plugin.on_place(event)
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._reserved_count, 0)

    def test_previous_cancelled_placement_never_reserves(self):
        event = self.place()
        event.cancel()
        self.plugin.on_place_guard(event)
        self.plugin.on_place(event)
        self.assertEqual(self.plugin._reserved_count, 0)
        self.assertEqual(self.scheduler.pending, [])

    def test_same_tick_pending_placements_cannot_overbook_record_limit(self):
        self.head(8)
        with patch.object(ADAPTER, "MAX_RECORDS", 2):
            first, second = self.place(1), self.place(2)
            self.plugin.on_place(first)
            self.plugin.on_place_guard(second)
            self.assertFalse(first.is_cancelled)
            self.assertTrue(second.is_cancelled)
            first.block.type = "minecraft:player_head"
            self.scheduler.next_tick()
            self.assertEqual(self.plugin._store.count, 2)
            self.assertEqual(self.plugin._reserved_count, 0)

    def test_pending_placements_cannot_overbook_byte_limit(self):
        first, second = self.place(1), self.place(2)
        needed = self.plugin._store.entry_size(("Overworld", 1, 64, 1), self.data)
        with patch.object(NbtHeadStore, "remaining_bytes", new_callable=PropertyMock, return_value=needed):
            self.plugin.on_place_guard(first)
            self.plugin.on_place_guard(second)
            self.assertFalse(first.is_cancelled)
            self.assertTrue(second.is_cancelled)

    def test_repeated_coordinate_approval_does_not_double_reserve(self):
        first, second = self.place(1), self.place(1)
        self.plugin.on_place_guard(first)
        first_token = self.plugin._placement_approvals[("Overworld", 1, 64, 1)][0]
        self.plugin.on_place_guard(second)
        self.assertEqual(self.plugin._reserved_count, 1)
        self.plugin._release_reservation(("Overworld", 1, 64, 1), first_token)
        self.assertEqual(self.plugin._reserved_count, 1)
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._reserved_count, 0)

    def test_not_ready_rejects_new_signed_placement_before_mutation(self):
        event = self.place()
        self.plugin.ready = False
        self.plugin.on_place_guard(event)
        self.assertTrue(event.is_cancelled)
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(event.block.type, "minecraft:air")

    def test_same_tick_plain_replacement_does_not_inherit_previous_head_identity(self):
        first = self.place()
        self.plugin.on_place(first)
        second = self.place()
        self.player.inventory.item_in_main_hand = FakeItem("minecraft:player_head")
        self.plugin.on_place_guard(second)
        self.plugin.on_place(second)
        second.block.type = "minecraft:player_head"
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._store.count, 0)
        self.assertEqual(self.plugin._reserved_count, 0)

    def test_scheduler_failure_releases_pending_reservation(self):
        event = self.place()
        self.plugin.on_place_guard(event)
        with patch.object(self.scheduler, "run_task", side_effect=RuntimeError("injected")):
            self.plugin.on_place(event)
        self.scheduler.next_tick()
        self.assertEqual(self.plugin._reserved_count, 0)
        self.assertEqual(self.plugin._pending_placements, {})

    def test_store_preflight_failure_cancels_before_consuming_head(self):
        event = self.place()
        with patch.object(self.plugin._store, "can_set", side_effect=RuntimeError("injected")):
            self.plugin.on_place_guard(event)
        self.assertTrue(event.is_cancelled)
        self.assertEqual(self.plugin._reserved_count, 0)
        self.assertEqual(event.block.type, "minecraft:air")

    def test_flush_failure_stops_issuance_and_keeps_records(self):
        self.head()
        with patch.object(self.plugin._store, "flush", side_effect=OSError("injected")):
            self.plugin._maintenance()
        self.assertFalse(self.plugin.ready)
        self.assertEqual(self.plugin._store.count, 1)

    def test_reload_failure_retains_previous_settings(self):
        previous = self.plugin._settings
        (self.plugin.data_folder / "config.toml").write_text("[heads]\nenabled = 123\n", encoding="utf-8")
        self.plugin.on_command(self.player, types.SimpleNamespace(name="heads"), ["reload"])
        self.assertIs(self.plugin._settings, previous)
        self.assertIn("предыдущие", self.player.messages[-1])

    def test_reload_keeps_unavailable_delivery_store_disabled(self):
        self.plugin.ready = False
        self.plugin._undelivered = None
        (self.plugin.data_folder / "config.toml").write_text("[heads]\nenabled = true\n", encoding="utf-8")
        self.plugin.on_command(self.player, types.SimpleNamespace(name="heads"), ["reload"])
        self.assertFalse(self.plugin.ready)
        self.assertIn("перезапусти", self.player.messages[-1])

    def test_key_loss_is_detected_even_before_any_head_was_placed(self):
        key = self.plugin._read_key(self.plugin.data_folder)
        self.assertEqual(len(key), 32)
        self.assertTrue((self.plugin.data_folder / "key.required").is_file())
        (self.plugin.data_folder / "signing.key").unlink()
        with self.assertRaises(ValueError):
            self.plugin._read_key(self.plugin.data_folder)

    def test_invalid_death_spawn_does_not_consume_pair_cooldown(self):
        self.dimension.drop_valid = False
        killer = FakePlayer(self.dimension, "456", "Killer")
        event = FakeEvent(player=self.player, damage_source=types.SimpleNamespace(actor=killer))
        self.plugin.on_death(event)
        self.assertEqual(self.plugin._drops, 0)
        self.assertEqual(self.plugin._cooldowns, {})

    def test_environment_death_without_source_is_allowed_only_when_configured(self):
        event = FakeEvent(player=self.player, damage_source=None)
        self.plugin.on_death(event)
        self.assertEqual(self.plugin._drops, 0)
        self.plugin._settings = Settings(pvp_only=False)
        self.plugin.on_death(event)
        self.assertEqual(self.plugin._drops, 1)


if __name__ == "__main__":
    unittest.main()
