from functools import lru_cache
from hashlib import sha256
import json
from struct import pack, unpack

from .live_skin import PLAYER_LIST, PacketReader, field, signed, unsigned, wire_uuid
from .skins import LIVE_HEAD, SIZES, image_snapshot


def roster_entries(payload):
    reader = PacketReader(payload)
    count = reader.unsigned()
    if count > 128:
        raise ValueError("oversize roster")
    entries = []
    for _ in range(count):
        start = reader.offset
        action = reader.take(2)
        identity = reader.take(16)
        if action == b"\x00\x01":
            entries.append((identity, None))
            continue
        if action != b"\x01\x00":
            raise ValueError("unknown roster entry")
        reader.signed(64)
        for _ in range(4):
            reader.blob()
        reader.take(4)
        skin_start = reader.offset
        skin_id = reader.blob()
        reader.blob()
        reader.take(8)
        reader.blob()
        animations = reader.unsigned()
        if animations > 64:
            raise ValueError("oversize animations")
        for _ in range(animations):
            reader.take(8)
            reader.blob()
            reader.unsigned()
            reader.take(4)
            reader.unsigned()
        reader.take(8)
        for _ in range(6):
            reader.blob()
        reader.take(5)
        pieces = reader.unsigned()
        if pieces > 256:
            raise ValueError("oversize persona")
        for _ in range(pieces):
            reader.blob()
            reader.take(21)
            reader.blob()
        tints = reader.unsigned()
        if tints > 256:
            raise ValueError("oversize persona tints")
        for _ in range(tints):
            reader.blob()
            reader.take(16)
        flags = reader.take(5)
        reader.blob()
        reader.blob()
        reader.take(7)
        if skin_id.startswith(b"kmheads:"):
            continue
        classic = not (animations or pieces or tints or flags[1])
        if classic:
            entries.append((identity, (payload[start:reader.offset], skin_start - start)))
        else:
            entries.append((identity, None))
    if reader.offset != len(payload):
        raise ValueError("trailing roster data")
    return entries


