import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from endstone_player_heads import skins


class VisualPackTests(unittest.TestCase):
    key = 'a' * 64

    def test_every_mount_faces_player_or_away_from_support(self):
        block = skins.block_document(self.key, 64, 64)['minecraft:block']
        self.assertNotIn('y_rotation_offset', block['description']['traits']['minecraft:placement_direction'])
        front_by_rotation = {0: 'north', 90: 'west', 180: 'south', 270: 'east'}
        opposite = {'north': 'south', 'south': 'north', 'east': 'west', 'west': 'east'}
        seen = set()
        for permutation in block['permutations']:
            facing, mount = re.findall(r"== '(\w+)'", permutation['condition'])
            seen.add((facing, mount))
            transformation = permutation['components']['minecraft:transformation']
            rotation = transformation['rotation'][1]
            expected_front = mount if mount in opposite else opposite[facing]
            self.assertEqual(front_by_rotation[rotation], expected_front, (facing, mount))
            offsets = {'north': [0, .25, .25], 'south': [0, .25, -.25],
                       'west': [.25, .25, 0], 'east': [-.25, .25, 0],
                       'up': [0, 0, 0], 'down': [0, .5, 0]}
            self.assertEqual(transformation['translation'], offsets[mount])
        self.assertEqual(len(seen), 24)

    def test_same_id_wearable_replaces_block_item_without_armor_bonus(self):
        item = skins.item_document(self.key)['minecraft:item']
        self.assertEqual(item['description']['identifier'], skins.custom_type(self.key))
        self.assertEqual(item['components']['minecraft:block_placer'], {
            'block': skins.custom_type(self.key), 'replace_block_item': True})
        self.assertEqual(item['components']['minecraft:wearable'], {
            'slot': 'slot.armor.head', 'protection': 0})

    def test_worn_geometry_uses_head_coordinates_and_all_supported_atlases(self):
        for width, height in skins.SIZES:
            with self.subTest(width=width, height=height):
                block = skins.geometry_document(width, height)['minecraft:geometry'][0]
                worn = skins.wearable_geometry_document(width, height)['minecraft:geometry'][0]
                scale = width // 64
                self.assertEqual(set(block['item_display_transforms']), {'gui'})
                self.assertNotIn('item_display_transforms', worn)
                for bone, x in zip(block['bones'], (24, 56)):
                    self.assertEqual(bone['cubes'][0]['uv']['down'],
                                     {'uv': [x * scale, 8 * scale], 'uv_size': [-8 * scale, -8 * scale]})
                self.assertNotEqual(block['description']['identifier'], worn['description']['identifier'])
                self.assertEqual(worn['bones'][0]['name'], 'head')
                self.assertEqual(worn['bones'][0]['pivot'], [0, 24, 0])
                self.assertEqual(worn['bones'][1]['parent'], 'head')
                for worn_bone, block_bone in zip(worn['bones'], block['bones']):
                    cube, base = worn_bone['cubes'][0], block_bone['cubes'][0]
                    self.assertEqual(cube['origin'], [-4, 24, -4])
                    self.assertEqual(cube['uv'], base['uv'])
                    self.assertGreater(cube['inflate'], base.get('inflate', 0))
                for bone in worn['bones']:
                    cube = bone['cubes'][0]
                    for face in cube['uv'].values():
                        self.assertLessEqual(sum((face['uv'][0], face['uv_size'][0])), width)
                        self.assertLessEqual(sum((face['uv'][1], face['uv_size'][1])), height)

    def test_pack_upgrade_refreshes_unchanged_skin_and_all_asset_references_exist(self):
        class Image:
            shape, dtype = (64, 64, 4), 'uint8'
            def tobytes(self, order):
                return bytes([100, 80, 60, 255]) * 64 * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = root / 'worlds' / 'Test'
            world.mkdir(parents=True)
            (root / 'server.properties').write_text('level-name=Test\n')
            catalogue = skins.SkinCatalogue(root / 'plugins' / 'player_heads')
            key = catalogue.capture('123', Image())
            for kind, uid in [('behavior', skins.BP_UUID), ('resource', skins.RP_UUID)]:
                (world / f'world_{kind}_packs.json').write_text(json.dumps([
                    {'pack_id': uid, 'version': [1, 0, 1]}]))
            version = catalogue.build_packs(root)
            self.assertGreater(version, [1, 0, 1])
            bp = root / 'behavior_packs' / skins.PACK_NAME
            rp = root / 'resource_packs' / skins.PACK_NAME
            load = lambda path: json.loads(path.read_text())
            item = load(bp / 'items' / f'{key}.json')['minecraft:item']
            block = load(bp / 'blocks' / f'{key}.json')['minecraft:block']
            attachable = load(rp / 'attachables' / f'{key}.json')['minecraft:attachable']['description']
            worn = load(rp / 'models/entity/head_worn_64_64.geo.json')['minecraft:geometry'][0]
            self.assertEqual(item['description']['identifier'], block['description']['identifier'])
            self.assertEqual(attachable['identifier'], item['description']['identifier'])
            self.assertEqual(attachable['geometry']['default'], worn['description']['identifier'])
            block_geometry = load(rp / 'models/blocks/head_64_64.geo.json')['minecraft:geometry'][0]
            hat = block_geometry['bones'][1]
            self.assertEqual(hat['parent'], 'head')
            self.assertEqual(hat['cubes'][0]['uv']['north']['uv'], [40, 8])
            materials = block['components']['minecraft:material_instances']
            self.assertEqual({entry['render_method'] for entry in materials.values()}, {'blend'})
            self.assertTrue((rp / (attachable['textures']['default'] + '.png')).is_file())
            controllers = load(rp / 'render_controllers/head_worn.json')['render_controllers']
            self.assertTrue(set(attachable['render_controllers']) <= set(controllers))
            for kind in ('behavior', 'resource'):
                self.assertEqual(load(world / f'world_{kind}_packs.json')[0]['version'], version)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(catalogue.build_packs(root), version)
            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
