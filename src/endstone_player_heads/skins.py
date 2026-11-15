from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import zlib

from .model import normalize_xuid

BP_UUID = 'ff3e24aa-3075-4d0f-9e9d-697e02c7a941'
RP_UUID = 'ec88c1a7-7606-4324-a11d-182e81c06a29'
BP_MODULE = '76745b77-d288-4a8c-956b-319775ff9640'
RP_MODULE = '7f743eb9-fc27-4669-8bc2-56095394581c'
PACK_NAME = 'kartexmine_player_heads'
PACK_ASSET_REVISION = 11
LIVE_HEAD = 'kartexheads:live_head'
MAX_SKINS = 512
MAX_HEADS = 8192
MAX_INDEX_BYTES = 3 * 1024 * 1024
SIZES = {(64, 32), (64, 64), (128, 128), (256, 256), (64, 128)}
KEY_RE = re.compile(r'[0-9a-f]{64}')
CUSTOM_RE = re.compile(r'kartexheads:h_[0-9a-f]{64}')


def custom_type(key):
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError('invalid_skin_key')
    return 'kartexheads:h_' + key


def is_custom_type(kind):
    return isinstance(kind, str) and CUSTOM_RE.fullmatch(kind) is not None


def active_world(server_root):
    properties = server_root / 'server.properties'
    name = 'Bedrock level'
    if properties.is_file():
        for line in properties.read_text(encoding='utf-8-sig').splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() == 'level-name':
                name = value.strip()
    if not name or name in ('.', '..') or any(char in name for char in '/\\\x00'):
        raise ValueError('invalid_level_name')
    return safe_path(server_root / 'worlds' / name)


def safe_path(path):
    path = Path(os.path.abspath(path))
    trusted = (os.environ.get('PLAYER_HEADS_TRUSTED_SERVER_ROOT')
               or os.environ.get('KARTEXMINE_TRUSTED_SERVER_ROOT')) if os.name == 'nt' else None
    root = Path(os.path.abspath(trusted)) if trusted else None
    if root is not None and (root != Path.cwd() or not path.is_relative_to(root)):
        raise ValueError('skin_path_outside_trusted_root')
    for part in (path, *path.parents):
        if root is not None and part == root:
            break
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('linked_skin_path')
    return path


def write_atomic(path, raw):
    path = safe_path(path)
    if path.is_file() and path.read_bytes() == raw:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            name = Path(f.name)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, indent=2) + '\n').encode('utf-8')


class SkinJsonError(ValueError):
    pass


def decode_skin_json(raw, path, *, empty_pack_list=False):
    try:
        text = raw.decode('utf-8-sig')
        if empty_pack_list and not text.strip():
            return []
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkinJsonError(f'{path}: {exc.msg}, строка {exc.lineno}, столбец {exc.colno}') from exc
    except UnicodeDecodeError as exc:
        raise SkinJsonError(f'{path}: ожидается UTF-8') from exc


def png_rgba(width, height, pixels):
    if (width, height) not in SIZES or type(pixels) is not bytes or len(pixels) != width * height * 4:
        raise ValueError('invalid_skin_image')
    def chunk(kind, content):
        return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', zlib.crc32(kind + content) & 0xffffffff)
    stride = width * 4
    scanlines = b''.join(b'\0' + pixels[y * stride:(y + 1) * stride] for y in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(scanlines, 9)) + chunk(b'IEND', b''))


def image_snapshot(image, allow_placeholder=False):
    shape = tuple(image.shape)
    if len(shape) != 3 or shape[2] != 4 or str(image.dtype) != 'uint8':
        raise ValueError('skin_requires_uint8_rgba')
    height, width, _ = shape
    if (width, height) not in SIZES and not (allow_placeholder and (width, height) == (1, 1)):
        raise ValueError('invalid_skin_image')
    raw = image.tobytes(order='C')
    if type(raw) is not bytes or len(raw) != width * height * 4:
        raise ValueError('invalid_skin_image')
    return width, height, raw


