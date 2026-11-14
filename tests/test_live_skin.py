from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endstone_player_heads.live_skin import ADD_PLAYER, PLAYER_LIST, REMOVE_ACTOR, LiveSkins, appearance


class LiveSkinTests(unittest.TestCase):
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
