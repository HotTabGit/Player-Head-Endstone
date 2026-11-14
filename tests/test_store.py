from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endstone_player_heads.model import make_head_data
from endstone_player_heads.store import (
    BoundedNbtCodec, HeadStoreError, MAX_FILE_BYTES, NbtHeadStore, validate_position,
)


KEY = b"0123456789abcdef0123456789abcdef"
OTHER_KEY = b"another-key-32-byte-or-longer!!!!!"
WORLD = "KartexMine-20261003T110506Z"
POSITION = ("Overworld", 12, 64, -4)


def head():
    return make_head_data("2535401234567890", "HotTabTg", "2535409876543210", "oksidciya",
                          created_at=1791280000, head_id="123e4567-e89b-42d3-a456-426614174000")


def sign_root(root):
    unsigned = {key: value for key, value in root.items() if key != "signature"}
    payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    root["signature"] = hmac.new(KEY, payload, hashlib.sha256).hexdigest()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "placed-heads.nbt"
        self.store = NbtHeadStore(self.path, WORLD, KEY)

    def persisted_root(self):
        self.store.set(POSITION, head())
        self.store.flush()
        return BoundedNbtCodec.loads(self.path.read_bytes())

    def write_root(self, root):
        self.path.write_bytes(BoundedNbtCodec.dumps(root))

    def test_new_missing_store_is_empty_clean_and_does_not_create_file(self):
        self.assertEqual(self.store.count, 0)
        self.assertFalse(self.store.dirty)
        self.assertFalse(self.store.flush())
        self.assertFalse(self.path.exists())

    def test_place_flush_restart_recover_then_break_flush_restart(self):
        self.store.set(POSITION, head())
        self.assertTrue(self.store.dirty)
        self.assertEqual(self.store.get(POSITION), head())
        self.assertTrue(self.store.flush())
        self.assertFalse(self.store.dirty)
        self.assertEqual(self.path.read_bytes()[:3], b"\x0a\x00\x00")
        restarted = NbtHeadStore(self.path, WORLD, KEY)
        self.assertEqual(restarted.get(POSITION), head())
        self.assertEqual(restarted.pop(POSITION), head())
        restarted.flush()
        self.assertEqual(NbtHeadStore(self.path, WORLD, KEY).count, 0)

    def test_dimensions_at_same_coordinates_are_independent(self):
        other = ("Nether", *POSITION[1:])
        nether_head = replace(head(), victim_name="OtherPlayer")
        self.store.set(POSITION, head())
        self.store.set(other, nether_head)
        self.store.flush()
        restarted = NbtHeadStore(self.path, WORLD, KEY)
        self.assertEqual(restarted.count, 2)
        self.assertEqual(restarted.get(POSITION), head())
        self.assertEqual(restarted.get(other), nether_head)
        self.assertEqual(restarted.pop(POSITION), head())
        self.assertEqual(restarted.get(other), nether_head)

    def test_equivalent_set_and_missing_pop_do_not_mark_dirty(self):
        self.store.set(POSITION, head())
        self.store.flush()
        self.store.set(POSITION, head())
        self.assertIsNone(self.store.pop(("Nether", 1, 64, 0)))
        self.assertFalse(self.store.dirty)
        self.assertFalse(self.store.flush())

    def test_records_are_an_immutable_snapshot(self):
        self.store.set(POSITION, head())
        previous = self.store.items()
        self.assertEqual(previous, self.store.records())
        self.store.pop(POSITION)
        self.assertEqual(previous, ((POSITION, head()),))
        self.assertEqual(self.store.items(), ())

    def test_wrong_world_refuses_to_replace_existing_file(self):
        self.persisted_root()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(HeadStoreError, "store_world_mismatch"):
            NbtHeadStore(self.path, "AnotherWorld", KEY)
        self.assertEqual(self.path.read_bytes(), original)

    def test_wrong_key_refuses_even_empty_store(self):
        self.persisted_root()
        with self.assertRaises(HeadStoreError):
            NbtHeadStore(self.path, WORLD, OTHER_KEY)
        self.store.pop(POSITION)
        self.store.flush()
        with self.assertRaisesRegex(HeadStoreError, "invalid_store_signature"):
            NbtHeadStore(self.path, WORLD, OTHER_KEY)

    def test_dropping_all_records_without_resigning_file_is_detected(self):
        root = self.persisted_root()
        root["entries"].clear()
        self.write_root(root)
        with self.assertRaisesRegex(HeadStoreError, "invalid_store_signature"):
            NbtHeadStore(self.path, WORLD, KEY)

    def test_forged_head_signature_is_detected_before_store_signature(self):
        root = self.persisted_root()
        key, raw = next(iter(root["entries"].items()))
        record = json.loads(raw)
        record["head"]["victim_xuid"] = "123"
        root["entries"][key] = json.dumps(record)
        sign_root(root)
        self.write_root(root)
        with self.assertRaisesRegex(HeadStoreError, "invalid_head_signature"):
            NbtHeadStore(self.path, WORLD, KEY)

    def test_moving_record_to_different_coordinate_is_detected(self):
        root = self.persisted_root()
        record = root["entries"].pop("Overworld|12|64|-4")
        root["entries"]["Overworld|13|64|-4"] = record
        sign_root(root)
        self.write_root(root)
        with self.assertRaisesRegex(HeadStoreError, "record_position_mismatch"):
            NbtHeadStore(self.path, WORLD, KEY)

    def test_rewriting_record_coordinate_without_record_hmac_is_detected(self):
        root = self.persisted_root()
        record = json.loads(root["entries"].pop("Overworld|12|64|-4"))
        record["x"] = 13
        root["entries"]["Overworld|13|64|-4"] = json.dumps(record)
        sign_root(root)
        self.write_root(root)
        with self.assertRaisesRegex(HeadStoreError, "invalid_record_signature"):
            NbtHeadStore(self.path, WORLD, KEY)

    def test_record_json_duplicate_fields_are_not_silently_overwritten(self):
        root = self.persisted_root()
        key = next(iter(root["entries"]))
        root["entries"][key] = root["entries"][key][:-1] + ',"world":"fake"}'
        sign_root(root)
        self.write_root(root)
        with self.assertRaisesRegex(HeadStoreError, "duplicate_json_field"):
            NbtHeadStore(self.path, WORLD, KEY)

    def test_reload_rejects_pending_changes(self):
        self.store.set(POSITION, head())
        with self.assertRaisesRegex(HeadStoreError, "store_has_unsaved_changes"):
            self.store.reload()
        self.assertEqual(self.store.get(POSITION), head())

    def test_failed_reload_retains_previously_loaded_memory(self):
        self.persisted_root()
        self.path.write_bytes(b"corrupt")
        with self.assertRaises(HeadStoreError):
            self.store.reload()
        self.assertEqual(self.store.get(POSITION), head())

    def test_atomic_replace_failure_keeps_previous_file_and_dirty_state(self):
        self.persisted_root()
        before = self.path.read_bytes()
        self.store.set(("Overworld", 100, 65, 100), head())
        with patch("endstone_player_heads.store.os.replace", side_effect=OSError("denied")):
            with self.assertRaisesRegex(HeadStoreError, "cannot_write_head_store"):
                self.store.flush()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.store.dirty)
        self.assertEqual(list(self.path.parent.glob(".placed-heads-*")), [])
        self.assertEqual(NbtHeadStore(self.path, WORLD, KEY).count, 1)
        self.assertTrue(self.store.flush())
        self.assertEqual(NbtHeadStore(self.path, WORLD, KEY).count, 2)

    def test_fsync_failure_keeps_previous_file_and_dirty_state(self):
        self.persisted_root()
        before = self.path.read_bytes()
        self.store.pop(POSITION)
        with patch("endstone_player_heads.store.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(HeadStoreError):
                self.store.flush()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.store.dirty)

    def test_codec_write_failure_keeps_previous_file_and_dirty_state(self):
        self.persisted_root()
        before = self.path.read_bytes()
        self.store.pop(POSITION)
        with patch.object(self.store._codec, "dumps", side_effect=ValueError("bad codec")):
            with self.assertRaises(HeadStoreError):
                self.store.flush()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.store.dirty)

    def test_file_bound_is_checked_before_codec_is_called(self):
        self.path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with patch.object(BoundedNbtCodec, "loads", side_effect=AssertionError("must not parse")):
            with self.assertRaisesRegex(HeadStoreError, "store_file_too_large"):
                NbtHeadStore(self.path, WORLD, KEY)

    def test_capacity_limits_only_new_coordinates_not_updates(self):
        with patch("endstone_player_heads.store.MAX_RECORDS", 2):
            self.store.set(POSITION, head())
            self.store.set(("Nether", 1, 64, 0), head())
            with self.assertRaisesRegex(HeadStoreError, "too_many_records"):
                self.store.set(("Nether", 2, 64, 0), head())
            self.store.set(POSITION, replace(head(), victim_name="Renamed"))
            self.assertEqual(self.store.count, 2)

    def test_invalid_position_cannot_mutate_existing_store(self):
        positions = [("Overworld", True, 64, 0), ("Overworld", 1.0, 64, 0),
                     ("Overworld", 30_000_001, 64, 0), ("Overworld", 0, -2049, 0),
                     ("Overworld", 0, 2048, 0), ("over|world", 0, 64, 0),
                     (" Overworld", 0, 64, 0), ("", 0, 64, 0), ("A\nB", 0, 64, 0)]
        for position in positions:
            with self.subTest(position=position), self.assertRaises(HeadStoreError):
                self.store.set(position, head())
        self.assertEqual(self.store.count, 0)

    def test_world_and_key_validation_do_not_create_file(self):
        for world in ("", " x", "A\x00B", "x" * 129):
            with self.subTest(world=world), self.assertRaises(HeadStoreError):
                NbtHeadStore(self.path, world, KEY)
        with self.assertRaises(HeadStoreError):
            NbtHeadStore(self.path, WORLD, b"short")
        self.assertFalse(self.path.exists())

    def test_injectable_codec_loads_and_dumps_are_used(self):
        class JsonCodec:
            def loads(self, raw):
                return json.loads(raw)

            def dumps(self, root):
                return json.dumps(root).encode("utf-8")

        store = NbtHeadStore(self.path, WORLD, KEY, codec=JsonCodec())
        store.set(POSITION, head())
        store.flush()
        restarted = NbtHeadStore(self.path, WORLD, KEY, codec=JsonCodec())
        self.assertEqual(restarted.get(POSITION), head())

    def test_byte_accounting_matches_actual_nbt_before_after_restart_and_pop(self):
        initial_bytes = self.store.byte_size
        first_size = self.store.entry_size(POSITION, head())
        second_position = ("Nether", -30_000_000, -64, 30_000_000)
        unicode_head = replace(head(), victim_name="😀" * 64, killer_name="你好" * 32)
        second_size = self.store.entry_size(second_position, unicode_head)
        self.store.set(POSITION, head())
        self.store.set(second_position, unicode_head)
        self.assertEqual(self.store.byte_size, initial_bytes + first_size + second_size)
        self.store.flush()
        self.assertEqual(len(self.path.read_bytes()), self.store.byte_size)
        restarted = NbtHeadStore(self.path, WORLD, KEY)
        self.assertEqual(restarted.byte_size, self.store.byte_size)
        self.assertEqual(restarted.existing_entry_size(POSITION), first_size)
        restarted.pop(POSITION)
        self.assertEqual(restarted.byte_size, initial_bytes + second_size)
        restarted.flush()
        self.assertEqual(len(self.path.read_bytes()), restarted.byte_size)

    def test_byte_capacity_preflight_blocks_new_head_before_mutation(self):
        first_size = self.store.entry_size(POSITION, head())
        exact_capacity = self.store.byte_size + first_size
        with patch("endstone_player_heads.store.MAX_FILE_BYTES", exact_capacity):
            self.assertTrue(self.store.can_set(POSITION, head()))
            self.assertEqual(self.store.count, 0)
            self.assertFalse(self.store.dirty)
            self.store.set(POSITION, head())
            self.store.flush()
            before = self.path.read_bytes()
            self.assertEqual(len(before), exact_capacity)
            self.assertEqual(self.store.remaining_bytes, 0)
            second = ("Overworld", 13, 64, -4)
            with self.assertRaisesRegex(HeadStoreError, "store_file_too_large"):
                self.store.can_set(second, head())
            with self.assertRaisesRegex(HeadStoreError, "store_file_too_large"):
                self.store.set(second, head())
            self.assertEqual(self.store.count, 1)
            self.assertFalse(self.store.dirty)
            self.assertEqual(self.store.get(POSITION), head())
            self.assertEqual(self.path.read_bytes(), before)

    def test_larger_replacement_is_blocked_and_smaller_replacement_frees_capacity(self):
        exact_capacity = self.store.byte_size + self.store.entry_size(POSITION, head())
        with patch("endstone_player_heads.store.MAX_FILE_BYTES", exact_capacity):
            self.store.set(POSITION, head())
            self.store.flush()
            before = self.path.read_bytes()
            larger = replace(head(), victim_name="😀" * 64)
            with self.assertRaisesRegex(HeadStoreError, "store_file_too_large"):
                self.store.set(POSITION, larger)
            self.assertEqual(self.store.get(POSITION), head())
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(self.store.dirty)
            smaller = replace(head(), victim_name="A")
            self.assertTrue(self.store.can_set(POSITION, smaller))
            self.store.set(POSITION, smaller)
            self.assertGreater(self.store.remaining_bytes, 0)
            self.store.flush()
            self.assertEqual(len(self.path.read_bytes()), self.store.byte_size)

    def test_preflight_does_not_serialize_all_records(self):
        self.store.set(POSITION, head())
        with patch.object(self.store._codec, "dumps", side_effect=AssertionError("no whole-file serialization")):
            self.assertTrue(self.store.can_set(("Overworld", 13, 64, -4), head()))


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.root = {"version": 1, "world": WORLD, "entries": {}, "signature": "0" * 64}

    def test_empty_binary_nbt_round_trip(self):
        self.assertEqual(BoundedNbtCodec.loads(BoundedNbtCodec.dumps(self.root)), self.root)

    def test_unknown_recursive_nbt_is_rejected_before_traversal(self):
        field = b"recursive"
        raw = b"\x0a\x00\x00\x0a" + struct.pack("<H", len(field)) + field + b"\x0a" * 10000
        with self.assertRaisesRegex(HeadStoreError, "invalid_store_field"):
            BoundedNbtCodec.loads(raw)

    def test_truncated_trailing_or_wrong_root_is_rejected(self):
        normal = BoundedNbtCodec.dumps(self.root)
        for raw in (b"", normal[:-1], normal + b"extra", b"\x09" + normal[1:]):
            with self.subTest(size=len(raw)), self.assertRaises(HeadStoreError):
                BoundedNbtCodec.loads(raw)

    def test_coordinate_key_canonicalization_rejects_aliases(self):
        for key in ("Overworld|01|64|0", "Overworld|-0|64|0", "Overworld|+1|64|0",
                    "Overworld|30000001|64|0", "Overworld|1|64|0|extra"):
            root = {**self.root, "entries": {key: "{}"}}
            with self.subTest(key=key), self.assertRaises(HeadStoreError):
                BoundedNbtCodec.dumps(root)

    def test_duplicate_root_and_coordinate_names_are_rejected(self):
        def string(value):
            raw = value.encode("utf-8")
            return struct.pack("<H", len(raw)) + raw

        version = b"\x03" + string("version") + struct.pack("<i", 1)
        duplicate_version = b"\x0a\x00\x00" + version + version + b"\x00"
        entry = b"\x08" + string("Overworld|1|64|0") + string("{}")
        duplicate_entry = b"\x0a\x00\x00\x0a" + string("entries") + entry + entry + b"\x00\x00"
        with self.assertRaisesRegex(HeadStoreError, "duplicate_root_field"):
            BoundedNbtCodec.loads(duplicate_version)
        with self.assertRaisesRegex(HeadStoreError, "duplicate_coordinate_key"):
            BoundedNbtCodec.loads(duplicate_entry)


if __name__ == "__main__":
    unittest.main()