def head_pixels(width, height, raw):
    stride = width * 4
    head_height = 16 * (width // 64)
    return raw[:stride * head_height].ljust(stride * height, b'\0')


def classic_head_atlas(width, raw):
    if width != 256:
        return True
    side = front = 0
    for y in range(32, 64):
        row = y * width * 4
        side += sum(a != 0 for a in raw[row + 3:row + 128:4])
        front += sum(a != 0 for a in raw[row + 131:row + 256:4])
    return side != 0 or front >= 512


def remap_head_atlas(raw, origin):
    x, y = origin
    result = bytearray(256 * 256 * 4)
    for dx, dy, length in ((8, 0, 16), (0, 8, 32)):
        for row in range(8):
            source = ((y + dy + row) * 256 + x + dx) * 4
            line = raw[source:source + length * 4]
            if not all(line[3::4]):
                raise ValueError('incomplete_persona_head')
            line = b''.join(line[i:i + 4] * 4 for i in range(0, len(line), 4))
            for repeat in range(4):
                target = (((dy + row) * 4 + repeat) * 256 + dx * 4) * 4
                result[target:target + len(line)] = line
    return bytes(result)


def matches_padded_persona_head(raw):
    stride = 256 * 4
    for y in range(9):
        offset = y * stride
        alpha = raw[offset + 3:offset + 34 * 4:4]
        if alpha != bytes(9) + b'\xff' * 16 + bytes(9):
            return False
    if raw[9 * 4:25 * 4] != raw[stride + 9 * 4:stride + 25 * 4]:
        return False
    for y in range(9, 17):
        offset = y * stride
        row = raw[offset:offset + 34 * 4]
        if row[3::4] != b'\xff' * 34 or row[:4] != row[4:8] or row[-4:] != row[-8:-4]:
            return False
    return not any(raw[17 * stride + 3:17 * stride + 34 * 4:4])


def image_metadata(image):
    result = {'shape': None, 'dtype': None}
    try:
        shape = tuple(image.shape)
        if len(shape) <= 4 and all(type(v) is int and 0 <= v <= 1000000 for v in shape):
            result['shape'] = list(shape)
    except Exception:
        pass
    try:
        dtype = str(image.dtype)
        if re.fullmatch(r'[a-zA-Z0-9_<>|=\-]{1,32}', dtype):
            result['dtype'] = dtype
    except Exception:
        pass
    return result


def capture_failure(error, image, context):
    message = str(error)
    reason = message if re.fullmatch(r'[a-z_]{1,80}', message) else type(error).__name__
    return {**image_metadata(image), 'reason': reason,
            'exception': type(error).__name__,
            'context': context if context in ('join', 'join_retry', 'skin_change', 'death', 'give', 'other') else 'other'}


def geometry_document(width, height):
    scale = width // 64
    def uv(offset):
        points = {'north': (8 + offset, 8), 'south': (24 + offset, 8),
                  'east': (0 + offset, 8), 'west': (16 + offset, 8),
                  'up': (8 + offset, 0)}
        faces = {face: {'uv': [x * scale, y * scale], 'uv_size': [8 * scale, 8 * scale]}
                 for face, (x, y) in points.items()}
        faces['down'] = {'uv': [(24 + offset) * scale, 8 * scale],
                         'uv_size': [-8 * scale, -8 * scale]}
        return faces
    return {'format_version': '1.21.130', 'minecraft:geometry': [{
        'description': {'identifier': f'geometry.kartexheads.head_{width}_{height}',
                        'texture_width': width, 'texture_height': height,
                        'visible_bounds_width': 2, 'visible_bounds_height': 2, 'visible_bounds_offset': [0, .5, 0]},
        'item_display_transforms': {'gui': {'rotation': [30, 225, 0],
                                            'translation': [0, 2, 0],
                                            'scale': [.88, .88, .88], 'fit_to_frame': False}},
        'bones': [
            {'name': 'head', 'pivot': [0, 4, 0], 'cubes': [
                {'origin': [-4, 0, -4], 'size': [8, 8, 8], 'uv': uv(0)}]},
            {'name': 'hat', 'parent': 'head', 'pivot': [0, 4, 0], 'cubes': [
                {'origin': [-4, 0, -4], 'size': [8, 8, 8], 'inflate': .5, 'uv': uv(32)}]},
        ]}]}


def block_document(key, width, height):
    texture = 'kmhead_' + key
    components = {
        'minecraft:geometry': {'identifier': f'geometry.kartexheads.head_{width}_{height}'},
        'minecraft:material_instances': {
            '*': {'texture': texture, 'render_method': 'blend',
                  'ambient_occlusion': False, 'face_dimming': False},
        },
        'minecraft:collision_box': {'origin': [-4, 0, -4], 'size': [8, 8, 8]},
        'minecraft:selection_box': {'origin': [-4, 0, -4], 'size': [8, 8, 8]},
        'minecraft:destructible_by_mining': {'seconds_to_destroy': .5},
        'minecraft:destructible_by_explosion': {'explosion_resistance': 1},
        'minecraft:movable': {'movement_type': 'immovable'},
        'minecraft:loot': 'loot_tables/empty.json',
        'minecraft:light_dampening': 0,
    }
    permutations = []
    translations = {'up': [0, 0, 0], 'down': [0, .5, 0], 'north': [0, .25, .25],
                    'south': [0, .25, -.25], 'west': [.25, .25, 0], 'east': [-.25, .25, 0]}
    angles = {'north': 0, 'east': 270, 'south': 180, 'west': 90}
    for facing, angle in angles.items():
        for face, translation in translations.items():
            rotation = angles[face] if face in angles else (angle + 180) % 360
            permutations.append({'condition': f"query.block_state('minecraft:cardinal_direction') == '{facing}' && query.block_state('minecraft:block_face') == '{face}'",
                                 'components': {'minecraft:transformation': {'rotation': [0, rotation, 0], 'translation': translation}}})
    return {'format_version': '1.21.100', 'minecraft:block': {
        'description': {'identifier': custom_type(key),
                        'traits': {'minecraft:placement_direction': {'enabled_states': ['minecraft:cardinal_direction']},
                                   'minecraft:placement_position': {'enabled_states': ['minecraft:block_face']}}},
        'components': components, 'permutations': permutations}}


def item_document(key):
    kind = custom_type(key)
    return {'format_version': '1.21.100', 'minecraft:item': {
        'description': {'identifier': kind},
        'components': {
            'minecraft:block_placer': {'block': kind, 'replace_block_item': True},
            'minecraft:wearable': {'slot': 'slot.armor.head', 'protection': 0}}}}


def live_item_document():
    return {'format_version': '1.21.100', 'minecraft:item': {
        'description': {'identifier': LIVE_HEAD},
        'components': {
            'minecraft:block_placer': {'block': 'minecraft:player_head'},
            'minecraft:wearable': {'slot': 'slot.armor.head', 'protection': 0}}}}


def wearable_geometry_document(width, height):
    document = geometry_document(width, height)
    geometry = document['minecraft:geometry'][0]
    del geometry['item_display_transforms']
    geometry['description'].update({
        'identifier': f'geometry.kartexheads.worn_{width}_{height}',
        'visible_bounds_height': 3, 'visible_bounds_offset': [0, 1, 0]})
    head = geometry['bones'][0]
    document['format_version'] = '1.16.0'
    head['binding'] = "'head'"
    for bone, inflate in zip(geometry['bones'], (.5, .85)):
        bone['pivot'] = [0, 24, 0]
        cube = bone['cubes'][0]
        cube['origin'] = [-4, 24, -4]
        cube['inflate'] = inflate
    return document


def attachable_document(key, width, height):
    return {'format_version': '1.20.30', 'minecraft:attachable': {'description': {
        'identifier': custom_type(key),
        'materials': {'default': 'entity_alphatest'},
        'textures': {'default': 'textures/blocks/' + key},
        'geometry': {'default': f'geometry.kartexheads.worn_{width}_{height}'},
        'render_controllers': ['controller.render.kartexheads.worn']}}}


class SkinCatalogue:
    def __init__(self, folder):
        self.folder = safe_path(Path(folder) / 'skins')
        self._persona_origins = {}
        overrides = safe_path(self.folder / 'persona-atlases.json')
        if overrides.is_file() and overrides.stat().st_size <= 4096:
            entries = decode_skin_json(overrides.read_bytes(), 'plugins/player_heads/skins/persona-atlases.json')
            if isinstance(entries, dict):
                self._persona_origins = {
                    key: origin for key, origin in entries.items()
                    if KEY_RE.fullmatch(key) and isinstance(origin, list) and len(origin) == 2
                    and all(type(coord) is int and 0 <= coord <= limit
                            for coord, limit in zip(origin, (224, 240)))
                }
        self.index = {'version': 1, 'skins': {}, 'players': {}, 'heads': {}}
        path = safe_path(self.folder / 'index.json')
        if path.exists():
            if path.stat().st_size > MAX_INDEX_BYTES:
                raise ValueError('skin_index_too_large')
            index = decode_skin_json(path.read_bytes(), 'plugins/player_heads/skins/index.json')
            if not isinstance(index, dict) or set(index) != set(self.index) or index['version'] != 1:
                raise ValueError('invalid_skin_index')
            for section, cap in [('skins', MAX_SKINS), ('players', MAX_HEADS), ('heads', MAX_HEADS)]:
                if not isinstance(index[section], dict) or len(index[section]) > cap:
                    raise ValueError('invalid_skin_index_size')
            for key, entry in index['skins'].items():
                custom_type(key)
                if not isinstance(entry, dict) or set(entry) != {'width', 'height'} or (entry['width'], entry['height']) not in SIZES:
                    raise ValueError('invalid_cached_skin')
                self.read_pixels(key, entry)
            for xuid, key in index['players'].items():
                if normalize_xuid(xuid) != xuid or key not in index['skins']:
                    raise ValueError('invalid_skin_player')
            for head_id, key in index['heads'].items():
                from uuid import UUID
                if str(UUID(head_id)) != head_id or key not in index['skins']:
                    raise ValueError('invalid_skin_head')
            self.index = index
        self.startup_keys = frozenset(self.index['skins'])

    def read_pixels(self, key, entry=None):
        custom_type(key)
        entry = self.index['skins'][key] if entry is None else entry
        path = safe_path(self.folder / (key + '.rgba'))
        if path.stat().st_size != entry['width'] * entry['height'] * 4:
            raise ValueError('invalid_cached_skin_size')
        raw = path.read_bytes()
        digest = hashlib.sha256(struct.pack('>II', entry['width'], entry['height']) + raw).hexdigest()
        if key != digest:
            raise ValueError('cached_skin_hash_mismatch')
        return raw

    def _save(self):
        write_atomic(self.folder / 'index.json', json_bytes(self.index))

    def render_pixels(self, key):
        raw = self.read_pixels(key)
        origin = self._persona_origins.get(key)
        if origin is not None and len(raw) == 256 * 256 * 4:
            return remap_head_atlas(raw, origin)
        return raw

    def capture(self, xuid, image, head_id=None, *, character_creator=False, static_snapshot=None):
        xuid = normalize_xuid(xuid)
        if head_id is not None:
            from uuid import UUID
            if str(UUID(head_id)) != head_id:
                raise ValueError('invalid_skin_head')
            if head_id not in self.index['heads'] and len(self.index['heads']) >= MAX_HEADS:
                raise ValueError('skin_head_catalogue_full')
        if xuid not in self.index['players'] and len(self.index['players']) >= MAX_HEADS:
            raise ValueError('skin_player_catalogue_full')
        width, height, raw = image_snapshot(image, allow_placeholder=static_snapshot is not None)
        if static_snapshot is not None:
            source_digest, static_pixels = static_snapshot
            if hashlib.sha256(raw).digest() == source_digest:
                if (width, height) == (1, 1) and len(static_pixels) == 64 * 64 * 4:
                    width = height = 64
                if len(static_pixels) == width * height * 4:
                    raw = static_pixels
                    character_creator = False
        if (width, height) not in SIZES:
            raise ValueError('invalid_skin_image')
        key = hashlib.sha256(struct.pack('>II', width, height) + raw).hexdigest()
        origin = self._persona_origins.get(key)
        if origin is not None and width == height == 256:
            remap_head_atlas(raw, origin)
        elif character_creator or not classic_head_atlas(width, raw):
            if width != 256 or height != 256 or not matches_padded_persona_head(raw):
                raise ValueError('unsupported_head_atlas')
            raw = remap_head_atlas(raw, (1, 1))
            key = hashlib.sha256(struct.pack('>II', width, height) + raw).hexdigest()
        updated = {section: dict(value) if isinstance(value, dict) else value for section, value in self.index.items()}
        if key not in self.index['skins']:
            if len(self.index['skins']) >= MAX_SKINS:
                raise ValueError('skin_catalogue_full')
            write_atomic(self.folder / (key + '.rgba'), raw)
            updated['skins'][key] = {'width': width, 'height': height}
        changed = self.index['players'].get(xuid) != key
        updated['players'][xuid] = key
        if head_id is not None:
            changed |= self.index['heads'].get(head_id) != key
            updated['heads'][head_id] = key
        if changed:
            write_atomic(self.folder / 'index.json', json_bytes(updated))
            self.index = updated
        return key

    def key_for(self, data):
        return self.index['heads'].get(data.head_id)

    def build_packs(self, server_root):
        server_root = safe_path(server_root)
        assets = {'behavior': {}, 'resource': {}}
        keys = sorted(self.index['skins'])
        version = [1, PACK_ASSET_REVISION, len(keys)]
        for kind, pack_uuid, module_uuid, module_type in [('behavior', BP_UUID, BP_MODULE, 'data'),
                                                        ('resource', RP_UUID, RP_MODULE, 'resources')]:
            manifest = {'format_version': 2, 'header': {'name': 'Player Heads',
                'description': 'Captured player head skins; restart to publish new captures',
                'uuid': pack_uuid, 'version': version, 'min_engine_version': [1, 21, 100]},
                'modules': [{'type': module_type, 'uuid': module_uuid, 'version': version}]}
            if kind == 'behavior':
                manifest['dependencies'] = [{'uuid': RP_UUID, 'version': version}]
            assets[kind]['manifest.json'] = json_bytes(manifest)
        assets['behavior']['loot_tables/empty.json'] = json_bytes({'pools': []})
        assets['behavior']['items/live_head.json'] = json_bytes(live_item_document())
        assets['resource']['render_controllers/head_worn.json'] = json_bytes({
            'format_version': '1.8.0', 'render_controllers': {
                'controller.render.kartexheads.worn': {
                    'geometry': 'Geometry.default',
                    'materials': [{'*': 'Material.default'}],
                    'textures': ['Texture.default']}}})
        textures = {}
        blocks = {'format_version': [1, 1, 0]}
        for key in keys:
            entry = self.index['skins'][key]
            width, height = entry['width'], entry['height']
            assets['behavior']['blocks/' + key + '.json'] = json_bytes(block_document(key, width, height))
            assets['behavior']['items/' + key + '.json'] = json_bytes(item_document(key))
            assets['resource'][f'models/blocks/head_{width}_{height}.geo.json'] = json_bytes(geometry_document(width, height))
            assets['resource'][f'models/entity/head_worn_{width}_{height}.geo.json'] = json_bytes(wearable_geometry_document(width, height))
            assets['resource']['attachables/' + key + '.json'] = json_bytes(attachable_document(key, width, height))
            pixels = head_pixels(width, height, self.render_pixels(key))
            assets['resource']['textures/blocks/' + key + '.png'] = png_rgba(width, height, pixels)
            textures['kmhead_' + key] = {'textures': 'textures/blocks/' + key}
            blocks[custom_type(key)] = {'sound': 'stone'}
        assets['resource']['textures/terrain_texture.json'] = json_bytes({
            'resource_pack_name': 'kartexmine_player_heads', 'texture_name': 'atlas.terrain',
            'padding': 8, 'num_mip_levels': 4, 'texture_data': textures})
        assets['resource']['blocks.json'] = json_bytes(blocks)
        changes = {}
        for kind, files in assets.items():
            pack = safe_path(server_root / (kind + '_packs') / PACK_NAME)
            existing = pack / 'manifest.json'
            if pack.exists():
                if not existing.is_file() or decode_skin_json(
                        existing.read_bytes(), existing.relative_to(server_root))['header']['uuid'] != (BP_UUID if kind == 'behavior' else RP_UUID):
                    raise ValueError('refuse_foreign_head_pack')
            for path, raw in files.items():
                changes[safe_path(pack / path)] = raw
        references = {}
        world = active_world(server_root)
        if world.is_dir():
            for kind, uid in [('behavior', BP_UUID), ('resource', RP_UUID)]:
                path = safe_path(world / ('world_' + kind + '_packs.json'))
                if path.exists() and not path.is_file():
                    raise ValueError('invalid_world_pack_ref_file')
                raw = path.read_bytes() if path.is_file() else None
                refs = decode_skin_json(raw, path.relative_to(server_root), empty_pack_list=True) if raw is not None else []
                if not isinstance(refs, list) or any(not isinstance(ref, dict) or not isinstance(ref.get('pack_id'), str) for ref in refs):
                    raise ValueError('invalid_world_pack_refs')
                matches = [ref for ref in refs if ref['pack_id'] == uid]
                if len(matches) > 1:
                    raise ValueError('duplicate_head_pack_ref')
                if matches:
                    matches[0]['version'] = version
                else:
                    refs.append({'pack_id': uid, 'version': version})
                references[path] = raw
                changes[path] = json_bytes(refs)
        changes[self.folder / 'pack-state.json'] = json_bytes({'version': version, 'keys': keys, 'bp_uuid': BP_UUID, 'rp_uuid': RP_UUID})
        before = {path: path.read_bytes() if path.is_file() else None for path in changes}
        for path, raw in references.items():
            if raw is not None and raw != changes[path]:
                token = hashlib.sha256(path.relative_to(server_root).as_posix().encode() + b'\0' + raw).hexdigest()
                write_atomic(self.folder / 'pack-ref-backups' / (token + '.json'), raw)
        applied = []
        try:
            for path, raw in changes.items():
                if before[path] != raw:
                    write_atomic(path, raw)
                    applied.append(path)
        except Exception:
            for path in reversed(applied):
                if before[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    write_atomic(path, before[path])
            raise
        return version
