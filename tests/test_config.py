from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endstone_player_heads.config import ConfigError, Settings, load_settings, parse_settings


class ConfigTests(unittest.TestCase):
    def test_shipped_config_has_enabled_full_chance_pvp_defaults(self):
        shipped = Path(__file__).resolve().parents[1] / "src" / "endstone_player_heads" / "config.toml"
        self.assertEqual(load_settings(shipped), Settings())

    def test_empty_config_uses_defaults(self):
        self.assertEqual(parse_settings({}), Settings())

    def test_fractional_chance_and_explicit_zero_cooldown(self):
        settings = parse_settings({"heads": {"drop_chance_percent": 12.5, "pair_cooldown_seconds": 0,
                                            "pvp_only": False, "enabled": False}})
        self.assertEqual(settings.drop_chance_percent, 12.5)
        self.assertEqual(settings.pair_cooldown_seconds, 0)
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.pvp_only)

    def test_unknown_section_or_key_is_not_silently_ignored(self):
        for record in ({"head": {}}, {"heads": {"drop_chance": 100}}, {"heads": []}, []):
            with self.subTest(record=record), self.assertRaises(ConfigError):
                parse_settings(record)

    def test_non_boolean_flags_rejected(self):
        for key in ("enabled", "pvp_only"):
            for value in (0, 1, "true", None):
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    parse_settings({"heads": {key: value}})

    def test_chance_cannot_be_boolean_nan_infinite_or_out_of_bounds(self):
        for value in (True, -1, 101, float("nan"), float("inf"), "100", 10 ** 1000):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ConfigError):
                parse_settings({"heads": {"drop_chance_percent": value}})

    def test_integer_fields_are_bounded_and_not_coerced(self):
        for key, maximum in (("pair_cooldown_seconds", 604800), ("item_data", 32767)):
            for value in (True, -1, maximum + 1, 1.0, "1"):
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    parse_settings({"heads": {key: value}})

    def test_item_id_must_be_namespaced_and_cannot_be_a_command(self):
        for item_id in ("skull", "minecraft:skull;kill @a", "Minecraft:Skull", "minecraft:", True):
            with self.subTest(item_id=item_id), self.assertRaises(ConfigError):
                parse_settings({"heads": {"item_type": item_id}})

    def test_config_load_errors_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            with self.assertRaises(ConfigError):
                load_settings(path)
            path.write_text("[heads]\nenabled = !!!", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)
            path.write_text("#" * 16385, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
