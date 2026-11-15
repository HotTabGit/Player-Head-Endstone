import json
from pathlib import Path
from struct import pack, unpack
import sys
from types import SimpleNamespace
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endstone_player_heads.live_skin import (ADD_PLAYER, PLAYER_LIST, REMOVE_ACTOR, LiveSkins,
                                             PacketReader, add_list_entry, appearance, avatar,
                                             field, serialize_skin, signed, skull_rotation)
from endstone_player_heads.skins import LIVE_HEAD
from endstone_player_heads.worn_skin import WornSkins, merged_entry, roster_entries


class LiveSkinTests(unittest.TestCase):
    def test_live_skin_preserves_alpha_and_uv_for_both_layers(self):
        for width in (64, 128):
            with self.subTest(width=width):
                scale = width // 64
                pixels = bytearray(width * width * 4)
                for x, rgba in ((8, b"\xcc\x33\x11\xff"), (40, b"\x55\x66\x77\x80")):
                    index = (8 * scale * width + x * scale) * 4
                    pixels[index:index + 4] = rgba
                reader = PacketReader(serialize_skin("key", width, width, bytes(pixels), "1.26.60"))
                self.assertTrue(reader.blob().startswith(b"kmheads:layers:"))
                patch = json.loads(reader.blob())
                self.assertEqual(unpack("<II", reader.take(8)), (width, width))
                self.assertEqual(reader.blob(), bytes(pixels))
                reader.take(10)
                geometry = json.loads(reader.blob())["minecraft:geometry"][0]
                self.assertEqual(patch["geometry"]["default"], geometry["description"]["identifier"])
                head, hat = geometry["bones"]
                self.assertEqual(hat["parent"], head["name"])
                self.assertGreater(hat["cubes"][0]["inflate"], 0)
                for bone, x in ((head, 8), (hat, 40)):
                    self.assertEqual(bone["cubes"][0]["uv"]["north"],
                                     {"uv": [x * scale, 8 * scale], "uv_size": [8 * scale] * 2})

    def test_wearer_gets_original_pixels_back_after_removal_and_can_equip_again(self):
        packets = []
        image = SimpleNamespace(shape=(64, 64, 4), dtype="uint8", tobytes=lambda order: b"a" * 16384)
        skin = SimpleNamespace(id="classic", image=image, cape_id="", cape_image=None)
        head = SimpleNamespace(type=LIVE_HEAD)
        inventory = SimpleNamespace(helmet=head)
        player = SimpleNamespace(unique_id=UUID(int=19), runtime_id=19, id=-19,
                                 name="Tester", xuid="123", game_version="1.26.60",
                                 inventory=inventory, skin=skin,
                                 send_packet=lambda kind, payload: packets.append((kind, payload)))
        manager = SimpleNamespace(get_plugin=lambda name: None)
        skins = SimpleNamespace(index={"skins": {"key": {"width": 64}}}, key_for=lambda data: "key",
                                read_pixels=lambda key: b"h" * 16384)
        plugin = SimpleNamespace(server=SimpleNamespace(online_players=[player], plugin_manager=manager),
                                 _item_data=lambda item: item, _skins=skins)
        worn = WornSkins()
        worn.update(plugin, True)
        worn.update(plugin, True)
        self.assertEqual(len(packets), 1)
        self.assertIn(b"kmheads:worn:", packets[0][1])
        inventory.helmet = None
        worn.update(plugin, True)
        self.assertEqual(len(packets), 2)
        restored = PacketReader(packets[1][1])
        offset = packets[1][1].find(b"kmheads:restore:")
        restored.take(offset - 1)
        self.assertTrue(restored.blob().startswith(b"kmheads:restore:"))
        restored.blob()
        restored.take(8)
        self.assertEqual(restored.blob(), b"a" * 16384)
        inventory.helmet = head
        worn.update(plugin, True)
        self.assertEqual(len(packets), 3)
        self.assertNotEqual(packets[2], packets[0])

    def test_floor_rotation_reads_exact_block_angle(self):
        position = (-3, 70, 4)
        payload = b"".join(map(signed, position)) + b"\x0a\x00"
        payload += b"\x08" + field("id") + field("Skull")
        payload += b"\x05" + field("Rotation") + pack("<f", 67.5) + b"\x00"
        self.assertEqual(skull_rotation(payload), (position, 67.5))
        self.assertEqual(appearance(("Overworld", *position), {"facing_direction": 1}, 67.5)[1], 247.5)
        self.assertEqual(appearance(("Overworld", *position), {"facing_direction": 2}, 67.5)[1], 180)

    def test_worn_skin_only_changes_head_pixels_and_preserves_model(self):
        identity = bytes(16)
        pixels = b"a" * 16384
        patch = '{"geometry":{"default":"geometry.humanoid.customSlim"}}'
        skin = (field("original") + field(patch) + pack("<II", 64, 64) + field(pixels)
                + bytes(9) + field("") * 6 + bytes(12) + field("True") + field(""))
        original = roster_entries(add_list_entry(identity, 19, skin))[0][1]
        changed = merged_entry(original, 64, b"h" * 16384)
        reader = PacketReader(changed)
        reader.take(original[1])
        self.assertTrue(reader.blob().startswith(b"kmheads:worn:"))
        self.assertEqual(reader.blob(), patch.encode())
        self.assertEqual(reader.take(8), pack("<II", 64, 64))
        self.assertEqual(reader.blob(), b"h" * 4096 + pixels[4096:])
        self.assertEqual(changed[2:18], identity)
        self.assertEqual(roster_entries(b"\x01" + changed), [])

    def test_protocol_2225_avatar_metadata(self):
        identity = bytes(16)
        payload = avatar(identity, 1, (0, 65, 0), 0)
        offset = len(identity) + 3 + 36 + 9
        self.assertEqual(payload[offset:offset + 7], bytes.fromhex("04 00 07 07 80 80 10"))
        offset += 7
        for key, expected in ((38, 1.08), (53, 0.001), (54, 0.001)):
            self.assertEqual(payload[offset:offset + 3], bytes((key, 3, 3)))
            self.assertAlmostEqual(unpack("<f", payload[offset + 3:offset + 7])[0], expected)
            offset += 7
        self.assertEqual(payload[offset:offset + 2], bytes(2))

    def test_add_unchanged_and_remove(self):
        packets = []
        player = SimpleNamespace(unique_id="viewer", game_version="1.26.60",
                                 send_packet=lambda kind, body: packets.append((kind, body)))
        key = "a" * 64
        pixels = bytes(64 * 64 * 4)

        class Skins:
            index = {"skins": {key: {"width": 64, "height": 64}}}

            def read_pixels(self, key):
                return pixels

        skins = Skins()
        renderer = LiveSkins()
        position = ("Overworld", 1, 65, 2)
        desired = {position: (key, *appearance(position, {"facing_direction": 2}))}
        renderer.reconcile(player, skins, desired)
        self.assertEqual([kind for kind, _ in packets], [PLAYER_LIST, ADD_PLAYER, PLAYER_LIST])
        self.assertEqual(packets[0][1][3:19], packets[1][1][:16])
        self.assertEqual(packets[1][1][:16], packets[2][1][3:19])
        renderer.reconcile(player, skins, desired)
        self.assertEqual(len(packets), 3)
        renderer.reconcile(player, skins, {})
        self.assertEqual(packets[-1][0], REMOVE_ACTOR)
        self.assertFalse(renderer.visible["viewer"])
