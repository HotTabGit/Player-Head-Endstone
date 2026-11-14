import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from endstone_player_heads import skins
from endstone_player_heads.model import make_head_data


class Image:
    dtype = 'uint8'

    def __init__(self, color=31, width=64, height=64):
        self.shape = (height, width, 4)
        self.raw = bytes([color, 71, 151, 255]) * width * height

    def tobytes(self, order):
        assert order == 'C'
        return self.raw


class SkinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'plugins/player_heads'
        self.catalogue = skins.SkinCatalogue(self.folder)
        self.world = self.root / 'worlds/Test'
        self.world.mkdir(parents=True)
        (self.root / 'server.properties').write_text('level-name=Test\n', encoding='utf-8')
        self.other = {'pack_id': str(uuid4()), 'version': [4, 3, 2], 'extra': {'keep': True}}
        for kind in ('behavior', 'resource'):
            (self.world / f'world_{kind}_packs.json').write_text(json.dumps([self.other]))

    def refs(self, kind):
        return json.loads((self.world / f'world_{kind}_packs.json').read_text())

    def test_capture_restart_publishes_new_version_and_preserves_other_refs(self):
        self.assertEqual(self.catalogue.build_packs(self.root), [1, skins.PACK_ASSET_REVISION, 0])
        data = make_head_data('123', 'Victim')
        key = self.catalogue.capture('123', Image(), data.head_id)
        self.assertNotIn(key, self.catalogue.startup_keys)
        self.assertEqual(self.refs('behavior')[-1]['version'], [1, skins.PACK_ASSET_REVISION, 0])
        restarted = skins.SkinCatalogue(self.folder)
        self.assertIn(key, restarted.startup_keys)
        self.assertEqual(restarted.build_packs(self.root), [1, skins.PACK_ASSET_REVISION, 1])
        for kind, uid in [('behavior', skins.BP_UUID), ('resource', skins.RP_UUID)]:
            self.assertEqual(self.refs(kind), [self.other, {'pack_id': uid, 'version': [1, skins.PACK_ASSET_REVISION, 1]}])
            manifest = json.loads((self.root / f'{kind}_packs' / skins.PACK_NAME / 'manifest.json').read_text())
            self.assertEqual(manifest['header']['version'], [1, skins.PACK_ASSET_REVISION, 1])
        self.assertEqual(restarted.build_packs(self.root), [1, skins.PACK_ASSET_REVISION, 1])
        backups = list((self.folder / 'skins/pack-ref-backups').glob('*.json'))
        self.assertEqual(len(backups), 4)

    def test_only_active_world_gets_pack_links(self):
        archived = self.root / 'worlds/Archive'
        archived.mkdir()
        original = b'[{"pack_id":"keep","version":[1,0,0]}]'
        for kind in ('behavior', 'resource'):
            (archived / f'world_{kind}_packs.json').write_bytes(original)
        self.catalogue.build_packs(self.root)
        for kind in ('behavior', 'resource'):
            self.assertEqual((archived / f'world_{kind}_packs.json').read_bytes(), original)
            self.assertTrue(any(ref['pack_id'] == (skins.BP_UUID if kind == 'behavior' else skins.RP_UUID)
                                for ref in self.refs(kind)))

    def test_rejects_world_path_traversal_before_writing_packs(self):
        (self.root / 'server.properties').write_text('level-name=../elsewhere\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'invalid_level_name'):
            self.catalogue.build_packs(self.root)
        self.assertFalse((self.root / 'behavior_packs').exists())

    def test_death_snapshot_stays_original_when_player_changes_skin(self):
        original = make_head_data('123', 'Victim')
        first = self.catalogue.capture('123', Image(10), original.head_id)
        second = self.catalogue.capture('123', Image(90))
        self.assertNotEqual(first, second)
        restarted = skins.SkinCatalogue(self.folder)
        self.assertEqual(restarted.key_for(original), first)
        self.assertIsNone(restarted.key_for(make_head_data('123', 'Victim')))
        self.assertEqual(restarted.build_packs(self.root), [1, skins.PACK_ASSET_REVISION, 2])

    def test_png_keeps_head_rgba_and_128px_uv_scales(self):
        image = Image(17, 128, 128)
        key = self.catalogue.capture('123', image)
        self.catalogue.build_packs(self.root)
        png = (self.root / 'resource_packs' / skins.PACK_NAME / 'textures/blocks' / (key + '.png')).read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        position, chunks = 8, {}
        while position < len(png):
            length = struct.unpack('>I', png[position:position+4])[0]
            kind, raw = png[position+4:position+8], png[position+8:position+8+length]
            crc = struct.unpack('>I', png[position+8+length:position+12+length])[0]
            self.assertEqual(crc, zlib.crc32(kind + raw) & 0xffffffff)
            chunks[kind] = raw
            position += 12 + length
        decoded = zlib.decompress(chunks[b'IDAT'])
        pixels = b''.join(decoded[i+1:i+513] for i in range(0, len(decoded), 513))
        limit = 128 * 4 * 32
        self.assertEqual(pixels[:limit], image.raw[:limit])
        self.assertEqual(pixels[limit:], bytes(len(pixels) - limit))
        geo = skins.geometry_document(128, 128)['minecraft:geometry'][0]
        self.assertEqual(geo['bones'][0]['cubes'][0]['uv']['north']['uv'], [16, 16])
        self.assertEqual(geo['bones'][0]['cubes'][1]['uv']['north']['uv'], [80, 16])

    def test_invalid_or_tampered_cache_refused_before_pack_changes(self):
        key = self.catalogue.capture('123', Image())
        cache = self.folder / 'skins' / (key + '.rgba')
        cache.write_bytes(b'x' * cache.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            skins.SkinCatalogue(self.folder)
        self.assertFalse((self.root / 'behavior_packs').exists())

    def test_observed_large_and_tall_atlases_publish_without_changing_pixels(self):
        for width, height in [(256, 256), (64, 128)]:
            with self.subTest(width=width, height=height):
                image = Image(width=width, height=height)
                key = self.catalogue.capture(str(width), image)
                restarted = skins.SkinCatalogue(self.folder)
                self.assertEqual(restarted.read_pixels(key), image.raw)
                restarted.build_packs(self.root)
                geometry = skins.geometry_document(width, height)['minecraft:geometry'][0]
                scale = width // 64
                self.assertEqual(geometry['description']['texture_height'], height)
                north = geometry['bones'][0]['cubes'][0]['uv']['north']
                self.assertEqual(north, {'uv': [8 * scale, 8 * scale], 'uv_size': [8 * scale, 8 * scale]})
                self.assertLessEqual(16 * scale, height)
                png = (self.root / 'resource_packs' / skins.PACK_NAME / 'textures/blocks' / (key + '.png')).read_bytes()
                self.assertEqual(struct.unpack('>II', png[16:24]), (width, height))

    def test_capture_does_not_encode_png_on_tick_thread(self):
        with patch.object(skins, 'png_rgba', side_effect=AssertionError('PNG on tick thread')):
            key = self.catalogue.capture('123', Image(width=256, height=256))
            self.assertEqual(self.catalogue.capture('123', Image(width=256, height=256)), key)

    def test_short_rgba_buffer_is_rejected_before_cache_write(self):
        image = Image(width=256, height=256)
        image.raw = image.raw[:-1]
        with self.assertRaisesRegex(ValueError, 'invalid_skin_image'):
            self.catalogue.capture('123', image)
        self.assertFalse(self.folder.exists())

    def test_oversized_image_rejected_before_copy(self):
        image = Image()
        image.shape = (100000, 100000, 4)
        with patch.object(image, 'tobytes', side_effect=AssertionError('copied')):
            with self.assertRaisesRegex(ValueError, 'invalid_skin_image'):
                self.catalogue.capture('123', image)

    def test_invalid_head_id_does_not_change_catalogue(self):
        with self.assertRaises(ValueError):
            self.catalogue.capture('123', Image(), 'wrong')
        self.assertEqual(self.catalogue.index['skins'], {})

    def test_index_write_failure_can_retry_identical_capture(self):
        actual = skins.write_atomic
        def fail_index(path, raw):
            if Path(path).name == 'index.json':
                raise OSError('index write failed')
            return actual(path, raw)
        with patch.object(skins, 'write_atomic', side_effect=fail_index):
            with self.assertRaises(OSError):
                self.catalogue.capture('123', Image())
        self.assertEqual(self.catalogue.index['skins'], {})
        key = self.catalogue.capture('123', Image())
        self.assertEqual(skins.SkinCatalogue(self.folder).index['players']['123'], key)

    def test_pack_and_refs_roll_back_on_mid_transaction_failure(self):
        self.catalogue.build_packs(self.root)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.catalogue.capture('123', Image())
        restarted = skins.SkinCatalogue(self.folder)
        actual = skins.write_atomic
        failed = False
        def fail_once(path, raw):
            nonlocal failed
            if Path(path) == self.world / 'world_resource_packs.json' and not failed:
                failed = True
                raise OSError('injected reference failure')
            return actual(path, raw)
        with patch.object(skins, 'write_atomic', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'injected'):
                restarted.build_packs(self.root)
        self.assertTrue(failed)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw, str(path))
        self.assertFalse(list((self.root / 'behavior_packs' / skins.PACK_NAME / 'blocks').glob('*.json')))
        self.assertEqual(restarted.build_packs(self.root), [1, skins.PACK_ASSET_REVISION, 1])

    def test_foreign_pack_or_invalid_refs_prevent_all_mutations(self):
        path = self.world / 'world_resource_packs.json'
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'invalid_world_pack_refs'):
            self.catalogue.build_packs(self.root)
        self.assertFalse((self.root / 'behavior_packs').exists())


if __name__ == '__main__':
    unittest.main()
