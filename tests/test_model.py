from dataclasses import replace
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endstone_player_heads.model import (
    HeadData, HeadDataError, MAX_NAME_BYTES, MAX_NAME_CHARS, MAX_XUID, NBT_KEY,
    decode_head_payload, head_display_name, head_lore, head_payload,
    make_head_data, make_head_tag, normalize_xuid, read_head_nbt, sanitize_name,
    should_drop,
)


KEY = b"0123456789abcdef0123456789abcdef"
VICTIM = "2535401234567890"
KILLER = "2535409876543210"
HEAD_ID = "123e4567-e89b-42d3-a456-426614174000"


class StringTag:
    def __init__(self, value):
        self.value = value


class IntTag(StringTag):
    pass


class LongTag(StringTag):
    pass


class CompoundTag:
    def __init__(self, mapping=None):
        self.mapping = {} if mapping is None else mapping

    def __len__(self):
        return len(self.mapping)

    def __contains__(self, key):
        return key in self.mapping

    def __getitem__(self, key):
        return self.mapping[key]

    def __setitem__(self, key, value):
        self.mapping[key] = value

    def items(self):
        return self.mapping.items()

    def to_dict(self):
        raise AssertionError("Untrusted NBT must not be materialized")


def data(**kwargs):
    defaults = dict(victim_xuid=VICTIM, victim_name="HotTabTg", killer_xuid=KILLER,
                    killer_name="oksidciya", created_at=1791280000, head_id=HEAD_ID)
    defaults.update(kwargs)
    return make_head_data(**defaults)


class IdentityTests(unittest.TestCase):
    def test_xuid_is_canonical_and_unsigned_64_bit(self):
        self.assertEqual(normalize_xuid("000000001"), "1")
        self.assertEqual(normalize_xuid(MAX_XUID), str(MAX_XUID))

    def test_bool_nickname_uuid_float_negative_zero_not_xuid(self):
        for value in (True, False, "HotTabTg", HEAD_ID, 123.0, 0, "0", -1,
                      MAX_XUID + 1, "-123", "25354 0", "", None, "１２３"):
            with self.subTest(value=value), self.assertRaises(HeadDataError):
                normalize_xuid(value)

    def test_same_name_different_xuid_remains_different_victim(self):
        first = data()
        second = data(victim_xuid="123")
        self.assertEqual(first.victim_name, second.victim_name)
        self.assertNotEqual(first.victim_xuid, second.victim_xuid)
        self.assertNotEqual(head_payload(first, KEY)["signature"], head_payload(second, KEY)["signature"])

    def test_rename_keeps_stable_victim_identity(self):
        self.assertEqual(data().victim_xuid, data(victim_name="NewName").victim_xuid)

    def test_no_killer_is_explicit_empty_pair(self):
        head = data(killer_xuid="", killer_name="")
        self.assertEqual(head.killer_xuid, "")
        self.assertEqual(head_lore(head), ["§r§7Игрок: HotTabTg"])

    def test_partial_killer_record_rejected(self):
        for changed in (dict(killer_xuid=""), dict(killer_name="")):
            with self.subTest(changed=changed), self.assertRaises(HeadDataError):
                replace(data(), **changed)

    def test_created_time_must_be_signed_64_bit_integer(self):
        for value in (True, -1, 1.5, "12", 1 << 63):
            with self.subTest(value=value), self.assertRaises(HeadDataError):
                data(created_at=value)

    def test_new_head_id_is_unique(self):
        first = make_head_data(VICTIM, "A")
        second = make_head_data(VICTIM, "A")
        self.assertNotEqual(first.head_id, second.head_id)

    def test_zero_and_noncanonical_uuid_rejected(self):
        for value in ("00000000-0000-0000-0000-000000000000", "x" * 36,
                      HEAD_ID.upper(), HEAD_ID.replace("-", ""), True):
            with self.subTest(value=value), self.assertRaises(HeadDataError):
                data(head_id=value)

    def test_names_remove_formatting_and_control_sequences(self):
        self.assertEqual(sanitize_name(" §aHot\nTab\x00Tg\u202e§r "), "HotTabTg")
        self.assertEqual(head_display_name(data(victim_name="§cHotTabTg")), "§rГолова HotTabTg")

    def test_names_preserve_real_unicode_and_bound_chars_bytes(self):
        self.assertEqual(sanitize_name("Алиса_你好"), "Алиса_你好")
        name = sanitize_name("😀" * 500)
        self.assertLessEqual(len(name), MAX_NAME_CHARS)
        self.assertLessEqual(len(name.encode("utf-8")), MAX_NAME_BYTES)

    def test_empty_or_attacker_sized_name_rejected(self):
        for value in ("", "§a\n\x00", "x" * 513, False, None):
            with self.subTest(value=value), self.assertRaises(HeadDataError):
                sanitize_name(value)


