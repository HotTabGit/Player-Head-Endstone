import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from test_plugin import ADAPTER, FakePlayer, FakeDimension, FakeEvent
from test_skins import Image
from endstone_player_heads import skins


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'plugins/player_heads'
        self.plugin = ADAPTER.PlayerHeadsPlugin()
        self.plugin.data_folder = self.folder
        self.plugin._skins = skins.SkinCatalogue(self.folder)
        self.plugin.ready = True
        self.plugin._main_thread = threading.get_ident()
        self.player = FakePlayer(FakeDimension(), name='PrivateName')
        self.player.unique_id = 'unique-runtime-player'
        self.player.runtime_id = 1
        self.player.skin = NS(image=Image(width=0, height=0))
        self.plugin.server = NS(online_mode=True, online_players=[self.player])
        self.warnings = []
        self.plugin.logger = NS(warning=self.warnings.append)
        self.clock = 100.
        timer = patch.object(ADAPTER.time, 'monotonic', lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)

    def test_empty_join_snapshot_retries_fresh_image_and_persists_it(self):
        self.plugin.on_skin_join(FakeEvent(player=self.player))
        self.assertEqual(len(self.plugin._skin_retries), 1)
        self.assertEqual(self.plugin._skins.index['players'], {})
        self.player.skin = NS(image=Image())
        self.clock += 3
        self.plugin._retry_skin_captures()
        self.assertFalse(self.plugin._skin_retries)
        self.assertIn('123', self.plugin._skins.index['players'])
        self.assertEqual(self.plugin._skin_diagnostics['successes'], 1)

    def test_retry_does_not_cross_a_reconnected_runtime_session(self):
        self.plugin.on_skin_join(FakeEvent(player=self.player))
        self.player.runtime_id = 2
        self.player.skin = NS(image=Image())
        self.clock += 3
        self.plugin._retry_skin_captures()
        self.assertFalse(self.plugin._skin_retries)
        self.assertEqual(self.plugin._skins.index['players'], {})

    def test_empty_image_retry_is_bounded_to_three_attempts(self):
        self.plugin.on_skin_join(FakeEvent(player=self.player))
        for _ in range(4):
            self.clock += 4
            self.plugin._retry_skin_captures()
        self.assertFalse(self.plugin._skin_retries)
        self.assertEqual(self.plugin._skin_diagnostics['attempts'], 4)

    def test_structural_size_failure_has_exact_shape_and_no_pointless_retry(self):
        self.player.skin = NS(image=Image(width=128, height=64))
        self.plugin.on_skin_join(FakeEvent(player=self.player))
        self.assertFalse(self.plugin._skin_retries)
        report = json.loads((self.folder / 'skin-diagnostics.json').read_text())
        record = next(iter(report['failures'].values()))
        self.assertEqual(record['shape'], [64, 128, 4])
        self.assertEqual(record['dtype'], 'uint8')
        self.assertEqual(record['reason'], 'invalid_skin_image')
        self.assertEqual(record['context'], 'join')

    def test_diagnostics_omit_name_identity_and_arbitrary_exception_text(self):
        self.plugin._skins = NS(capture=lambda *args: (_ for _ in ()).throw(RuntimeError('PrivateName password=secret /players/123')))
        self.player.skin = NS(image=Image())
        self.assertFalse(self.plugin._capture_skin(self.player))
        text = (self.folder / 'skin-diagnostics.json').read_text()
        text += ''.join(self.warnings)
        for secret in ('PrivateName', 'password', 'secret', '/players/123'):
            self.assertNotIn(secret, text)
        self.assertIn('RuntimeError', text)

    def test_diagnostic_disk_failure_does_not_disable_signed_head_handling(self):
        with patch.object(ADAPTER, 'write_atomic', side_effect=OSError('readonly diagnostic disk')):
            self.assertFalse(self.plugin._capture_skin(self.player))
            self.plugin._flush_skin_diagnostics(force=True)
        self.assertTrue(self.plugin.ready)

    def test_offline_mode_never_captures_or_schedules(self):
        self.plugin.server.online_mode = False
        self.plugin.on_skin_join(FakeEvent(player=self.player))
        self.assertFalse(self.plugin._skin_retries)
        self.assertEqual(self.plugin._skin_diagnostics['attempts'], 0)

    def test_quit_removes_only_matching_authenticated_pending_capture(self):
        self.plugin.on_skin_join(FakeEvent(player=self.player))
        self.plugin._skin_retries[('another', 3)] = {'xuid': '456'}
        self.plugin.on_quit(FakeEvent(player=self.player))
        self.assertEqual(set(self.plugin._skin_retries), {('another', 3)})

    def test_failure_diagnostic_variants_are_bounded(self):
        for width in range(256, 300):
            self.player.skin = NS(image=Image(width=width, height=32))
            self.plugin._capture_skin(self.player)
        self.assertEqual(len(self.plugin._skin_diagnostics['failures']), 32)


if __name__ == '__main__':
    unittest.main()
