from functools import lru_cache
from itertools import count
import json
import math
from struct import pack
from uuid import uuid4

from .skins import SIZES, head_pixels


PROTOCOL = 2225
ADD_PLAYER, REMOVE_ACTOR, PLAYER_LIST = 12, 14, 63
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


def serialize_skin(key, width, height, pixels, version):
    if (width, height) not in SIZES or len(pixels) != width * height * 4:
        raise ValueError("invalid live skin")
    scale = width // 64
    if height < width:
        pixels = pixels.ljust(width * width * 4, b"\0")
        height = width
    name = "geometry.kmheads.live"
    cubes = [
        {"origin": [-4, 0, -4], "size": [8, 8, 8], "uv": [0, 0]},
        {"origin": [-4, 0, -4], "size": [8, 8, 8], "uv": [32 * scale, 0], "inflate": 0.35},
    ]
    geometry = {"format_version": "1.10.0", name: {
        "texturewidth": width, "textureheight": height, "visible_bounds_width": 2,
        "visible_bounds_height": 2, "visible_bounds_offset": [0, 0.25, 0],
        "bones": [{"name": "head", "pivot": [0, 4, 0], "cubes": cubes}],
    }}
    skin_id = "kmheads:" + key
    return b"".join((
        field(skin_id), field(json.dumps({"geometry": {"default": name}})),
        pack("<II", width, height), field(head_pixels(width, height, pixels)),
        bytes(10), field(json.dumps(geometry)), field(version), bytes(2),
        field(skin_id), bytes(7), b"\x01" + bytes(4), field("True"), b"\0",
    ))


def add_list_entry(identity, entity, skin):
    return b"\x01\x01\x00" + identity + signed(entity) + bytes(4) + pack("<i", -1) + skin + bytes(7)


def remove_list_entry(identity):
    return b"\x01\x00\x01" + identity


def avatar(identity, entity, xyz, yaw):
    metadata = b"".join((b"\x04\x00\x07", signed(1 << 17),
                        b"\x26\x03", pack("<f", 1.08),
                        b"\x35\x03", pack("<f", 0.001),
                        b"\x36\x03", pack("<f", 0.001)))
    abilities = pack("<qBBBHIIfff", entity, 1, 0, 1, 1, 0, 0, 0.05, 1, 0.1)
    return b"".join((identity, b"\0", unsigned(entity), b"\0",
                     pack("<9f", *xyz, 0, 0, 0, 0, yaw, yaw), bytes(9), metadata,
                     bytes(2), abilities, bytes(3), pack("<i", -1)))


def appearance(position, states):
    _, x, y, z = position
    face = states.get("minecraft:block_face", {2: "north", 3: "south", 4: "west", 5: "east"}
                      .get(states.get("facing_direction"), "up"))
    offsets = {"north": (0, .25, .24, 180), "south": (0, .25, -.24, 0),
               "west": (.24, .25, 0, 90), "east": (-.24, .25, 0, 270)}
    if face in offsets:
        dx, dy, dz, yaw = offsets[face]
        return (x + .5 + dx, y + dy, z + .5 + dz), yaw
    rotation = states.get("ground_sign_direction", 0)
    return (x + .5, y + (.5 if face == "down" else 0), z + .5), (180 + rotation * 22.5) % 360


class LiveSkins:
    def __init__(self):
        self.visible = {}
        self.ids = count(1 << 52)

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
        return serialize_skin(key, info["width"], info["height"], skins.read_pixels(key), version)

    def update(self, plugin, vanilla, type_name):
        if plugin.server.protocol_version != PROTOCOL:
            return
        candidates = {}
        for position, data in plugin._store.items():
            key = plugin._skins.key_for(data)
            if key is not None:
                dimension, x, _, z = position
                candidates.setdefault((dimension, x >> 4, z >> 4), []).append((position, key))
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
                desired[position] = (key, *appearance(position, block.data.block_states))
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
            token = uuid4().bytes
            identity = token[:8][::-1] + token[8:][::-1]
            entity = next(self.ids)
            player.send_packet(PLAYER_LIST, add_list_entry(identity, entity, skin))
            try:
                player.send_packet(ADD_PLAYER, avatar(identity, entity, xyz, yaw))
                current[position] = (entity, state)
            finally:
                player.send_packet(PLAYER_LIST, remove_list_entry(identity))
