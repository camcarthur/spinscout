from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Any

import httpx


UNPAVED_SURFACES = {
    "DIRT",
    "EARTH",
    "FINE_GRAVEL",
    "GRASS",
    "GRAVEL",
    "GROUND",
    "MUD",
    "PEBBLESTONE",
    "ROCK",
    "SAND",
    "UNPAVED",
}
TRAIL_ROAD_CLASSES = {"PATH", "TRACK"}
MAJOR_ROAD_CLASSES = {"MOTORWAY", "TRUNK", "PRIMARY", "SECONDARY"}


class RoutingError(Exception):
    pass


@dataclass
class GeneratedRoute:
    route_polyline: str
    points: list[tuple[float, float]]
    distance_miles: float | None
    elevation_ft: float | None
    duration_min: int | None
    trail_percent: float | None
    bike_network_percent: float | None
    unpaved_percent: float | None
    major_road_percent: float | None
    surface_summary: str | None
    provider: str = "GraphHopper"


def encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10**precision
    output: list[str] = []
    last_lat = 0
    last_lng = 0

    for lat, lng in points:
        lat_value = int(round(lat * factor))
        lng_value = int(round(lng * factor))
        for value in (lat_value - last_lat, lng_value - last_lng):
            shifted = value << 1
            if value < 0:
                shifted = ~shifted
            while shifted >= 0x20:
                output.append(chr((0x20 | (shifted & 0x1F)) + 63))
                shifted >>= 5
            output.append(chr(shifted + 63))
        last_lat = lat_value
        last_lng = lng_value

    return "".join(output)


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    if not encoded:
        return []

    factor = 10**precision
    points: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0

    while index < len(encoded):
        shift = 0
        result = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1

        shift = 0
        result = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lng += ~(result >> 1) if result & 1 else result >> 1

        points.append((lat / factor, lng / factor))

    return points


