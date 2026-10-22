"""Offline policy tests; no microphones, Bluetooth or real device output."""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from dataclasses import replace
from itertools import permutations


class DeviceRoutingTests(unittest.TestCase):
    def test_single_device_wins(self):
        self.assertIsNotNone(importlib.util.find_spec("device_routing"))
        routing = importlib.import_module("device_routing")
        candidate = routing.DeviceState(device_id="mac-1", device_type="mac")
        self.assertEqual(routing.arbitrate([candidate]), "mac-1")

    def test_weighted_scoring_uses_all_four_factors(self):
        routing = importlib.import_module("device_routing")
        for factor, value in (
            ("signal_strength", 0.8),
            ("recently_used", 1.0),
            ("wearing", True),
            ("normalized_doa_variance", 0.0),
        ):
            with self.subTest(factor=factor):
                baseline = dict(normalized_doa_variance=1.0)
                better = dict(baseline, **{factor: value})
                weak = routing.DeviceState("weak", "iphone", **baseline)
                strong = routing.DeviceState("strong", "mac", **better)
                self.assertEqual(routing.arbitrate([weak, strong]), "strong")
                self.assertEqual(routing.arbitrate([strong, weak]), "strong")
        # Signal alone loses to the combined recent-use + wearing weights.
        signal = routing.DeviceState("signal", "iphone", signal_strength=1.0)
        recent_worn = routing.DeviceState("recent", "watch", recently_used=1.0, wearing=True)
        self.assertEqual(routing.arbitrate([signal, recent_worn]), "recent")

    def test_ties_follow_priority_not_candidate_arrival(self):
        routing = importlib.import_module("device_routing")
        candidates = [
            routing.DeviceState("speaker", "xiaoai", signal_strength=0.5),
            routing.DeviceState("mac", "mac", signal_strength=0.5),
            routing.DeviceState("phone", "iphone", signal_strength=0.5),
            routing.DeviceState("watch", "watch", wearing=True),
        ]
        # Watch wearing=1 contributes .20, identical to signal=.5 * .40.
        for subset, winner in (
            (candidates, "watch"),
            (candidates[:3], "phone"),
            (candidates[:2], "mac"),
        ):
            for order in permutations(subset):
                self.assertEqual(routing.arbitrate(list(order)), winner)
        same_kind = [routing.DeviceState("b", "mac"), routing.DeviceState("a", "mac")]
        for order in permutations(same_kind):
            self.assertEqual(routing.arbitrate(list(order)), "a")
        unworn = replace(candidates[3], wearing=False, signal_strength=0.5)
        self.assertEqual(routing.arbitrate([unworn, candidates[0]]), "speaker")

    def test_ineligible_devices_never_participate_or_win(self):
        routing = importlib.import_module("device_routing")
        self.assertTrue(hasattr(routing, "should_participate"))
        eligible = routing.DeviceState("phone", "iphone")
        self.assertTrue(routing.should_participate(eligible))
        for flag, value in (
            ("face_down", True),
            ("locked", True),
            ("bluetooth_enabled", False),
            ("online", False),
        ):
            with self.subTest(flag=flag):
                bad = routing.DeviceState("bad", "mac", signal_strength=1.0, **{flag: value})
                self.assertFalse(routing.should_participate(bad))
                self.assertEqual(routing.arbitrate([bad, eligible]), "phone")
                with self.assertRaisesRegex(ValueError, "eligible"):
                    routing.arbitrate([bad])
        with self.assertRaisesRegex(ValueError, "eligible"):
            routing.arbitrate([])

    def test_location_switches_speaker_and_preserves_home_fallback(self):
        routing = importlib.import_module("device_routing")
        self.assertTrue(hasattr(routing, "select_output_device"))
        devices = (
            routing.DeviceState("speaker", "xiaoai"),
            routing.DeviceState("watch", "watch", wearing=True),
            routing.DeviceState("phone", "iphone"),
            routing.DeviceState("mac", "mac"),
        )
        home = routing.DeviceContext(devices=devices, tailscale_online=True, mac_active=True)
        plan = routing.select_output_device(home)
        self.assertEqual((plan.device_id, plan.channel, plan.location), ("mac", "speaker", "home"))
        self.assertEqual(
            routing.select_output_device(replace(home, devices=devices[:3])).device_id, "phone"
        )
        # XiaoAI is not eligible on a coarse home heuristic alone.
        from unittest.mock import patch

        with patch.object(routing, "xiaoai_output_allowed", return_value=True):
            self.assertEqual(
                routing.select_output_device(replace(home, devices=devices[:3])).device_id,
                "speaker",
            )
        for online, active in ((False, True), (True, False), (False, False)):
            away = replace(home, tailscale_online=online, mac_active=active)
            self.assertEqual(routing.select_output_device(away).device_id, "phone")
            self.assertEqual(routing.select_output_device(away).location, "away")
            self.assertEqual(
                routing.select_output_device(replace(away, devices=devices[:2])).device_id, "watch"
            )
        # A remote home speaker must never be used when no portable is available.
        self.assertIsNone(
            routing.select_output_device(
                routing.DeviceContext(devices=(devices[0], devices[3]))
            ).device_id
        )
        self.assertIsNone(routing.select_output_device(routing.DeviceContext()).device_id)

    def test_headphones_override_location_even_on_locked_face_down_device(self):
        routing = importlib.import_module("device_routing")
        self.assertIn("headphones", routing.DeviceState.__dataclass_fields__)
        for kind in ("airpods", "wired"):
            phone = routing.DeviceState(
                "phone", "iphone", locked=True, face_down=True, headphones=kind
            )
            mac = routing.DeviceState("mac", "mac")
            self.assertFalse(routing.should_participate(phone))
            home = routing.DeviceContext(
                devices=(mac, phone), tailscale_online=True, mac_active=True
            )
            plan = routing.select_output_device(home)
            self.assertEqual((plan.device_id, plan.channel), ("phone", "headphones"))
            self.assertEqual(
                routing.select_output_device(replace(home, tailscale_online=False)).device_id,
                "phone",
            )
            disconnected = replace(phone, online=False)
            self.assertEqual(
                routing.select_output_device(replace(home, devices=(mac, disconnected))).device_id,
                "mac",
            )
        bluetooth_off = replace(phone, headphones="airpods", bluetooth_enabled=False)
        self.assertEqual(
            routing.select_output_device(replace(home, devices=(mac, bluetooth_off))).device_id,
            "mac",
        )
        wired = replace(bluetooth_off, headphones="wired")
        self.assertEqual(
            routing.select_output_device(replace(home, devices=(mac, wired))).device_id, "phone"
        )
        not_worn = replace(phone, headphones_worn=False)
        self.assertEqual(
            routing.select_output_device(replace(home, devices=(mac, not_worn))).device_id, "mac"
        )

    def test_invalid_telemetry_is_rejected_instead_of_winning(self):
        routing = importlib.import_module("device_routing")
        for field in ("signal_strength", "recently_used", "normalized_doa_variance"):
            for value in (-0.1, 1.1, float("nan"), float("inf"), "0.5"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    routing.DeviceState("mac", "mac", **{field: value})
        for changes in ({"device_id": ""}, {"device_type": "unknown"}, {"locked": "false"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                routing.DeviceState(**dict({"device_id": "mac", "device_type": "mac"}, **changes))

    def test_duplicate_device_ids_are_rejected(self):
        routing = importlib.import_module("device_routing")
        a = routing.DeviceState("same", "iphone")
        b = routing.DeviceState("same", "mac")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            routing.arbitrate([a, b])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            routing.DeviceContext(devices=(a, b))

    def test_mathematical_ties_are_not_broken_by_float_rounding(self):
        routing = importlib.import_module("device_routing")
        mac = routing.DeviceState("mac", "mac", signal_strength=0.5, normalized_doa_variance=0.0)
        phone = routing.DeviceState("phone", "iphone", recently_used=1.0)
        for order in permutations((mac, phone)):
            self.assertEqual(routing.arbitrate(list(order)), "phone")

    def test_self_evaluation_discloses_unwired_transport(self):
        routing = importlib.import_module("device_routing")
        self.assertTrue(hasattr(routing, "self_evaluate"))
        evaluation = routing.self_evaluate()
        for key in (
            "module",
            "domain",
            "current_version",
            "known_limitations",
            "improvement_directions",
            "last_evaluated_at",
        ):
            self.assertIn(key, evaluation)
        self.assertEqual(evaluation["module"], "device_routing")
        self.assertTrue(evaluation["known_limitations"])

    def test_context_rejects_nonboolean_location_flags_and_snapshots_input(self):
        routing = importlib.import_module("device_routing")
        for field in ("tailscale_online", "mac_active"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                routing.DeviceContext(**{field: "false"})
        devices = [routing.DeviceState("phone", "iphone")]
        context = routing.DeviceContext(devices=devices)
        devices.clear()
        self.assertEqual(routing.select_output_device(context).device_id, "phone")


if __name__ == "__main__":
    unittest.main()
