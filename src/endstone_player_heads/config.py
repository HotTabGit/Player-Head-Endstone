from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import tomllib


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    pvp_only: bool = True
    drop_chance_percent: float = 100.0
    pair_cooldown_seconds: int = 0
    item_type: str = "minecraft:player_head"
    item_data: int = 0


def parse_settings(data: dict) -> Settings:
    if not isinstance(data, dict) or set(data) - {"heads"}:
        raise ConfigError("Неизвестный раздел конфигурации; разрешён только [heads].")
    section = data.get("heads", {})
    allowed = set(Settings.__dataclass_fields__)
    if not isinstance(section, dict) or set(section) - allowed:
        raise ConfigError("В разделе [heads] найдена неизвестная настройка.")
    values = {}
    defaults = Settings()
    for key in ("enabled", "pvp_only"):
        value = section.get(key, getattr(defaults, key))
        if type(value) is not bool:
            raise ConfigError(f"{key}: укажи true или false.")
        values[key] = value
    chance = section.get("drop_chance_percent", defaults.drop_chance_percent)
    if type(chance) not in (int, float) or not 0 <= chance <= 100 or not math.isfinite(chance):
        raise ConfigError("drop_chance_percent: укажи число от 0 до 100.")
    values["drop_chance_percent"] = float(chance)
    for key, high in (("pair_cooldown_seconds", 604800), ("item_data", 32767)):
        value = section.get(key, getattr(defaults, key))
        if type(value) is not int or not 0 <= value <= high:
            raise ConfigError(f"{key}: укажи целое число от 0 до {high}.")
        values[key] = value
    item_type = section.get("item_type", defaults.item_type)
    if type(item_type) is not str or len(item_type) > 128 or not re.fullmatch(
        r"[a-z0-9_]+:[a-z0-9_]+", item_type
    ):
        raise ConfigError("item_type: укажи идентификатор вида minecraft:player_head.")
    values["item_type"] = item_type
    return Settings(**values)


def load_settings(path: Path) -> Settings:
    try:
        if path.stat().st_size > 16384:
            raise ConfigError("Файл конфигурации слишком большой.")
        with path.open("rb") as source:
            return parse_settings(tomllib.load(source))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(str(exc)) from exc
