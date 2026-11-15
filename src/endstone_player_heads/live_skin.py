from functools import lru_cache
from itertools import count
import json
import math
from struct import pack, unpack
from uuid import uuid4

from .skins import SIZES, classic_head_atlas, head_pixels


PROTOCOL = 2225
ADD_PLAYER, REMOVE_ACTOR, BLOCK_ACTOR_DATA, PLAYER_LIST = 12, 14, 56, 63
PLAYER_SKIN = 93
DISTANCE, MAX_VISIBLE, MAX_NEW = 24, 12, 2


def unsigned(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def signed(value):
    return unsigned((value << 1) ^ (value >> 63))


def field(value):
    if isinstance(value, str):
        value = value.encode()
    return unsigned(len(value)) + value


def wire_uuid(value):
    raw = value.bytes
    return raw[:8][::-1] + raw[8:][::-1]


class PacketReader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, length):
        end = self.offset + length
        if end > len(self.data):
            raise ValueError("truncated packet")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def unsigned(self, bits=32):
        value = 0
        for shift in range(0, bits, 7):
            byte = self.take(1)[0]
            value |= (byte & 127) << shift
            if byte < 128 and value < 1 << bits:
                return value
        raise ValueError("invalid varint")

    def signed(self, bits=32):
        value = self.unsigned(bits)
        return (value >> 1) ^ -(value & 1)

    def blob(self):
        return self.take(self.unsigned())


def skull_rotation(payload):
    reader = PacketReader(payload)
    position = tuple(reader.signed() for _ in range(3))
    if len(payload) > 512 or reader.take(1) != b"\x0a":
        return None
    reader.blob()
    skull, degrees, legacy = False, None, None
    while kind := reader.take(1)[0]:
        name = reader.blob()
        if kind == 8:
            value = reader.blob()
            if name == b"id":
                skull = value == b"Skull"
        elif kind in (3, 4):
            reader.unsigned(32 if kind == 3 else 64)
        elif kind in (1, 2, 5, 6):
            value = reader.take({1: 1, 2: 2, 5: 4, 6: 8}[kind])
            if kind == 5 and name == b"Rotation":
                degrees = unpack("<f", value)[0]
            if kind == 1 and name == b"Rot" and value[0] < 16:
                legacy = value[0] * 22.5
        else:
            return None
    rotation = degrees if degrees is not None else legacy
    if skull and rotation is not None and math.isfinite(rotation) and reader.offset == len(payload):
        return position, rotation % 360
    return None


def serialize_skin(key, width, height, pixels, version):
    if (width, height) not in SIZES or len(pixels) != width * height * 4:
        raise ValueError("invalid live skin")
    scale = width // 64
    if scale == 1:
        pixels = head_pixels(64, 64, pixels)
    else:
        rows = []
        for y in range(16):
            row = y * scale * width * 4
            rows.append(b"".join(pixels[row + x * scale * 4:row + x * scale * 4 + 4] for x in range(64)))
        pixels = b"".join(rows).ljust(64 * 64 * 4, b"\0")
    top = [pixels[(y * 64 + 8) * 4:(y * 64 + 16) * 4] for y in range(8)]
    bottom = [(y * 64 + 16) * 4 for y in range(8)]
    if all(row[3::4] == b"\xff" * 8 for row in top) and not any(
            alpha for start in bottom for alpha in pixels[start + 3:start + 32:4]):
        pixels = bytearray(pixels)
        for start, row in zip(bottom, top):
            pixels[start:start + 32] = row
    pixels = bytearray(pixels)
    for y in range(8):
        for x in (16, 48):
            start = (y * 64 + x) * 4
            target = ((y + 32) * 64 + x) * 4
            pixels[target:target + 32] = pixels[start:start + 32]
            pixels[start:start + 32] = bytes(32)
    name = "geometry.kmheads.live_skull"
    geometry = {"format_version": "1.10.0", name: {
        "texturewidth": 64, "textureheight": 64, "visible_bounds_width": 2,
        "visible_bounds_height": 1, "visible_bounds_offset": [0, 0, 0],
        "bones": [
            {"name": "head", "pivot": [0, 24, 0], "cubes": [
                {"origin": [-4, 0, -4], "size": [8, 8, 8], "uv": [0, 0]},
                {"origin": [-4, -.125, -4], "size": [8, 1, 8], "uv": [0, 32]}]},
            {"name": "hat", "parent": "head", "pivot": [0, 24, 0], "cubes": [
                {"origin": [-4, 0, -4], "size": [8, 8, 8], "uv": [32, 0], "inflate": 0.5},
                {"origin": [-4, -.125, -4], "size": [8, 1, 8], "uv": [32, 32], "inflate": 0.5}]},
        ],
    }}
    skin_id = "kmheads:skull:v6:" + key
    return b"".join((
        field(skin_id), field(json.dumps({"geometry": {"default": name}})),
        pack("<II", 64, 64), field(pixels),
        bytes(10), field(json.dumps(geometry)), field(version), bytes(2),
        field(skin_id), bytes(7), b"\x01\x00\x00\x00\x01", field("True"), b"\0",
    ))


