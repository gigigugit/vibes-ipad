"""Geocoding helpers for patient location lookup."""

from __future__ import annotations

import importlib
import time
from typing import Optional, Tuple

# Lazy-load geopy to avoid hard dependency
try:
    geopy_geocoders = importlib.import_module("geopy.geocoders")
    geopy_distance = importlib.import_module("geopy.distance")
    geopy_exc = importlib.import_module("geopy.exc")

    Nominatim = geopy_geocoders.Nominatim
    geodesic = geopy_distance.geodesic
    GeocoderTimedOut = getattr(geopy_exc, "GeocoderTimedOut", Exception)
    GeocoderServiceError = getattr(geopy_exc, "GeocoderServiceError", Exception)
    GEOCODER_ERRORS = (GeocoderTimedOut, GeocoderServiceError)
except ImportError:
    Nominatim = None  # type: ignore[assignment,misc]
    geodesic = None  # type: ignore[assignment,misc]
    GEOCODER_ERRORS = (Exception,)

from .config import CHIPPEWA_FALLS_QUERIES, GEOCODER_USER_AGENT

_CHIPPEWA_FALLS_CACHE: Optional[Tuple[float, float]] = None


def geocode_address(
    geolocator,
    address: Optional[str],
    retries: int = 2,
    delay: float = 0.5,
) -> Optional[Tuple[float, float]]:
    if not geolocator or not address:
        return None
    cleaned = " ".join(address.split())
    for attempt in range(retries):
        try:
            location = geolocator.geocode(cleaned)
            if location:
                return (location.latitude, location.longitude)
        except GEOCODER_ERRORS:
            pass
        except Exception:
            return None
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def get_chippewa_falls_coords(geolocator) -> Optional[Tuple[float, float]]:
    global _CHIPPEWA_FALLS_CACHE
    if _CHIPPEWA_FALLS_CACHE:
        return _CHIPPEWA_FALLS_CACHE
    if not geolocator:
        return None
    for query in CHIPPEWA_FALLS_QUERIES:
        coords = geocode_address(geolocator, query)
        if coords:
            _CHIPPEWA_FALLS_CACHE = coords
            return coords
    return None
