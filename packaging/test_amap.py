"""Offline tests for the Amap client and its JARVIS capability registration."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import amap
import capabilities
from senses.location import LocationAdapter

KEY = "test-amap-key-0123456789"
LOCATION_BATCH = (
    json.dumps(
        {
            "locations": [
                {
                    "properties": {"timestamp": "2026-10-18T10:00:00Z"},
                    "geometry": {"coordinates": [116.3975, 39.9087]},
                }
            ]
        }
    )
    + "\n"
)
Record = dict[str, Any]


def response(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


class _IsolatedAmapKeyTests(unittest.TestCase):
    """Never let an offline fixture inspect the installed user's map key."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="amap-key-fixture-")
        self.addCleanup(temporary.cleanup)
        self.key_path = Path(temporary.name) / "amap.key"
        key_override = patch.object(amap, "_DEFAULT_KEY_FILE", self.key_path)
        key_override.start()
        self.addCleanup(key_override.stop)


class ClientTests(_IsolatedAmapKeyTests):
    def test_reverse_geocode_is_pinned_bounded_and_sanitized(self) -> None:
        seen: Record = {}

        def transport(request: urllib.request.Request, timeout: float) -> bytes:
            seen.update(url=request.full_url, timeout=timeout)
            return response(
                {
                    "status": "1",
                    "info": "OK",
                    "infocode": "10000",
                    "regeocode": {
                        "formatted_address": "北京市东城区某街道 1 号",
                        "addressComponent": {
                            "province": "北京市",
                            "city": [],
                            "district": "东城区",
                            "township": "景山街道",
                            "adcode": "110101",
                            "streetNumber": {"street": "RAW_SECRET"},
                        },
                        "pois": [{"name": "RAW_SECRET"}],
                    },
                }
            )

        client = amap.AmapClient(KEY, timeout=2, transport=transport)
        result = client.reverse_geocode(39.9087, 116.3975)

        url = urlparse(str(seen["url"]))
        query = parse_qs(url.query)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, "restapi.amap.com")
        self.assertEqual(url.path, "/v3/geocode/regeo")
        self.assertEqual(query["key"], [KEY])
        self.assertEqual(query["extensions"], ["base"])
        self.assertEqual(query["radius"], ["1000"])
        gcj_lat, gcj_lon = amap.wgs84_to_gcj02(39.9087, 116.3975)
        self.assertEqual(query["location"], [f"{gcj_lon:.6f},{gcj_lat:.6f}"])
        self.assertEqual(seen["timeout"], 2)
        self.assertEqual(result["province"], "北京市")
        self.assertEqual(result["city"], "")
        self.assertEqual(result["district"], "东城区")
        self.assertEqual(result["township"], "景山街道")
        self.assertEqual(result["adcode"], "110101")
        self.assertEqual(result["coordinate_system"], "gcj02")
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("RAW_SECRET", rendered)
        self.assertNotIn(KEY, rendered)
        self.assertLess(len(rendered), 2000)

    def test_poi_search_bounds_fields_and_reports_distances(self) -> None:
        def transport(request: urllib.request.Request, timeout: float) -> bytes:
            self.assertIn("keywords=%E5%92%96%E5%95%A1", request.full_url)
            self.assertIn("offset=3", request.full_url)
            self.assertIn("sortrule=distance", request.full_url)
            return response(
                {
                    "status": "1",
                    "count": "42",
                    "pois": [
                        {
                            "id": "B0FF" + str(index),
                            "name": "店" * 300,
                            "type": "餐饮服务;" * 50,
                            "address": "地址" * 300,
                            "distance": "123",
                            "location": "116.400000,39.910000",
                        }
                        for index in range(8)
                    ]
                    + ["not-an-object"],
                }
            )

        client = amap.AmapClient(KEY, transport=transport)
        result = client.search_poi(39.9087, 116.3975, "咖啡", limit=3)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total"], 42)
        self.assertEqual(result["keywords"], "咖啡")
        self.assertEqual(result["radius_m"], 1000)
        self.assertEqual(len(result["pois"]), 3)
        first = result["pois"][0]
        self.assertEqual(first["distance_m"], 123)
        self.assertEqual(first["latitude"], 39.91)
        self.assertEqual(first["longitude"], 116.4)
        self.assertEqual(len(first["name"]), 100)
        self.assertEqual(len(first["type"]), 120)
        self.assertEqual(len(first["address"]), 160)
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 4000)

    def test_amap_error_status_is_reduced_to_a_code(self) -> None:
        client = amap.AmapClient(
            KEY,
            transport=lambda request, timeout: response(
                {
                    "status": "0",
                    "info": "INVALID_USER_KEY " + KEY,
                    "infocode": "10001",
                }
            ),
        )
        with self.assertRaises(amap.AmapError) as caught:
            client.reverse_geocode(39.9, 116.4)
        self.assertEqual(caught.exception.code, "amap_10001")
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn("INVALID_USER_KEY", str(caught.exception))

    def test_invalid_inputs_fail_before_transport(self) -> None:
        calls = 0

        def transport(request: urllib.request.Request, timeout: float) -> bytes:
            nonlocal calls
            calls += 1
            return response({"status": "1"})

        with self.assertRaises(ValueError):
            amap.AmapClient("key with space", transport=transport)
        with self.assertRaises(ValueError):
            amap.AmapClient(KEY, timeout=0, transport=transport)
        client = amap.AmapClient(KEY, transport=transport)
        for latitude, longitude in ((91, 0), (-91, 0), (0, 181), (0, -181), (True, 0)):
            with self.subTest(latitude=latitude, longitude=longitude):
                with self.assertRaises(ValueError):
                    client.reverse_geocode(latitude, longitude)
        for keywords in ("", "   ", "a\nb", "\x00", "长" * 65):
            with self.subTest(keywords=keywords):
                with self.assertRaises(ValueError):
                    client.search_poi(0, 0, keywords)
        for radius in (0, 50_001, True, 1.0):
            with self.subTest(radius=radius):
                with self.assertRaises(ValueError):
                    client.search_poi(0, 0, "咖啡", radius=radius)  # type: ignore[arg-type]
        for limit in (0, 26, True, 1.0):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    client.search_poi(0, 0, "咖啡", limit=limit)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            client.reverse_geocode(0, 0, coord_type="bd09")
        with self.assertRaises(ValueError):
            client.reverse_geocode(0, 0, radius=3001)
        self.assertEqual(calls, 0)

    def test_transport_rejects_oversized_and_malformed_bodies(self) -> None:
        oversized = amap.AmapClient(
            KEY, transport=lambda request, timeout: b"x" * (amap.MAX_BODY + 1)
        )
        with self.assertRaises(amap.AmapError) as too_large:
            oversized.reverse_geocode(0, 0)
        self.assertEqual(too_large.exception.code, "response_too_large")

        malformed = amap.AmapClient(KEY, transport=lambda request, timeout: b"not json")
        with self.assertRaises(amap.AmapError) as invalid:
            malformed.reverse_geocode(0, 0)
        self.assertEqual(invalid.exception.code, "invalid_response")

        no_code = amap.AmapClient(KEY, transport=lambda request, timeout: response({"status": "0"}))
        with self.assertRaises(amap.AmapError) as missing:
            no_code.reverse_geocode(0, 0)
        self.assertEqual(missing.exception.code, "amap_error")

    def test_redirects_are_rejected(self) -> None:
        handler = amap._NoRedirect()
        with self.assertRaises(amap.AmapError) as caught:
            handler.redirect_request(
                urllib.request.Request("https://restapi.amap.com/v3/geocode/regeo"),
                None,
                302,
                "Found",
                {},
                "https://example.invalid/",
            )
        self.assertEqual(caught.exception.code, "redirect_rejected")

    def test_coordinate_conversion_is_bounded_and_identity_outside_china(self) -> None:
        latitude, longitude = amap.wgs84_to_gcj02(39.9087, 116.3975)
        self.assertNotEqual((latitude, longitude), (39.9087, 116.3975))
        self.assertTrue(39.90 < latitude < 39.92)
        self.assertTrue(116.39 < longitude < 116.41)
        self.assertEqual(amap.wgs84_to_gcj02(35.0, -120.0), (35.0, -120.0))

    def test_environment_configuration_never_exposes_the_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(amap.configured())
            self.assertIsNone(amap.AmapClient.from_environment())
            with self.assertRaises(amap.AmapError) as missing:
                amap.reverse_geocode(39.9, 116.4)
            self.assertEqual(missing.exception.code, "not_configured")
        with patch.dict(os.environ, {"AMAP_API_KEY": KEY}, clear=True):
            self.assertTrue(amap.configured())
            self.assertIsNotNone(amap.AmapClient.from_environment())
        with patch.dict(os.environ, {"AMAP_API_KEY": "bad key"}, clear=True):
            self.assertFalse(amap.configured())