class NbtTests(unittest.TestCase):
    def test_signed_plain_json_round_trip(self):
        original = data(victim_name="Алиса")
        payload = json.loads(json.dumps(head_payload(original, KEY)))
        self.assertEqual(decode_head_payload(payload, KEY), original)
        self.assertEqual(HeadData.from_dict(original.to_dict()), original)

    def test_item_root_and_full_item_wrapper_accepted(self):
        payload = head_payload(data(), KEY)
        self.assertEqual(read_head_nbt({NBT_KEY: payload}, KEY), data())
        self.assertEqual(read_head_nbt({"Name": "minecraft:skull", "tag": {NBT_KEY: payload}}, KEY), data())

    def test_compound_native_tag_shape_round_trip_without_bulk_copy(self):
        native = ModuleType("endstone.nbt")
        native.CompoundTag, native.StringTag = CompoundTag, StringTag
        native.IntTag, native.LongTag = IntTag, LongTag
        with patch.dict(sys.modules, {"endstone.nbt": native}):
            metadata = make_head_tag(data(), KEY)
        self.assertIsInstance(metadata["schema"], IntTag)
        self.assertIsInstance(metadata["created_at"], LongTag)
        root = CompoundTag({NBT_KEY: metadata})
        self.assertEqual(read_head_nbt(root, KEY), data())

    def test_forged_identity_and_display_metadata_rejected(self):
        altered_fields = dict(head_id="223e4567-e89b-42d3-a456-426614174000", victim_xuid="123",
                              victim_name="Impostor", killer_xuid="456", killer_name="Forged",
                              created_at=1, schema=2)
        for field, value in altered_fields.items():
            forged = head_payload(data(), KEY)
            forged[field] = value
            with self.subTest(field=field):
                self.assertIsNone(read_head_nbt({NBT_KEY: forged}, KEY))

    def test_signature_from_another_key_rejected(self):
        root = {NBT_KEY: head_payload(data(), KEY)}
        self.assertIsNone(read_head_nbt(root, b"another-key-32-byte-or-longer!!!!!"))

    def test_signature_missing_short_nonhex_uppercase_rejected(self):
        for signature in (None, "", "0" * 64, "g" * 64, "0" * 10000, True):
            payload = head_payload(data(), KEY)
            payload["signature"] = signature
            with self.subTest(signature_type=type(signature).__name__):
                self.assertIsNone(read_head_nbt({NBT_KEY: payload}, KEY))

    def test_native_bool_scalar_not_accepted_as_integer(self):
        payload = head_payload(data(), KEY)
        payload["schema"] = IntTag(True)
        self.assertIsNone(read_head_nbt({NBT_KEY: CompoundTag(payload)}, KEY))

    def test_malformed_or_unrelated_nbt_returns_none(self):
        for nbt in (None, 1, [], {}, {"tag": []}, {NBT_KEY: []}, {NBT_KEY: {"schema": 1}},
                    {NBT_KEY: "x" * 10000}):
            with self.subTest(kind=type(nbt).__name__):
                self.assertIsNone(read_head_nbt(nbt, KEY))

    def test_duplicate_top_and_wrapped_metadata_rejected(self):
        payload = head_payload(data(), KEY)
        self.assertIsNone(read_head_nbt({NBT_KEY: payload, "tag": {NBT_KEY: payload}}, KEY))

    def test_unknown_or_missing_field_rejected(self):
        for changed in ("add", "remove", "replace"):
            payload = head_payload(data(), KEY)
            if changed in ("remove", "replace"):
                payload.pop("victim_xuid")
            if changed in ("add", "replace"):
                payload["ignored_field"] = "x"
            with self.subTest(changed=changed):
                self.assertIsNone(read_head_nbt({NBT_KEY: payload}, KEY))

    def test_attacker_sized_field_rejected_before_signing(self):
        payload = head_payload(data(), KEY)
        payload["victim_name"] = "x" * 1000000
        self.assertIsNone(read_head_nbt({NBT_KEY: payload}, KEY))

    def test_name_with_control_codes_in_saved_record_rejected(self):
        record = data().to_dict()
        record["victim_name"] = "§aHotTabTg"
        with self.assertRaises(HeadDataError):
            HeadData.from_dict(record)

    def test_exact_copy_is_valid_but_keeps_original_head_id(self):
        root = {NBT_KEY: head_payload(data(), KEY)}
        self.assertEqual(read_head_nbt(json.loads(json.dumps(root)), KEY).head_id, HEAD_ID)

    def test_invalid_signing_key_is_configuration_error(self):
        for key in (b"short", "x" * 32, bytearray(32), b"x" * 129):
            with self.subTest(kind=type(key).__name__), self.assertRaises(HeadDataError):
                read_head_nbt({}, key)


class DropChanceTests(unittest.TestCase):
    def test_zero_and_full_chance_do_not_call_random(self):
        with patch("endstone_player_heads.model.random.random", side_effect=AssertionError):
            self.assertFalse(should_drop(0))
            self.assertTrue(should_drop(100))

    def test_fractional_chance_and_boundary_are_deterministic(self):
        self.assertTrue(should_drop(25.5, 0.25499))
        self.assertFalse(should_drop(25.5, 0.255))
        self.assertFalse(should_drop(25.5, 0.99))

    def test_invalid_chance_does_not_pass(self):
        for chance in (True, -1, 101, float("nan"), float("inf"), "100", 10 ** 1000):
            with self.subTest(kind=type(chance).__name__), self.assertRaises(ValueError):
                should_drop(chance, 0.5)

    def test_invalid_random_sample_does_not_pass(self):
        for sample in (True, -0.1, 1, float("nan"), float("inf"), "0.5", 10 ** 1000):
            with self.subTest(kind=type(sample).__name__), self.assertRaises(ValueError):
                should_drop(50, sample)


if __name__ == "__main__":
    unittest.main()