@lru_cache(maxsize=16)
def merged_entry(original, source_width, source_pixels, revision=0):
    raw, start = original
    reader = PacketReader(raw)
    reader.take(start)
    skin_id = reader.blob()
    patch = reader.blob()
    if b"geometry.humanoid.custom" not in patch:
        return None
    dimensions = reader.take(8)
    width, height = unpack("<II", dimensions)
    pixels = reader.blob()
    if (width, height) not in SIZES or len(pixels) != width * height * 4:
        return None
    if source_width == width:
        head = source_pixels[:width * width]
    else:
        head = bytearray()
        for y in range(width // 4):
            row = y * source_width // width * source_width
            for x in range(width):
                offset = (row + x * source_width // width) * 4
                head.extend(source_pixels[offset:offset + 4])
        head = bytes(head)
    if len(head) != width * width:
        return None
    identifier = b"kmheads:worn:" + sha256(skin_id + pixels + head + str(revision).encode()).hexdigest().encode()
    if json.loads(patch).get("geometry", {}).get("default") not in (
            "geometry.humanoid.custom", "geometry.humanoid.customSlim"):
        return None
    return replace_skin(raw, start, reader, identifier, patch, dimensions, head + pixels[len(head):])


def restore_entry(original, revision):
    raw, start = original
    reader = PacketReader(raw)
    reader.take(start)
    skin_id = reader.blob()
    patch = reader.blob()
    dimensions = reader.take(8)
    pixels = reader.blob()
    identifier = b"kmheads:restore:" + sha256(skin_id + pixels + str(revision).encode()).hexdigest().encode()
    return replace_skin(raw, start, reader, identifier, patch, dimensions, pixels)


def replace_skin(raw, start, reader, identifier, patch, dimensions, pixels):
    end_of_pixels = reader.offset
    reader.unsigned()
    reader.take(8)
    for _ in range(5):
        reader.blob()
    full_id = reader.offset
    reader.blob()
    end_of_full_id = reader.offset
    reader.take(11)
    override = reader.offset
    return (raw[:start] + field(identifier) + field(patch) + dimensions + field(pixels)
            + raw[end_of_pixels:full_id] + field(identifier) + raw[end_of_full_id:override]
            + b"\x01" + raw[override + 1:])


class WornSkins:
    def __init__(self):
        self.roster = {}
        self.applied = {}
        self.local_roster = {}
        self.sending = False
        self.updates = 0
        self.revision = 0

    def observe(self, viewer, payload):
        if self.sending or len(payload) > 4 * 1024 * 1024:
            return
        for identity, entry in roster_entries(payload):
            pair = (viewer, identity)
            if entry is None:
                self.roster.pop(pair, None)
                if pair[0] != pair[1]:
                    self.applied.pop(pair, None)
            elif len(entry[0]) <= 350000 and (len(self.roster) < 128 or pair in self.roster):
                self.roster[pair] = entry

    def forget(self, player):
        identity = wire_uuid(player.unique_id)
        self.local_roster.pop(identity, None)
        for records in (self.roster, self.applied):
            for pair in tuple(records):
                if pair[0] == identity or pair[1] == identity:
                    records.pop(pair, None)

    def skin_changed(self, player):
        identity = wire_uuid(player.unique_id)
        self.local_roster.pop(identity, None)
        for pair in tuple(self.roster):
            if pair[1] == identity:
                self.roster.pop(pair, None)
        for pair in tuple(self.applied):
            if pair[1] == identity:
                self.applied.pop(pair, None)

    def update(self, plugin, enabled):
        if not enabled and not self.applied:
            return
        players = {wire_uuid(p.unique_id): p for p in plugin.server.online_players}
        staff = plugin.server.plugin_manager.get_plugin("kartexmine_staff")
        vanish = getattr(staff, "_vanish", None)
        desired = {}
        if enabled:
            for identity, player in players.items():
                item = player.inventory.helmet
                data = plugin._item_data(item)
                if data is None or getattr(item.type, "id", item.type) != LIVE_HEAD:
                    continue
                key = plugin._skins.key_for(data)
                if key is not None:
                    desired[identity] = key
        routes = dict(self.roster)
        for (_, identity), original in self.roster.items():
            if identity in desired or (identity, identity) in self.applied:
                routes.setdefault((identity, identity), original)
        for identity in desired:
            pair = (identity, identity)
            if pair not in routes and identity not in self.local_roster:
                try:
                    entry = self_roster(players[identity], identity)
                except (AttributeError, RuntimeError, ValueError):
                    continue
                self.local_roster[identity] = entry
        for identity, entry in tuple(self.local_roster.items()):
            pair = identity, identity
            if identity in desired or pair in self.applied:
                routes.setdefault(pair, entry)
            else:
                self.local_roster.pop(identity)
        for pair, (_, original) in self.applied.items():
            routes.setdefault(pair, original)
        for pair, original in routes.items():
            viewer, subject = (players.get(identity) for identity in pair)
            if viewer is None or subject is None:
                continue
            if pair[0] != pair[1] and vanish is not None and vanish.is_hidden(subject):
                continue
            key = desired.get(pair[1])
            applied = self.applied.get(pair)
            if key is None and applied is None:
                continue
            state = (key, original)
            if self.applied.get(pair) == state:
                continue
            self.revision += 1
            entry = restore_entry(original, self.revision) if key is None else None
            if key is not None:
                info = plugin._skins.index["skins"][key]
                skin = plugin._skins.read_pixels(key)
                entry = merged_entry(original, info["width"], skin, self.revision)
                if entry is None:
                    continue
            self.sending = True
            try:
                viewer.send_packet(PLAYER_LIST, unsigned(1) + entry)
            finally:
                self.sending = False
            if key is None:
                self.applied.pop(pair, None)
            else:
                self.applied[pair] = state
                self.updates += 1


def self_roster(player, identity):
    skin = player.skin
    width, height, pixels = image_snapshot(skin.image)
    skin_id = skin.id
    if "persona" in skin_id.casefold():
        raise ValueError("persona skin")
    geometry = "geometry.humanoid.customSlim" if "slim" in skin_id.casefold() else "geometry.humanoid.custom"
    patch = json.dumps({"geometry": {"default": geometry}})
    cape_width = cape_height = 0
    cape = b""
    image = skin.cape_image
    if image is not None and image.size:
        cape_height, cape_width, depth = image.shape
        if depth != 4 or cape_width * cape_height > 65536:
            raise ValueError("unsupported cape")
        cape = image.tobytes(order="C")
    prefix = (b"\x01\x00" + identity + signed(player.id) + field(player.name)
              + field(str(player.xuid)) + b"\0\0" + pack("<i", -1))
    encoded_skin = (field(skin_id) + field(patch) + pack("<II", width, height) + field(pixels)
                    + b"\0" + pack("<II", cape_width, cape_height) + field(cape)
                    + field("") + field(player.game_version) + field("") + field(skin.cape_id)
                    + field(skin_id) + bytes(10) + b"\x01\x00" + field("True") + field(""))
    return prefix + encoded_skin + bytes(7), len(prefix)
