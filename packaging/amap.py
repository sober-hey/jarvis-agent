"""Amap (高德地图) client: reverse geocoding and nearby POI search.

Stdlib only. The Web service key comes from ``AMAP_API_KEY`` or an explicit
constructor argument and is never placed in results, error text or logs.
Requests are pinned to HTTPS ``restapi.amap.com``, reject redirects, cap the
response body and use a bounded timeout. Amap expects GCJ-02 coordinates in
mainland China, so WGS-84 input (for example Overland) is converted first.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

HOST: Final = "restapi.amap.com"
BASE_URL: Final = "https://" + HOST
USER_AGENT: Final = "jarvis-amap/1"
MAX_BODY: Final = 262_144
MAX_KEY: Final = 128
MAX_KEYWORDS: Final = 64
DEFAULT_TIMEOUT: Final = 5.0
DEFAULT_RADIUS: Final = 1000
MAX_RADIUS: Final = 50_000
MAX_LIMIT: Final = 25
_ERROR_CODE: Final = re.compile(r"[A-Za-z0-9_]{1,48}", re.ASCII)

type Transport = Callable[[urllib.request.Request, float], bytes]


class AmapError(RuntimeError):
    """A bounded, non-secret reason code for a failed Amap lookup."""

    def __init__(self, code: str) -> None:
        self.code = code if _ERROR_CODE.fullmatch(code) else "amap_error"
        super().__init__(self.code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Key-bearing query strings must never follow a redirect to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise AmapError("redirect_rejected")


def _https_transport(request: urllib.request.Request, timeout: float) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise AmapError("http_status")
            body = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError:
        raise AmapError("http_status") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AmapError("network_unavailable") from None
    if len(body) > MAX_BODY:
        raise AmapError("response_too_large")
    return body


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _valid_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Amap key must be text")
    key = value.strip()
    if not 1 <= len(key) <= MAX_KEY or any(ch.isspace() or not ch.isprintable() for ch in key):
        raise ValueError("Invalid Amap key")
    return key


_DEFAULT_KEY_FILE = Path.home() / ".jarvis" / "amap.key"


def _key_from_file(path: Path) -> str:
    """Read and validate a key from a file; raises ValueError on any failure."""
    try:
        return _valid_key(path.read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise ValueError(f"key file unreadable: {exc}") from exc


def configured() -> bool:
    """Whether a syntactically valid key is present (env var or default key file)."""
    # 1. Environment variable wins.
    if os.environ.get("AMAP_API_KEY", "").strip():
        try:
            _valid_key(os.environ["AMAP_API_KEY"])
            return True
        except ValueError:
            return False
    # 2. Fall back to ~/.jarvis/amap.key.
    try:
        _key_from_file(_DEFAULT_KEY_FILE)
        return True
    except ValueError:
        return False


def _coordinate(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} out of range")
    return result


def _radius(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RADIUS:
        raise ValueError("radius must be an integer from 1 to 50000")
    return value


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise ValueError("limit must be an integer from 1 to 25")
    return value


def _regeo_radius(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3000:
        raise ValueError("reverse geocode radius must be an integer from 0 to 3000")
    return value


def _keywords(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("keywords must be text")
    text = value.strip()
    if not 1 <= len(text) <= MAX_KEYWORDS or any(not ch.isprintable() for ch in text):
        raise ValueError("invalid keywords")
    return text


def _text(value: object, limit: int) -> str:
    """Allowlist one printable scalar; Amap uses [] for absent string fields."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:limit]


def _number(value: object) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 1_000_000 else None


def _location(value: object) -> tuple[float, float] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        lon = float(parts[0])
        lat = float(parts[1])
    except ValueError:
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        return None
    return round(lat, 6), round(lon, 6)


def _out_of_china(latitude: float, longitude: float) -> bool:
    return not (72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271)


def wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    """Approximate the GCJ-02 offset used by Amap; identity outside China."""
    if _out_of_china(latitude, longitude):
        return latitude, longitude

    def transform_lat(x: float, y: float) -> float:
        value = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        value += (
            (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        )
        value += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        value += (
            (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0))
            * 2.0
            / 3.0
        )
        return value

    def transform_lon(x: float, y: float) -> float:
        value = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        value += (
            (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        )
        value += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        value += (
            (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi))
            * 2.0
            / 3.0
        )
        return value

    a = 6_378_245.0
    ee = 0.00669342162296594323
    delta_lat = transform_lat(longitude - 105.0, latitude - 35.0)
    delta_lon = transform_lon(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = 1 - ee * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    delta_lat = (delta_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    delta_lon = (delta_lon * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return round(latitude + delta_lat, 6), round(longitude + delta_lon, 6)


def _query_point(latitude: object, longitude: object, coord_type: object) -> tuple[float, float]:
    lat = _coordinate(latitude, "latitude", -90.0, 90.0)
    lon = _coordinate(longitude, "longitude", -180.0, 180.0)
    if coord_type == "gcj02":
        return lat, lon
    if coord_type != "wgs84":
        raise ValueError("coord_type must be wgs84 or gcj02")
    return wgs84_to_gcj02(lat, lon)


class AmapClient:
    """Bounded, redirect-free Web service client; one instance per key."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ) -> None:
        self._key = _valid_key(api_key)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0.1 <= float(timeout) <= 30.0
        ):
            raise ValueError("timeout must be between 0.1 and 30 seconds")
        self.timeout = float(timeout)
        self._transport = transport or _https_transport

    @classmethod
    def from_environment(cls) -> AmapClient | None:
        """Return a client from ``AMAP_API_KEY`` env var or ~/.jarvis/amap.key, else None."""
        # 1. Environment variable wins.
        key = os.environ.get("AMAP_API_KEY", "").strip()
        if key:
            try:
                return cls(key)
            except ValueError:
                return None
        # 2. Fall back to the default key file.
        try:
            file_key = _key_from_file(_DEFAULT_KEY_FILE)
            return cls(file_key)
        except ValueError:
            return None

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            method="GET",
        )
        body = self._transport(request, self.timeout)
        if len(body) > MAX_BODY:
            raise AmapError("response_too_large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise AmapError("invalid_response") from None
        if not isinstance(payload, dict):
            raise AmapError("invalid_response")
        if str(payload.get("status")) != "1":
            code = payload.get("infocode")
            reason = f"amap_{code}" if isinstance(code, (str, int)) else "amap_error"
            raise AmapError(reason)
        return payload

    def reverse_geocode(
        self,
        latitude: object,
        longitude: object,
        *,
        coord_type: str = "wgs84",
        radius: int = DEFAULT_RADIUS,
    ) -> dict[str, Any]:
        lat, lon = _query_point(latitude, longitude, coord_type)
        payload = self._get(
            "/v3/geocode/regeo",
            {
                "key": self._key,
                "location": f"{lon:.6f},{lat:.6f}",
                "extensions": "base",
                "radius": str(_regeo_radius(radius)),
            },
        )
        regeocode = payload.get("regeocode")
        if not isinstance(regeocode, dict):
            raise AmapError("invalid_response")
        component = regeocode.get("addressComponent")
        if not isinstance(component, dict):
            component = {}
        formatted = _text(regeocode.get("formatted_address"), 200)
        province = _text(component.get("province"), 40)
        city = _text(component.get("city"), 40)
        district = _text(component.get("district"), 40)
        township = _text(component.get("township"), 40)
        if not formatted:
            formatted = "".join(part for part in (province, city, district, township) if part)
        return {
            "provider": "amap",
            "kind": "reverse_geocode",
            "coordinate_system": "gcj02",
            "latitude": round(_coordinate(latitude, "latitude", -90.0, 90.0), 6),
            "longitude": round(_coordinate(longitude, "longitude", -180.0, 180.0), 6),
            "province": province,
            "city": city,
            "district": district,
            "township": township,
            "adcode": _text(component.get("adcode"), 12),
            "formatted_address": formatted,
            "fetched_at": _now(),
        }

    def search_poi(
        self,
        latitude: object,
        longitude: object,
        keywords: object,
        *,
        coord_type: str = "wgs84",
        radius: int = DEFAULT_RADIUS,
        limit: int = 10,
    ) -> dict[str, Any]:
        keyword = _keywords(keywords)
        lat, lon = _query_point(latitude, longitude, coord_type)
        size = _limit(limit)
        payload = self._get(
            "/v3/place/around",
            {
                "key": self._key,
                "location": f"{lon:.6f},{lat:.6f}",
                "keywords": keyword,
                "radius": str(_radius(radius)),
                "offset": str(size),
                "page": "1",
                "extensions": "base",
                "sortrule": "distance",
            },
        )
        raw_pois = payload.get("pois")
        pois: list[dict[str, Any]] = []
        if isinstance(raw_pois, list):
            for item in raw_pois[:size]:
                if not isinstance(item, dict):
                    continue
                point = _location(item.get("location"))
                pois.append(
                    {
                        "id": _text(item.get("id"), 64),
                        "name": _text(item.get("name"), 100),
                        "type": _text(item.get("type"), 120),
                        "address": _text(item.get("address"), 160),
                        "distance_m": _number(item.get("distance")),
                        "latitude": point[0] if point else None,
                        "longitude": point[1] if point else None,
                    }
                )
        total = _number(payload.get("count"))
        return {
            "provider": "amap",
            "kind": "poi_search",
            "coordinate_system": "gcj02",
            "latitude": round(_coordinate(latitude, "latitude", -90.0, 90.0), 6),
            "longitude": round(_coordinate(longitude, "longitude", -180.0, 180.0), 6),
            "keywords": keyword,
            "radius_m": _radius(radius),
            "count": len(pois),
            "total": total if total is not None else len(pois),
            "pois": pois,
            "fetched_at": _now(),
        }


def reverse_geocode(
    latitude: object, longitude: object, *, client: AmapClient | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Convenience wrapper; ``client`` is injectable for offline tests."""
    selected = client or AmapClient.from_environment()
    if selected is None:
        raise AmapError("not_configured")
    return selected.reverse_geocode(latitude, longitude, **kwargs)


def search_poi(
    latitude: object,
    longitude: object,
    keywords: object,
    *,
    client: AmapClient | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convenience wrapper; ``client`` is injectable for offline tests."""
    selected = client or AmapClient.from_environment()
    if selected is None:
        raise AmapError("not_configured")
    return selected.search_poi(latitude, longitude, keywords, **kwargs)


def self_evaluate() -> dict[str, object]:
    """Describe source capabilities and gaps without accessing private state."""
    from datetime import datetime, timezone

    return {
        "module": "amap",
        "domain": "external map reverse geocoding and nearby point of interest lookup",
        "current_version": "bounded HTTPS Amap GCJ-02 reverse-geocode and first-page nearby POI "
        "client with WGS-84 coordinate approximation",
        "known_limitations": [
            "WGS-84 to GCJ-02 conversion is approximate and the mainland bounding "
            "rectangle is not a geographic border model.",
            "Nearby POI queries return only the first page, at most 25 results; no "
            "response cache, rate limiter or retry scheduler is implemented.",
            "Fallback key-file reads validate key syntax but do not enforce "
            "product_config owner, permission or no-symlink policy.",
        ],
        "improvement_directions": [
            "Add explicit pagination and a quota-aware cache with provenance "
            "and freshness metadata.",
            "Unify map key loading with a dedicated permission-checked "
            "credential boundary without exposing keys in diagnostics.",
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