class CapabilityTests(_IsolatedAmapKeyTests):
    def test_catalog_registration_is_exact_and_gated_by_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            entries = capabilities.amap_catalog()
        self.assertEqual(
            {entry["id"] for entry in entries},
            {"amap_reverse_geocode", "amap_nearby_poi"},
        )
        expected_fields = {
            "id",
            "name",
            "provides",
            "persona_description",
            "call",
            "costs_money",
            "security_sensitive",
            "precision",
            "freshness",
            "implementation",
            "available",
        }
        for entry in entries:
            self.assertEqual(set(entry), expected_fields)
            self.assertEqual(entry["implementation"], "amap_http")
            self.assertTrue(entry["costs_money"])
            self.assertTrue(entry["security_sensitive"])
            self.assertFalse(entry["available"])
            self.assertTrue(capabilities.recognized(entry))
            availability = {**entry, "available": True}
            self.assertTrue(capabilities.recognized(availability))
            for field in set(entry) - {"available"}:
                forged = {**entry, field: "forged"}
                self.assertFalse(capabilities.recognized(forged))
                with self.assertRaises(ValueError):
                    capabilities.execute(forged)
            with self.assertRaises(ValueError):
                capabilities.execute({**entry, "unexpected": "metadata"})
        with patch.dict(os.environ, {"AMAP_API_KEY": KEY}, clear=True):
            self.assertTrue(all(entry["available"] for entry in capabilities.amap_catalog()))

    def test_reverse_capability_uses_latest_local_coordinate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracks = Path(directory)
            (tracks / "day.jsonl").write_text(
                json.dumps(
                    {
                        "locations": [
                            {
                                "properties": {"timestamp": "2026-10-18T10:00:00Z"},
                                "geometry": {"coordinates": [116.3975, 39.9087]},
                            }
                        ]
                    }
                )
                + "\n"
            )
            adapter = LocationAdapter(tracks)
            entry = next(
                e for e in capabilities.amap_catalog() if e["id"] == "amap_reverse_geocode"
            )
            client = FakeClient()
            result = capabilities.execute(entry, adapters=[adapter], amap_client=client)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["source"], "amap")
        self.assertEqual(result["sampled_at"], "2026-10-18T10:00:00+00:00")
        self.assertEqual(result["province"], "北京市")
        self.assertEqual(client.calls[0][0], (39.9087, 116.3975))
        self.assertEqual(client.calls[0][1]["coord_type"], "wgs84")
        self.assertNotIn(KEY, json.dumps(result, ensure_ascii=False))

    def test_poi_capability_accepts_bounded_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracks = Path(directory)
            (tracks / "day.jsonl").write_text(
                json.dumps(
                    {
                        "locations": [
                            {
                                "properties": {"timestamp": "2026-10-18T10:00:00Z"},
                                "geometry": {"coordinates": [116.3975, 39.9087]},
                            }
                        ]
                    }
                )
                + "\n"
            )
            adapter = LocationAdapter(tracks)
            entry = next(e for e in capabilities.amap_catalog() if e["id"] == "amap_nearby_poi")
            client = FakeClient()
            result = capabilities.execute(
                entry,
                adapters=[adapter],
                amap_client=client,
                amap_arguments={"keywords": "咖啡", "limit": 3},
            )
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["keywords"], "咖啡")
        self.assertEqual(result["count"], 1)
        self.assertEqual(client.calls[0][0], (39.9087, 116.3975))
        self.assertEqual(client.calls[0][1]["keywords"], "咖啡")
        self.assertEqual(client.calls[0][1]["limit"], 3)
        with self.assertRaises(ValueError):
            capabilities.execute(
                entry, adapters=[adapter], amap_client=client, amap_arguments={"secret": 1}
            )

    def test_unconfigured_or_failed_lookup_is_unavailable(self) -> None:
        entry = capabilities.amap_catalog()[0]
        with patch.dict(os.environ, {}, clear=True):
            result = capabilities.execute(entry, adapters=[])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"], "not_configured")

        with tempfile.TemporaryDirectory() as directory:
            adapter = LocationAdapter(Path(directory))
            (Path(directory) / "day.jsonl").write_text(LOCATION_BATCH)
            failed = capabilities.execute(entry, adapters=[adapter], amap_client=FailingClient())
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(failed["error"], "amap_10003")

    def test_missing_location_does_not_call_amap(self) -> None:
        entry = capabilities.amap_catalog()[0]
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocationAdapter(Path(directory))
            result = capabilities.execute(entry, adapters=[adapter], amap_client=client)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"], "location_unavailable")
        self.assertEqual(client.calls, [])


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, Any]]] = []

    def reverse_geocode(self, latitude: object, longitude: object, **kwargs: Any) -> Record:
        self.calls.append(((latitude, longitude), kwargs))
        return {
            "provider": "amap",
            "kind": "reverse_geocode",
            "province": "北京市",
            "city": "",
            "district": "东城区",
            "township": "景山街道",
            "adcode": "110101",
            "formatted_address": "北京市东城区",
            "fetched_at": datetime.now(UTC).isoformat(),
        }

    def search_poi(
        self, latitude: object, longitude: object, keywords: object, **kwargs: Any
    ) -> Record:
        self.calls.append(((latitude, longitude), {"keywords": keywords, **kwargs}))
        return {
            "provider": "amap",
            "kind": "poi_search",
            "keywords": keywords,
            "radius_m": kwargs.get("radius", 1000),
            "count": 1,
            "total": 1,
            "pois": [{"name": "咖啡店"}],
            "fetched_at": datetime.now(UTC).isoformat(),
        }