def haversine_meters(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    radius_m = 6371000.0
    lat1 = math.radians(point_a[0])
    lat2 = math.radians(point_b[0])
    delta_lat = math.radians(point_b[0] - point_a[0])
    delta_lng = math.radians(point_b[1] - point_a[1])
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def route_distance_meters(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(haversine_meters(first, second) for first, second in zip(points, points[1:]))


def detail_shares(
    detail_entries: list[list[Any]] | None,
    points: list[tuple[float, float]],
) -> dict[str, float]:
    if not detail_entries or len(points) < 2:
        return {}

    totals: dict[str, float] = defaultdict(float)
    total_distance = route_distance_meters(points)
    if total_distance <= 0:
        return {}

    for entry in detail_entries:
        if len(entry) < 3:
            continue
        start_idx = int(entry[0])
        end_idx = int(entry[1])
        value = str(entry[2]).upper()
        segment_distance = 0.0
        last_idx = min(end_idx, len(points) - 1)
        for idx in range(max(0, start_idx), last_idx):
            segment_distance += haversine_meters(points[idx], points[idx + 1])
        totals[value] += segment_distance

    return {key: value / total_distance for key, value in totals.items() if value > 0}


def summarize_surface_mix(surface_shares: dict[str, float]) -> str | None:
    if not surface_shares:
        return None

    ordered = sorted(surface_shares.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return None

    top_surface, top_ratio = ordered[0]
    top_label = top_surface.replace("_", " ").lower()
    if top_ratio >= 0.75:
        return f"Mostly {top_label}."

    if len(ordered) == 1:
        return f"Mostly {top_label}."

    second_surface, second_ratio = ordered[1]
    second_label = second_surface.replace("_", " ").lower()
    combined = (top_ratio + second_ratio) * 100
    return f"Mostly {top_label} and {second_label} ({combined:.0f}% combined)."


def infer_elevation_ft(raw_points: list[list[float]], fallback_ascend_m: float | None) -> float | None:
    if raw_points and len(raw_points[0]) >= 3:
        ascent_m = 0.0
        previous = raw_points[0][2]
        for point in raw_points[1:]:
            elevation = point[2]
            if elevation > previous:
                ascent_m += elevation - previous
            previous = elevation
        return ascent_m * 3.28084

    if fallback_ascend_m is not None:
        return float(fallback_ascend_m) * 3.28084

    return None


def resolve_profile(desired_style: str | None, intensity_label: str | None, sport_type: str | None) -> str:
    normalized_style = (desired_style or "").lower()
    normalized_sport = (sport_type or "").lower()
    normalized_intensity = (intensity_label or "").lower()

    if normalized_sport in {"mountainbikeride", "mtb"}:
        return "mtb"
    if normalized_style in {"gravel", "adventure"}:
        return "mtb"
    if normalized_sport in {"ride", "road", "roadride"} and normalized_intensity in {"hard", "training"}:
        return "racingbike"
    if normalized_style in {"hard", "training"}:
        return "racingbike"
    return "bike"


class GraphHopperClient:
    def __init__(self, *, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def generate_round_trip(
        self,
        *,
        start_lat: float,
        start_lng: float,
        distance_miles: float,
        seed: int,
        desired_style: str | None,
        intensity_label: str | None,
        sport_type: str | None,
    ) -> GeneratedRoute:
        route_distance_m = max(5000, int(distance_miles * 1609.34))
        payload = {
            "profile": resolve_profile(desired_style, intensity_label, sport_type),
            "points": [[start_lng, start_lat]],
            "algorithm": "round_trip",
            "round_trip.distance": route_distance_m,
            "round_trip.seed": seed,
            "instructions": False,
            "points_encoded": False,
            "elevation": True,
            "details": ["surface", "road_class", "bike_network"],
        }

        return await self._request_route(payload)

    async def generate_route(
        self,
        *,
        points: list[tuple[float, float]],
        desired_style: str | None,
        intensity_label: str | None,
        sport_type: str | None,
    ) -> GeneratedRoute:
        if len(points) < 2:
            raise RoutingError("At least two route points are required.")

        payload = {
            "profile": resolve_profile(desired_style, intensity_label, sport_type),
            "points": [[lng, lat] for lat, lng in points],
            "instructions": False,
            "points_encoded": False,
            "elevation": True,
            "details": ["surface", "road_class", "bike_network"],
        }

        return await self._request_route(payload)

    async def _request_route(self, payload: dict[str, Any]) -> GeneratedRoute:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{self.base_url}/route",
                params={"key": self.api_key},
                json=payload,
            )

            if (
                response.status_code >= 400
                and payload.get("profile") != "bike"
                and self._should_retry_with_bike(response)
            ):
                fallback_payload = dict(payload)
                fallback_payload["profile"] = "bike"
                response = await client.post(
                    f"{self.base_url}/route",
                    params={"key": self.api_key},
                    json=fallback_payload,
                )

        if response.status_code >= 400:
            raise RoutingError(self._error_message(response))

        data = response.json()
        paths = data.get("paths") if isinstance(data, dict) else None
        if not isinstance(paths, list) or not paths:
            raise RoutingError("GraphHopper returned no route options for that start point.")

        path = paths[0]
        points_geojson = path.get("points") or {}
        raw_coordinates = points_geojson.get("coordinates") if isinstance(points_geojson, dict) else None
        if not isinstance(raw_coordinates, list) or len(raw_coordinates) < 2:
            raise RoutingError("GraphHopper returned a route without usable map geometry.")

        points = [(float(item[1]), float(item[0])) for item in raw_coordinates if len(item) >= 2]
        if len(points) < 2:
            raise RoutingError("GraphHopper returned a route without enough coordinates.")

        details = path.get("details") if isinstance(path, dict) else {}
        surface_shares = detail_shares(details.get("surface"), points) if isinstance(details, dict) else {}
        road_class_shares = detail_shares(details.get("road_class"), points) if isinstance(details, dict) else {}
        bike_network_shares = detail_shares(details.get("bike_network"), points) if isinstance(details, dict) else {}

        trail_percent = sum(share for value, share in road_class_shares.items() if value in TRAIL_ROAD_CLASSES) * 100
        major_road_percent = (
            sum(share for value, share in road_class_shares.items() if value in MAJOR_ROAD_CLASSES) * 100
        )
        bike_network_percent = sum(share for value, share in bike_network_shares.items() if value != "MISSING") * 100
        unpaved_percent = sum(share for value, share in surface_shares.items() if value in UNPAVED_SURFACES) * 100

        distance_miles_value = float(path.get("distance", 0.0)) * 0.000621371 or None
        duration_min = round(float(path.get("time", 0.0)) / 60000) if path.get("time") is not None else None
        elevation_ft = infer_elevation_ft(raw_coordinates, path.get("ascend"))

        return GeneratedRoute(
            route_polyline=encode_polyline(points),
            points=points,
            distance_miles=round(distance_miles_value, 2) if distance_miles_value is not None else None,
            elevation_ft=round(elevation_ft, 0) if elevation_ft is not None else None,
            duration_min=duration_min,
            trail_percent=round(trail_percent, 0),
            bike_network_percent=round(bike_network_percent, 0),
            unpaved_percent=round(unpaved_percent, 0),
            major_road_percent=round(major_road_percent, 0),
            surface_summary=summarize_surface_mix(surface_shares),
        )

    def _should_retry_with_bike(self, response: httpx.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False

        messages: list[str] = []
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                messages.append(message)
            hints = payload.get("hints")
            if isinstance(hints, list):
                for hint in hints:
                    if isinstance(hint, dict):
                        hint_message = hint.get("message")
                        if isinstance(hint_message, str):
                            messages.append(hint_message)

        combined = " ".join(messages).lower()
        return "profile parameter can only be one of [car, bike, foot]" in combined

    def _error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code in {401, 403}:
            return "GraphHopper rejected the API key. Set a valid GRAPHHOPPER_API_KEY and restart the API."
        if response.status_code == 429:
            return "GraphHopper rate limit reached. Try again in a moment."

        if isinstance(payload, dict):
            hints = payload.get("hints")
            if isinstance(hints, list) and hints:
                message = hints[0].get("message")
                if isinstance(message, str) and message.strip():
                    return message
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                if "flexible mode" in message.lower():
                    return (
                        "Your GraphHopper plan rejected a flexible-mode request. "
                        "Spin Scout now uses standard profiles, so restart the API and try again."
                    )
                return message

        return f"GraphHopper route generation failed with status {response.status_code}."
