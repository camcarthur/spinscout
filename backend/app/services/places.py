from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import httpx


class PlacesError(Exception):
    pass


DESTINATION_FILTERS: dict[str, list[str]] = {
    "brewery": [
        'node["craft"="brewery"]',
        'way["craft"="brewery"]',
        'relation["craft"="brewery"]',
        'node["brewery"="yes"]',
        'way["brewery"="yes"]',
        'relation["brewery"="yes"]',
        'node["amenity"="pub"]["brewery"="yes"]',
        'way["amenity"="pub"]["brewery"="yes"]',
        'relation["amenity"="pub"]["brewery"="yes"]',
        'node["amenity"="bar"]["brewery"="yes"]',
        'way["amenity"="bar"]["brewery"="yes"]',
        'relation["amenity"="bar"]["brewery"="yes"]',
    ],
    "taco_shop": [
        'node["amenity"~"restaurant|fast_food|food_court"]["cuisine"~"mexican|taco|tex-mex",i]',
        'way["amenity"~"restaurant|fast_food|food_court"]["cuisine"~"mexican|taco|tex-mex",i]',
        'relation["amenity"~"restaurant|fast_food|food_court"]["cuisine"~"mexican|taco|tex-mex",i]',
    ],
    "coffee_shop": [
        'node["amenity"="cafe"]',
        'way["amenity"="cafe"]',
        'relation["amenity"="cafe"]',
    ],
    "bakery": [
        'node["shop"="bakery"]',
        'way["shop"="bakery"]',
        'relation["shop"="bakery"]',
    ],
    "lake": [
        'node["natural"="water"]["water"="lake"]',
        'way["natural"="water"]["water"="lake"]',
        'relation["natural"="water"]["water"="lake"]',
        'node["water"="lake"]',
        'way["water"="lake"]',
        'relation["water"="lake"]',
        'node["natural"="water"]["water"="reservoir"]',
        'way["natural"="water"]["water"="reservoir"]',
        'relation["natural"="water"]["water"="reservoir"]',
    ],
    "river": [
        'node["waterway"~"river|stream"]',
        'way["waterway"~"river|stream"]',
        'relation["waterway"~"river|stream"]',
    ],
    "park": [
        'node["leisure"="park"]',
        'way["leisure"="park"]',
        'relation["leisure"="park"]',
    ],
}
GRAVEL_SEGMENT_FILTERS = [
    'way["highway"~"track|path|bridleway|cycleway"]["surface"~"gravel|fine_gravel|dirt|ground|earth|unpaved|compacted",i]',
    'way["highway"="track"]',
    'way["highway"="path"]["tracktype"~"grade2|grade3|grade4|grade5"]',
]


@dataclass
class PlaceCandidate:
    category: str
    label: str
    lat: float
    lng: float
    distance_miles: float
    tags: dict[str, str]


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_miles = 3958.8
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2
    )
    return radius_miles * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def build_overpass_query(filters: list[str], *, lat: float, lng: float, radius_meters: int) -> str:
    lines = ["[out:json][timeout:25];", "("]
    for filter_expression in filters:
        lines.append(f"  {filter_expression}(around:{radius_meters},{lat:.6f},{lng:.6f});")
    lines.extend([");", "out center;"])
    return "\n".join(lines)


def resolve_label(category: str, tags: dict[str, str]) -> str:
    for key in ("name", "official_name", "short_name", "brand"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    category_label = category.replace("_", " ")
    street = tags.get("addr:street")
    if isinstance(street, str) and street.strip():
        return f"{category_label.title()} on {street.strip()}"
    return category_label.title()


def extract_candidate(
    *,
    category: str,
    element: dict[str, Any],
    origin_lat: float,
    origin_lng: float,
) -> PlaceCandidate | None:
    lat = element.get("lat")
    lng = element.get("lon")
    if lat is None or lng is None:
        center = element.get("center")
        if isinstance(center, dict):
            lat = center.get("lat")
            lng = center.get("lon")

    try:
        lat_value = float(lat)
        lng_value = float(lng)
    except (TypeError, ValueError):
        return None

    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    distance_miles = haversine_miles(origin_lat, origin_lng, lat_value, lng_value)
    return PlaceCandidate(
        category=category,
        label=resolve_label(category, tags),
        lat=lat_value,
        lng=lng_value,
        distance_miles=distance_miles,
        tags={str(key): str(value) for key, value in tags.items()},
    )


class OverpassPlacesClient:
    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def search_destinations(
        self,
        *,
        category: str,
        lat: float,
        lng: float,
        radius_miles: float,
        limit: int = 8,
    ) -> list[PlaceCandidate]:
        filters = DESTINATION_FILTERS.get(category)
        if not filters:
            return []
        return await self._search(
            filters=filters,
            category=category,
            lat=lat,
            lng=lng,
            radius_miles=radius_miles,
            limit=limit,
        )

    async def search_gravel_segments(
        self,
        *,
        lat: float,
        lng: float,
        radius_miles: float,
        limit: int = 6,
    ) -> list[PlaceCandidate]:
        return await self._search(
            filters=GRAVEL_SEGMENT_FILTERS,
            category="gravel_segment",
            lat=lat,
            lng=lng,
            radius_miles=radius_miles,
            limit=limit,
        )

    async def _search(
        self,
        *,
        filters: list[str],
        category: str,
        lat: float,
        lng: float,
        radius_miles: float,
        limit: int,
    ) -> list[PlaceCandidate]:
        radius_meters = max(800, int(radius_miles * 1609.34))
        query = build_overpass_query(filters, lat=lat, lng=lng, radius_meters=radius_meters)

        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(
                self.base_url,
                data={"data": query},
            )

        if response.status_code >= 400:
            detail = response.text.strip()
            if detail:
                detail = detail.splitlines()[0]
                raise PlacesError(
                    f"OpenStreetMap place search failed with status {response.status_code}: {detail}"
                )
            raise PlacesError(f"OpenStreetMap place search failed with status {response.status_code}.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise PlacesError("OpenStreetMap place search returned invalid data.") from exc

        elements = payload.get("elements") if isinstance(payload, dict) else None
        if not isinstance(elements, list):
            return []

        seen: set[str] = set()
        candidates: list[PlaceCandidate] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            candidate = extract_candidate(category=category, element=element, origin_lat=lat, origin_lng=lng)
            if candidate is None:
                continue

            key = f"{round(candidate.lat, 4)}:{round(candidate.lng, 4)}:{candidate.label.lower()}"
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

        candidates.sort(key=lambda item: item.distance_miles)
        return candidates[:limit]
