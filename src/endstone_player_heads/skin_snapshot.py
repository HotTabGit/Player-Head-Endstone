from hashlib import sha256
import json
from struct import unpack

from .skins import SIZES, classic_head_atlas, head_pixels


HEAD_FACES = {'east': (0, 8), 'north': (8, 8), 'west': (16, 8),
              'south': (24, 8), 'up': (8, 0), 'down': (16, 0)}


def read_skin_visuals(reader):
    skin_id = reader.blob()
    patch = reader.blob()
    width, height = unpack("<II", reader.take(8))
    pixels = reader.blob()
    count = reader.unsigned()
    if count > 64:
        raise ValueError("oversize animations")
    animations = []
    for _ in range(count):
        size = unpack("<II", reader.take(8))
        data = reader.blob()
        kind = reader.unsigned()
        frames = unpack("<f", reader.take(4))[0]
        reader.unsigned()
        if kind == 1:
            animations.append((size, data, frames))
    reader.take(8)
    reader.blob()
    geometry = reader.blob()
    return skin_id, patch, width, height, pixels, count, animations, geometry


def face_frame(size, data, frames):
    width, height = size
    if frames not in range(1, 65) or len(data) != width * height * 4:
        return None
    for frame_width in (32, 64, 128, 256):
        for frame_height in (frame_width // 2,) if frame_width == 32 else (frame_width // 4, frame_width):
            if (width, height) in ((frame_width, frame_height * frames), (frame_width * frames, frame_height)):
                return frame_width, (data[y * width * 4:y * width * 4 + frame_width * 4]
                                     for y in range(frame_height))
    return None


def animation_snapshot(width, height, pixels, animations):
    if not classic_head_atlas(width, pixels):
        return None
    for size, data, count in animations:
        frame = face_frame(size, data, count)
        if frame is None:
            continue
        frame_width, rows = frame
        rows = list(rows)
        scale = 1 if frame_width == 32 else frame_width // 64
        output = bytearray(head_pixels(width, height, pixels))
        dest_scale = width // 64
        for y in range(16 * dest_scale):
            row = rows[y * scale // dest_scale]
            for x in range((32 if frame_width == 32 else 64) * dest_scale):
                offset = (y * width + x) * 4
                i = x * scale // dest_scale * 4
                opacity = row[i + 3]
                if opacity == 255 or (opacity and not output[offset + 3]):
                    output[offset:offset + 4] = row[i:i + 4]
                elif opacity:
                    base_alpha = output[offset + 3] * (255 - opacity) // 255
                    alpha = opacity + base_alpha
                    output[offset:offset + 4] = bytes(
                        [(row[i + c] * opacity + output[offset + c] * base_alpha) // alpha for c in range(3)]
                        + [alpha])
        if output != head_pixels(width, height, pixels):
            return bytes(output)
    return None


def cube_uv(cube):
    uv = cube.get("uv")
    if isinstance(uv, list) and len(uv) == 2 and all(type(v) is int for v in uv):
        return {face: (uv[0] + x, uv[1] + y, 8, 8) for face, (x, y) in HEAD_FACES.items()}
    if not isinstance(uv, dict):
        return {}
    faces = {}
    for face, definition in uv.items():
        if face not in HEAD_FACES or not isinstance(definition, dict):
            continue
        start, size = definition.get("uv"), definition.get("uv_size", [8, 8])
        if (isinstance(start, list) and isinstance(size, list) and len(start) == len(size) == 2
                and all(type(value) is int for value in start + size)
                and 0 < abs(size[0]) <= 256 and 0 < abs(size[1]) <= 256):
            faces[face] = (*start, *size)
    return faces


def persona_snapshot(patch, geometry, pixels):
    if not geometry or len(geometry) > 256 * 1024 or len(patch) > 4096:
        return None
    try:
        name = json.loads(patch)["geometry"]["default"]
        doc = json.loads(geometry)
        model = doc.get(name)
        if model is None:
            model = next((m for m in doc.get("minecraft:geometry", ())
                          if m.get("description", {}).get("identifier") == name), {})
        description = model.get("description", model)
        texture_width = description.get("texture_width", description.get("texturewidth", 256))
        texture_height = description.get("texture_height", description.get("textureheight", 256))
        if (texture_width, texture_height) != (256, 256):
            return None
        cubes = []
        for bone in model.get("bones", ()):
            if bone.get("name", "").casefold() not in ("head", "hat", "headwear", "head_layer", "head_layer2"):
                continue
            if any(bone.get("rotation", ())):
                continue
            for cube in bone.get("cubes", ()):
                if (cube.get("size") == [8, 8, 8] and not any(cube.get("rotation", ()))
                        and not cube.get("mirror", bone.get("mirror", False))):
                    cubes.append((bone.get("name").casefold(), cube))
        base = next(cube for bone, cube in cubes if bone == "head" and not cube.get("inflate"))
        layers = [base] + [cube for bone, cube in cubes if cube is not base
                           and (bone != "head" or cube.get("inflate"))
                           and cube.get("origin") == base.get("origin")]
    except (AttributeError, LookupError, StopIteration, TypeError, ValueError, RecursionError):
        return None
    output = bytearray(256 * 256 * 4)
    for layer, cube in enumerate(layers[:2]):
        faces = cube_uv(cube)
        if layer == 0 and len(faces) != 6:
            return None
        for face, (u, v, sx, sy) in faces.items():
            if not (0 <= min(u, u + sx) < max(u, u + sx) <= 256
                    and 0 <= min(v, v + sy) < max(v, v + sy) <= 256):
                return None
            x, y = HEAD_FACES[face]
            for row in range(32):
                source_y = v + row * sy // 32 if sy > 0 else v - 1 - row * -sy // 32
                for col in range(32):
                    source_x = u + col * sx // 32 if sx > 0 else u - 1 - col * -sx // 32
                    source = (source_y * 256 + source_x) * 4
                    target = ((y * 4 + row) * 256 + x * 4 + layer * 128 + col) * 4
                    output[target:target + 4] = pixels[source:source + 4]
            if layer == 0 and not any(output[((y * 4 + row) * 256 + x * 4 + col) * 4 + 3]
                                      for row in range(32) for col in range(32)):
                return None
    return bytes(output)


def snapshot_from_visuals(visuals, persona):
    _, patch, width, height, pixels, _, frames, geometry = visuals
    if (width, height) == (1, 1) and len(pixels) == 4 and frames:
        static = animation_snapshot(64, 64, bytes(64 * 64 * 4), frames)
        if static is not None and all(
                all(static[(y * 64 + x) * 4 + 3] for y in range(v, v + 8) for x in range(u, u + 8))
                for u, v in HEAD_FACES.values()):
            return sha256(pixels).digest(), static
        return None
    if (width, height) not in SIZES or len(pixels) != width * height * 4:
        return None
    if persona:
        static = persona_snapshot(patch, geometry, pixels) if (width, height) == (256, 256) else None
    else:
        static = animation_snapshot(width, height, pixels, frames) if frames else None
    return (sha256(pixels).digest(), static) if static is not None else None
