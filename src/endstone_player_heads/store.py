from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Any
import unicodedata

from .model import HeadData, HeadDataError, decode_head_payload, head_payload


STORE_VERSION = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 8192
MAX_RECORD_BYTES = 4096
MAX_KEY_BYTES = 128
MAX_HORIZONTAL = 30_000_000
MIN_Y = -2048
MAX_Y = 2047
Position = tuple[str, int, int, int]


class HeadStoreError(ValueError):
    pass


def _world_name(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128 or value != value.strip():
        raise HeadStoreError("invalid_world_name")
    if any(unicodedata.category(char).startswith("C") or char in "\n\r" for char in value):
        raise HeadStoreError("invalid_world_name")
    if len(value.encode("utf-8")) > 256:
        raise HeadStoreError("invalid_world_name")
    return value


def validate_position(position: Any) -> Position:
    if type(position) not in (tuple, list) or len(position) != 4:
        raise HeadStoreError("invalid_position")
    dimension, x, y, z = position
    if type(dimension) is not str or not re.fullmatch(r"[A-Za-z0-9_:. -]{1,64}", dimension):
        raise HeadStoreError("invalid_dimension")
    if dimension != dimension.strip():
        raise HeadStoreError("invalid_dimension")
    if any(type(value) is not int for value in (x, y, z)):
        raise HeadStoreError("invalid_coordinates")
    if not -MAX_HORIZONTAL <= x <= MAX_HORIZONTAL or not -MAX_HORIZONTAL <= z <= MAX_HORIZONTAL:
        raise HeadStoreError("coordinates_out_of_bounds")
    if not MIN_Y <= y <= MAX_Y:
        raise HeadStoreError("coordinates_out_of_bounds")
    return dimension, x, y, z


def _position_key(position: Position) -> str:
    dimension, x, y, z = validate_position(position)
    return f"{dimension}|{x}|{y}|{z}"


def _parse_position_key(key: object) -> Position:
    if type(key) is not str or len(key) > MAX_KEY_BYTES:
        raise HeadStoreError("invalid_coordinate_key")
    fields = key.split("|")
    if len(fields) != 4 or any(not re.fullmatch(r"-?[0-9]{1,8}", value) for value in fields[1:]):
        raise HeadStoreError("invalid_coordinate_key")
    result = validate_position((fields[0], *(int(value) for value in fields[1:])))
    if _position_key(result) != key:
        raise HeadStoreError("noncanonical_coordinate_key")
    return result


def _entry_bytes(position: Position, encoded_record: str) -> int:
    return 5 + len(_position_key(position).encode("utf-8")) + len(encoded_record.encode("utf-8"))


def _scalar(value: Any, expected: type) -> Any:
    scalar = value if type(value) in (str, int) else getattr(value, "value", None)
    if type(scalar) is not expected:
        raise HeadStoreError("invalid_store_scalar")
    return scalar


def _mapping(value: Any) -> bool:
    return isinstance(value, Mapping) or (
        callable(getattr(value, "items", None)) and hasattr(value, "__getitem__")
    )


def _get(mapping: Any, key: str) -> Any:
    if not _mapping(mapping):
        raise HeadStoreError("invalid_store_compound")
    try:
        return mapping[key]
    except (KeyError, TypeError, AttributeError) as exc:
        raise HeadStoreError("missing_store_field") from exc


def _canonical_json(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _store_signature(root: dict, signing_key: bytes) -> str:
    return hmac.new(signing_key, _canonical_json(root), hashlib.sha256).hexdigest()


def _record(world: str, position: Position, head: HeadData, signing_key: bytes) -> str:
    dimension, x, y, z = position
    record = {"world": world, "dimension": dimension, "x": x, "y": y, "z": z,
              "head": head_payload(head, signing_key)}
    signature = hmac.new(signing_key, _canonical_json(record), hashlib.sha256).hexdigest()
    encoded = _canonical_json({**record, "record_signature": signature})
    if len(encoded) > MAX_RECORD_BYTES:
        raise HeadStoreError("record_too_large")
    return encoded.decode("utf-8")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HeadStoreError("duplicate_json_field")
        result[key] = value
    return result


def _read_record(raw: str, world: str, position: Position, signing_key: bytes) -> HeadData:
    if type(raw) is not str or len(raw) > MAX_RECORD_BYTES or len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
        raise HeadStoreError("record_too_large")
    try:
        record = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise HeadStoreError("invalid_record_json") from exc
    expected_fields = {"world", "dimension", "x", "y", "z", "head", "record_signature"}
    if type(record) is not dict or set(record) != expected_fields:
        raise HeadStoreError("invalid_record_fields")
    if _world_name(record["world"]) != world:
        raise HeadStoreError("record_world_mismatch")
    if validate_position((record["dimension"], record["x"], record["y"], record["z"])) != position:
        raise HeadStoreError("record_position_mismatch")
    signature = record.pop("record_signature")
    if type(signature) is not str or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise HeadStoreError("invalid_record_signature")
    try:
        head = decode_head_payload(record["head"], signing_key)
    except (HeadDataError, TypeError, AttributeError) as exc:
        raise HeadStoreError("invalid_head_signature") from exc
    if not hmac.compare_digest(signature, hmac.new(signing_key, _canonical_json(record), hashlib.sha256).hexdigest()):
        raise HeadStoreError("invalid_record_signature")
    return head


class BoundedNbtCodec:

    @staticmethod
    def dumps(root: Any) -> bytes:
        if not _mapping(root) or len(root) != 4:
            raise HeadStoreError("invalid_store_root")
        version = _scalar(_get(root, "version"), int)
        if version != STORE_VERSION:
            raise HeadStoreError("unsupported_store_version")
        world = _world_name(_scalar(_get(root, "world"), str))
        signature = _scalar(_get(root, "signature"), str)
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise HeadStoreError("invalid_store_signature")
        entries = _get(root, "entries")
        if not _mapping(entries) or len(entries) > MAX_RECORDS:
            raise HeadStoreError("too_many_records")

        def string(value: str, limit: int) -> bytes:
            if type(value) is not str or len(value) > limit:
                raise HeadStoreError("invalid_nbt_string")
            encoded = value.encode("utf-8")
            if len(encoded) > limit:
                raise HeadStoreError("nbt_string_too_large")
            return struct.pack("<H", len(encoded)) + encoded

        output = bytearray(b"\x0a\x00\x00")
        output.extend(b"\x03" + string("version", 16) + struct.pack("<i", version))
        output.extend(b"\x08" + string("world", 16) + string(world, 256))
        output.extend(b"\x08" + string("signature", 16) + string(signature, 64))
        output.extend(b"\x0a" + string("entries", 16))
        for key, value in entries.items():
            _parse_position_key(key)
            output.extend(b"\x08" + string(key, MAX_KEY_BYTES) + string(_scalar(value, str), MAX_RECORD_BYTES))
            if len(output) > MAX_FILE_BYTES - 2:
                raise HeadStoreError("store_file_too_large")
        output.extend(b"\x00\x00")
        return bytes(output)

    @staticmethod
    def loads(raw: bytes) -> dict:
        if type(raw) is not bytes or len(raw) > MAX_FILE_BYTES:
            raise HeadStoreError("store_file_too_large")
        offset = 0

        def take(size: int) -> bytes:
            nonlocal offset
            end = offset + size
            if end > len(raw):
                raise HeadStoreError("truncated_nbt")
            result = raw[offset:end]
            offset = end
            return result

        def byte() -> int:
            return take(1)[0]

        def string(limit: int) -> str:
            length = struct.unpack("<H", take(2))[0]
            if length > limit:
                raise HeadStoreError("nbt_string_too_large")
            try:
                return take(length).decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise HeadStoreError("invalid_nbt_utf8") from exc

        if byte() != 10:
            raise HeadStoreError("invalid_store_root")
        string(128)
        root = {}
        while (kind := byte()) != 0:
            key = string(16)
            if key in root:
                raise HeadStoreError("duplicate_root_field")
            if key == "version" and kind == 3:
                root[key] = struct.unpack("<i", take(4))[0]
            elif key in ("world", "signature") and kind == 8:
                root[key] = string(256 if key == "world" else 64)
            elif key == "entries" and kind == 10:
                entries = {}
                while (entry_kind := byte()) != 0:
                    if entry_kind != 8:
                        raise HeadStoreError("invalid_entry_nbt_type")
                    if len(entries) >= MAX_RECORDS:
                        raise HeadStoreError("too_many_records")
                    coordinate = string(MAX_KEY_BYTES)
                    if coordinate in entries:
                        raise HeadStoreError("duplicate_coordinate_key")
                    _parse_position_key(coordinate)
                    entries[coordinate] = string(MAX_RECORD_BYTES)
                root[key] = entries
            else:
                raise HeadStoreError("invalid_store_field")
        if offset != len(raw) or set(root) != {"version", "world", "entries", "signature"}:
            raise HeadStoreError("invalid_store_root")
        return root


class NbtHeadStore:
    def __init__(self, path: Path, world_name: str, signing_key: bytes, codec: Any = None):
        self.path = Path(path)
        self.world_name = _world_name(world_name)
        if type(signing_key) is not bytes or not 32 <= len(signing_key) <= 128:
            raise HeadStoreError("invalid_signing_key")
        self._key = signing_key
        self._codec = BoundedNbtCodec() if codec is None else codec
        self._records: dict[Position, HeadData] = {}
        self._encoded_records: dict[Position, str] = {}
        self._record_bytes: dict[Position, int] = {}
        self._used_record_bytes = 0
        self._base_bytes = 117 + len(self.world_name.encode("utf-8"))
        self._dirty = False
        self.reload()

    @property
    def count(self) -> int:
        return len(self._records)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def byte_size(self) -> int:
        return self._base_bytes + self._used_record_bytes

    @property
    def remaining_bytes(self) -> int:
        return MAX_FILE_BYTES - self.byte_size

    def entry_size(self, position: Position, data: HeadData) -> int:
        position = validate_position(position)
        if not isinstance(data, HeadData):
            raise HeadStoreError("invalid_head_data")
        return _entry_bytes(position, _record(self.world_name, position, data, self._key))

    def existing_entry_size(self, position: Position) -> int:
        return self._record_bytes.get(validate_position(position), 0)

    def get(self, position: Position) -> HeadData | None:
        return self._records.get(validate_position(position))

    def _prepare_set(self, position: Position, data: HeadData) -> tuple[Position, str, int]:
        position = validate_position(position)
        if not isinstance(data, HeadData):
            raise HeadStoreError("invalid_head_data")
        if position not in self._records and self.count >= MAX_RECORDS:
            raise HeadStoreError("too_many_records")
        encoded = _record(self.world_name, position, data, self._key)
        size = _entry_bytes(position, encoded)
        projected_bytes = self.byte_size - self._record_bytes.get(position, 0) + size
        if projected_bytes > MAX_FILE_BYTES:
            raise HeadStoreError("store_file_too_large")
        return position, encoded, size

    def can_set(self, position: Position, data: HeadData) -> bool:
        self._prepare_set(position, data)
        return True

    def set(self, position: Position, data: HeadData) -> None:
        position, encoded, size = self._prepare_set(position, data)
        if self._records.get(position) != data:
            self._used_record_bytes += size - self._record_bytes.get(position, 0)
            self._records[position] = data
            self._encoded_records[position] = encoded
            self._record_bytes[position] = size
            self._dirty = True

    def pop(self, position: Position) -> HeadData | None:
        position = validate_position(position)
        value = self._records.pop(position, None)
        if value is not None:
            self._encoded_records.pop(position)
            self._used_record_bytes -= self._record_bytes.pop(position)
            self._dirty = True
        return value

    def items(self) -> tuple[tuple[Position, HeadData], ...]:
        return tuple(self._records.items())

    def records(self) -> tuple[tuple[Position, HeadData], ...]:
        return self.items()

    def reload(self) -> None:
        if self._dirty:
            raise HeadStoreError("store_has_unsaved_changes")
        if not self.path.exists():
            self._records = {}
            self._encoded_records = {}
            self._record_bytes = {}
            self._used_record_bytes = 0
            return
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise HeadStoreError("store_file_too_large")
            with self.path.open("rb") as source:
                raw = source.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise HeadStoreError("store_file_too_large")
            root = self._codec.loads(raw)
            if not _mapping(root) or len(root) != 4:
                raise HeadStoreError("invalid_store_root")
            if _scalar(_get(root, "version"), int) != STORE_VERSION:
                raise HeadStoreError("unsupported_store_version")
            if _world_name(_scalar(_get(root, "world"), str)) != self.world_name:
                raise HeadStoreError("store_world_mismatch")
            entries = _get(root, "entries")
            if not _mapping(entries) or len(entries) > MAX_RECORDS:
                raise HeadStoreError("too_many_records")
            loaded = {}
            encoded_records = {}
            record_bytes = {}
            plain_entries = {}
            for key, value in entries.items():
                position = _parse_position_key(key)
                if position in loaded:
                    raise HeadStoreError("duplicate_position")
                encoded = _scalar(value, str)
                loaded[position] = _read_record(encoded, self.world_name, position, self._key)
                encoded_records[position] = encoded
                record_bytes[position] = _entry_bytes(position, encoded)
                plain_entries[key] = encoded
            signature = _scalar(_get(root, "signature"), str)
            unsigned_root = {"version": STORE_VERSION, "world": self.world_name, "entries": plain_entries}
            if not re.fullmatch(r"[0-9a-f]{64}", signature) or not hmac.compare_digest(
                signature, _store_signature(unsigned_root, self._key)
            ):
                raise HeadStoreError("invalid_store_signature")
        except HeadStoreError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
            raise HeadStoreError("cannot_load_head_store") from exc
        self._records = loaded
        self._encoded_records = encoded_records
        self._record_bytes = record_bytes
        self._used_record_bytes = sum(record_bytes.values())

    def flush(self) -> bool:
        if not self._dirty:
            return False
        root = {"version": STORE_VERSION, "world": self.world_name,
                "entries": {_position_key(position): encoded for position, encoded in self._encoded_records.items()}}
        root["signature"] = _store_signature(root, self._key)
        try:
            encoded = self._codec.dumps(root)
            if type(encoded) is not bytes or len(encoded) > MAX_FILE_BYTES:
                raise HeadStoreError("store_file_too_large")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".placed-heads-", suffix=".nbt", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
                if os.name == "posix":
                    directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except HeadStoreError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise HeadStoreError("cannot_write_head_store") from exc
        self._dirty = False
        return True