class FailingClient:
    def reverse_geocode(self, latitude: object, longitude: object, **kwargs: Any) -> Record:
        raise amap.AmapError("amap_10003")

    def search_poi(
        self, latitude: object, longitude: object, keywords: object, **kwargs: Any
    ) -> Record:
        raise amap.AmapError("amap_10003")


class SelfEvaluationTests(unittest.TestCase):
    def test_owned_hook_returns_honest_schema_without_private_io(self):
        import importlib
        import json
        from datetime import datetime, timezone
        from unittest.mock import patch

        module = importlib.import_module("amap")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        before = datetime.now(timezone.utc)
        with (
            patch("builtins.open", side_effect=AssertionError("private read")),
            patch("pathlib.Path.open", side_effect=AssertionError("private read")),
            patch("sqlite3.connect", side_effect=AssertionError("database access")),
            patch("subprocess.Popen", side_effect=AssertionError("external process")),
            patch("socket.socket.connect", side_effect=AssertionError("network")),
        ):
            report = hook()
        self.assertEqual(
            set(report),
            {
                "module",
                "domain",
                "current_version",
                "known_limitations",
                "improvement_directions",
                "last_evaluated_at",
            },
        )
        self.assertEqual(report["module"], "amap")
        self.assertIn("GCJ-02", report["current_version"])
        for field in ("domain", "current_version"):
            self.assertGreater(len(report[field]), 10)
        for field in ("known_limitations", "improvement_directions"):
            self.assertTrue(report[field])
            self.assertTrue(all(isinstance(item, str) and len(item) > 15 for item in report[field]))
        evaluated = datetime.fromisoformat(report["last_evaluated_at"])
        self.assertIsNotNone(evaluated.utcoffset())
        self.assertGreaterEqual(evaluated, before)
        self.assertLessEqual(evaluated, datetime.now(timezone.utc))
        json.dumps(report)

    def test_reports_own_mutable_lists(self):
        import importlib

        module = importlib.import_module("amap")
        hook = getattr(module, "self_evaluate", None)
        self.assertTrue(callable(hook), "module-owned self_evaluate is missing")
        report = hook()
        report["known_limitations"].clear()
        report["improvement_directions"].clear()
        fresh = hook()
        self.assertTrue(fresh["known_limitations"])
        self.assertTrue(fresh["improvement_directions"])


if __name__ == "__main__":
    unittest.main()