def add_list_entry(identity, entity, skin):
    return b"\x01\x01\x00" + identity + signed(entity) + bytes(4) + pack("<i", -1) + skin + bytes(7)


def remove_list_entry(identity):
    return b"\x01\x00\x01" + identity


def avatar(identity, entity, xyz, yaw):
    entries = ((0, 7, signed(1 << 17)), (38, 3, pack("<f", 1.08)),
               (53, 3, pack("<f", 0.001)), (54, 3, pack("<f", 0.001)))
    metadata = unsigned(len(entries)) + b"".join(
        unsigned(key) + unsigned(kind) + bytes((kind,)) + value
        for key, kind, value in entries
    )
    abilities = pack("<qBBBHIIfff", entity, 1, 0, 1, 1, 0, 0, 0.05, 1, 0.1)
    return b"".join((identity, b"\0", unsigned(entity), b"\0",
                     pack("<9f", *xyz, 0, 0, 0, 0, yaw, yaw), bytes(9), metadata,
                     bytes(2), abilities, bytes(3), pack("<i", -1)))


def appearance(position, states, rotation=None):
    _, x, y, z = position
    face = states.get("minecraft:block_face", {2: "north", 3: "south", 4: "west", 5: "east"}
                      .get(states.get("facing_direction"), "up"))
    offsets = {"north": (0, .25, .24, 180), "south": (0, .25, -.24, 0),
               "west": (.24, .25, 0, 90), "east": (-.24, .25, 0, 270)}
    if face in offsets:
        dx, dy, dz, yaw = offsets[face]
        return (x + .5 + dx, y + dy, z + .5 + dz), yaw
    if rotation is None:
        rotation = states.get("ground_sign_direction", 0) * 22.5
    return (x + .5, y + (.5 if face == "down" else 0), z + .5), (180 + rotation) % 360


class LiveSkins:
    def __init__(self):
        self.visible = {}
        self.ids = count(1 << 52)
        self.rotations = {}

    def forget(self, player):
        if self.visible:
            self.visible.pop(player.unique_id, None)

    def clear(self, player):
        current = self.visible.get(player.unique_id, {})
        for position, (entity, _) in tuple(current.items()):
            player.send_packet(REMOVE_ACTOR, signed(entity))
            del current[position]
        self.visible.pop(player.unique_id, None)

    @lru_cache(maxsize=32)
    def skin_bytes(self, skins, key, version):
        info = skins.index["skins"][key]
        pixels = skins.render_pixels(key)
        if not classic_head_atlas(info["width"], pixels):
            return None
        return serialize_skin(key, info["width"], info["height"], pixels, version)

    def update(self, plugin, vanilla, type_name):
        if plugin.server.protocol_version != PROTOCOL:
            return
        candidates = {}
        registered = {}
        for position, data in plugin._store.items():
            key = plugin._skins.key_for(data)
            registered[position] = data.head_id
            if key is not None:
                dimension, x, _, z = position
                candidates.setdefault((dimension, x >> 4, z >> 4), []).append((position, key))
        self.rotations = {position: value for position, value in self.rotations.items()
                          if registered.get(position) == value[0]}
        loaded = {}
        for player in plugin.server.online_players:
            where = player.location
            dim = where.dimension
            if dim.name not in loaded:
                loaded[dim.name] = plugin._loaded(dim)
            cx, cz = math.floor(where.x) >> 4, math.floor(where.z) >> 4
            nearby = []
            for dx in range(-2, 3):
                for dz in range(-2, 3):
                    chunk = (cx + dx, cz + dz)
                    if chunk not in loaded[dim.name]:
                        continue
                    for position, key in candidates.get((dim.name, *chunk), ()):
                        _, x, y, z = position
                        squared = (x + .5 - where.x)**2 + (y - where.y)**2 + (z + .5 - where.z)**2
                        if squared <= DISTANCE**2:
                            nearby.append((squared, position, key))
            desired = {}
            for _, position, key in sorted(nearby):
                block = dim.get_block_at(*position[1:])
                if position in plugin._pending_explosions or block is None or type_name(block) not in vanilla:
                    continue
                _, rotation = self.rotations.get(position, (None, None))
                desired[position] = (key, *appearance(position, block.data.block_states, rotation))
                if len(desired) >= MAX_VISIBLE:
                    break
            self.reconcile(player, plugin._skins, desired)

    def reconcile(self, player, skins, desired):
        current = self.visible.setdefault(player.unique_id, {})
        for position, (entity, state) in tuple(current.items()):
            if desired.get(position) != state:
                player.send_packet(REMOVE_ACTOR, signed(entity))
                del current[position]
        pending = [(position, state) for position, state in desired.items() if position not in current]
        for position, state in pending[:MAX_NEW]:
            key, xyz, yaw = state
            skin = self.skin_bytes(skins, key, player.game_version)
            if skin is None:
                continue
            identity = wire_uuid(uuid4())
            entity = next(self.ids)
            player.send_packet(PLAYER_LIST, add_list_entry(identity, entity, skin))
            try:
                player.send_packet(ADD_PLAYER, avatar(identity, entity, xyz, yaw))
                current[position] = (entity, state)
            finally:
                player.send_packet(PLAYER_LIST, remove_list_entry(identity))
