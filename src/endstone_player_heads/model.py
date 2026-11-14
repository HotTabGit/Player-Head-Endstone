from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import random
import re
from typing import Any
import unicodedata
from uuid import UUID, uuid4


NBT_KEY = "kartexmine:player_head"
SCHEMA_VERSION = 1
MAX_NAME_CHARS = 64
MAX_NAME_BYTES = 256
MAX_XUID = (1 << 64) - 1
MAX_CREATED_AT = (1 << 63) - 1
_MISSING = object()
_DATA_FIELDS = ("schema", "head_id", "victim_xuid", "victim_name", "killer_xuid", "killer_name", "created_at")


class HeadDataError(ValueError):
    pass


def normalize_xuid(value: object, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if type(value) is int:
        number = value
    elif type(value) is str and len(value) <= 20 and re.fullmatch(r"[0-9]{1,20}", value):
        number = int(value)
    else:
        raise HeadDataError("invalid_xuid")
    if not 0 < number <= MAX_XUID:
        raise HeadDataError("invalid_xuid")
    return str(number)


def sanitize_name(value: str) -> str:
    if type(value) is not str:
        raise HeadDataError("invalid_name")
    if len(value) > 512:
        raise HeadDataError("name_input_too_long")
    value = re.sub(r"§.?", "", value, flags=re.DOTALL)
    result = []
    byte_count = 0
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("C") or category in ("Zl", "Zp"):
            continue
        size = len(char.encode("utf-8"))
        if len(result) >= MAX_NAME_CHARS or byte_count + size > MAX_NAME_BYTES:
            break
        result.append(char)
        byte_count += size
    name = "".join(result).strip()
    if not name:
        raise HeadDataError("empty_name")
    return name


def _canonical_name(value: object, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if type(value) is not str or len(value) > MAX_NAME_CHARS:
        raise HeadDataError("invalid_name")
    if sanitize_name(value) != value:
        raise HeadDataError("invalid_name")
    return value


def _canonical_uuid(value: object) -> str:
    if type(value) is not str or len(value) != 36:
        raise HeadDataError("invalid_head_id")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HeadDataError("invalid_head_id") from exc
    if str(parsed) != value or parsed.int == 0:
        raise HeadDataError("invalid_head_id")
    return value


@dataclass(frozen=True)
class HeadData:
    head_id: str
    victim_xuid: str
    victim_name: str
    killer_xuid: str
    killer_name: str
    created_at: int
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != SCHEMA_VERSION:
            raise HeadDataError("unsupported_schema")
        _canonical_uuid(self.head_id)
        if normalize_xuid(self.victim_xuid) != self.victim_xuid:
            raise HeadDataError("noncanonical_xuid")
        _canonical_name(self.victim_name)
        if normalize_xuid(self.killer_xuid, optional=True) != self.killer_xuid:
            raise HeadDataError("noncanonical_xuid")
        _canonical_name(self.killer_name, optional=True)
        if bool(self.killer_xuid) != bool(self.killer_name):
            raise HeadDataError("incomplete_killer")
        if type(self.created_at) is not int or not 0 <= self.created_at <= MAX_CREATED_AT:
            raise HeadDataError("invalid_created_at")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, record: Any) -> HeadData:
        if not _mapping(record) or len(record) != len(_DATA_FIELDS):
            raise HeadDataError("invalid_metadata_fields")
        values = {field: _scalar(_get(record, field), int if field in ("schema", "created_at") else str)
                  for field in _DATA_FIELDS}
        return cls(**values)


def make_head_data(victim_xuid: object, victim_name: str, killer_xuid: object = "",
                   killer_name: str = "", *, created_at: int | None = None,
                   head_id: str | None = None) -> HeadData:
    if created_at is None:
        created_at = int(datetime.now(timezone.utc).timestamp())
    victim_id = normalize_xuid(victim_xuid)
    killer_id = normalize_xuid(killer_xuid, optional=True)
    return HeadData(
        head_id=str(uuid4()) if head_id is None else head_id,
        victim_xuid=victim_id,
        victim_name=sanitize_name(victim_name),
        killer_xuid=killer_id,
        killer_name=sanitize_name(killer_name) if killer_id else "",
        created_at=created_at,
    )


def head_display_name(data: HeadData) -> str:
    return f"§rГолова {data.victim_name}"


def head_lore(data: HeadData) -> list[str]:
    lines = [f"§r§7Игрок: {data.victim_name}"]
    if data.killer_name:
        lines.append(f"§r§7Убийца: {data.killer_name}")
    return lines


def _signing_key(key: bytes) -> bytes:
    if type(key) is not bytes or not 32 <= len(key) <= 128:
        raise HeadDataError("invalid_signing_key")
    return key


def _signed_bytes(data: HeadData) -> bytes:
    return json.dumps(data.to_dict(), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def head_payload(data: HeadData, signing_key: bytes) -> dict:
    signature = hmac.new(_signing_key(signing_key), _signed_bytes(data), hashlib.sha256).hexdigest()
    return {**data.to_dict(), "signature": signature}


def _mapping(value: Any) -> bool:
    return isinstance(value, Mapping) or (
        callable(getattr(value, "items", None)) and hasattr(value, "__getitem__")
    )


def _get(value: Any, key: str) -> Any:
    if not _mapping(value):
        raise HeadDataError("invalid_compound")
    try:
        return value[key] if key in value else _MISSING
    except (KeyError, TypeError, AttributeError) as exc:
        raise HeadDataError("invalid_compound") from exc


def _scalar(value: Any, expected: type) -> Any:
    scalar = value if type(value) in (str, int) else getattr(value, "value", _MISSING)
    if type(scalar) is not expected:
        raise HeadDataError("invalid_scalar")
    return scalar


def decode_head_payload(payload: Any, signing_key: bytes) -> HeadData:
    _signing_key(signing_key)
    if not _mapping(payload) or len(payload) != len(_DATA_FIELDS) + 1:
        raise HeadDataError("invalid_metadata_fields")
    values = {field: _scalar(_get(payload, field), int if field in ("schema", "created_at") else str)
              for field in _DATA_FIELDS}
    data = HeadData(**values)
    signature = _scalar(_get(payload, "signature"), str)
    if len(signature) != 64 or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise HeadDataError("invalid_signature")
    expected = hmac.new(signing_key, _signed_bytes(data), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HeadDataError("invalid_signature")
    return data


def read_head_nbt(root: Any, signing_key: bytes) -> HeadData | None:
    _signing_key(signing_key)
    try:
        own = _get(root, NBT_KEY)
        wrapper = _get(root, "tag")
        wrapped = _get(wrapper, NBT_KEY) if wrapper is not _MISSING else _MISSING
        if own is not _MISSING and wrapped is not _MISSING:
            raise HeadDataError("ambiguous_item_nbt")
        payload = wrapped if own is _MISSING else own
        return None if payload is _MISSING else decode_head_payload(payload, signing_key)
    except (HeadDataError, TypeError, ValueError, AttributeError):
        return None


def make_head_tag(data: HeadData, signing_key: bytes) -> Any:
    from endstone.nbt import CompoundTag, IntTag, LongTag, StringTag

    tag = CompoundTag()
    for field, value in head_payload(data, signing_key).items():
        if field == "schema":
            tag[field] = IntTag(value)
        elif field == "created_at":
            tag[field] = LongTag(value)
        else:
            tag[field] = StringTag(value)
    return tag


def should_drop(chance_percent: float, sample: float | None = None) -> bool:
    if type(chance_percent) not in (int, float) or not 0 <= chance_percent <= 100 or not math.isfinite(chance_percent):
        raise ValueError("chance_percent must be finite and in 0..100")
    if sample is not None and (type(sample) not in (int, float) or not 0 <= sample < 1 or not math.isfinite(sample)):
        raise ValueError("sample must be finite and in [0, 1)")
    if chance_percent == 0:
        return False
    if chance_percent == 100:
        return True
    return (random.random() if sample is None else sample) < chance_percent / 100
