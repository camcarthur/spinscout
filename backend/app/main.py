from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import re
import urllib.parse
from xml.sax.saxutils import escape

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, engine, get_db, migrate_large_strava_ids
from app.models import Activity, RideFeedback, User
from app.schemas import (
    ActivityOut,
    GeneratedRouteOptionOut,
    IncludedSegmentOut,
    RecommendationOut,
    RecommendationRequest,
    RouteExportRequest,
    RidePlanOut,
    RidePlanRequest,
    RideFeedbackCreate,
    RideFeedbackOut,
    UserOut,
)
from app.services.candidates import (
    CandidatePlanInputs,
    deduplicate_by_bearing,
    plan_candidate_requests,
)
from app.services.places import OverpassPlacesClient, PlaceCandidate, PlacesError
from app.services.routing import GeneratedRoute, GraphHopperClient, RoutingError, decode_polyline, encode_polyline
from app.services.scoring import (
    POPULARITY_OVERLAP_THRESHOLD,
    RideContext,
    distance_tolerance_miles,
    filter_bike_path_candidates,
    filter_destination_candidates,
    filter_gravel_candidates,
    gravel_signal,
    route_within_distance_goal,
    score_candidate,
)
from app.services.strava import StravaClient, ensure_valid_token, upsert_activities, upsert_user_from_token_data

app = FastAPI(title=settings.app_name)
client = StravaClient()
routing_client = GraphHopperClient(
    api_key=settings.graphhopper_api_key,
    base_url=settings.graphhopper_base_url,
)
places_client = OverpassPlacesClient(base_url=settings.overpass_api_url)
ROUTE_EXPORT_DEFAULTS: dict[str, dict[str, str]] = {
    "strava": {"label": "Strava Routes", "format": "gpx"},
    "garmin": {"label": "Garmin Connect", "format": "tcx"},
    "wahoo": {"label": "Wahoo ELEMNT", "format": "gpx"},
    "ridewithgps": {"label": "Ride with GPS", "format": "gpx"},
    "komoot": {"label": "Komoot", "format": "gpx"},
    "hammerhead": {"label": "Hammerhead", "format": "gpx"},
    "other": {"label": "Other GPS App", "format": "gpx"},
}

LOCAL_FRONTEND_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}
STYLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "brewery": ("brewery", "beer", "patio", "taproom"),
    "social": ("social", "group ride", "group", "friends"),
    "casual": ("casual", "coffee", "easy spin", "easy"),
    "training": ("training", "tempo", "workout", "interval", "threshold"),
    "hard": ("hard", "intense", "smash", "race", "drop ride"),
    "gravel": ("gravel", "dirt", "mixed surface", "unpaved", "fire road", "double track", "singletrack"),
    "adventure": ("adventure", "scenic", "explore", "wander"),
    "endurance": ("endurance", "steady", "zone 2", "base"),
    "recovery": ("recovery", "recovery spin", "gentle", "low stress"),
}
DESTINATION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "brewery": ("brewery", "taproom", "brew pub", "brewpub", "beer"),
    "taco_shop": ("taco", "taqueria", "burrito", "mexican food"),
    "coffee_shop": ("coffee", "cafe", "espresso", "latte"),
    "bakery": ("bakery", "pastry", "donut", "croissant"),
    "lake": ("lake", "reservoir"),
    "river": ("river", "creek", "waterfront"),
    "park": ("park",),
}
DESTINATION_METADATA: dict[str, dict[str, str | float]] = {
    "brewery": {"label": "brewery", "threshold_miles": 0.35, "radius_multiplier": 0.55},
    "taco_shop": {"label": "taco shop", "threshold_miles": 0.35, "radius_multiplier": 0.55},
    "coffee_shop": {"label": "coffee shop", "threshold_miles": 0.35, "radius_multiplier": 0.5},
    "bakery": {"label": "bakery", "threshold_miles": 0.35, "radius_multiplier": 0.5},
    "lake": {"label": "lake", "threshold_miles": 0.75, "radius_multiplier": 0.75},
    "river": {"label": "river", "threshold_miles": 0.6, "radius_multiplier": 0.75},
    "park": {"label": "park", "threshold_miles": 0.45, "radius_multiplier": 0.65},
}
INTENSITY_PROFILES: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("hard", ("hard", "intense", "smash", "race"), 8),
    ("training", ("training", "tempo", "workout", "interval", "threshold"), 7),
    ("endurance", ("endurance", "steady", "zone 2", "base"), 5),
    ("recovery", ("recovery", "easy", "coffee", "casual", "social", "brewery"), 3),
)
POPULARITY_KEYWORDS: tuple[str, ...] = (
    "popular with cyclists",
    "popular with locals",
    "popular roads",
    "popular route",
    "well ridden",
    "well-ridden",
    "cyclist favorite",
    "local favorite",
    "locals ride",
    "heatmap",
)

# When the rider explicitly asks for protected/separated cycling infrastructure
# we want to anchor the route through OSM-tagged cycleways and reward the
# `bike_network` GraphHopper detail more aggressively.
BIKE_PATH_KEYWORDS: tuple[str, ...] = (
    "bike path",
    "bike paths",
    "bike trail",
    "bike trails",
    "bike lane",
    "bike lanes",
    "bicycle path",
    "bicycle paths",
    "cycleway",
    "cycle way",
    "cycle path",
    "cycle paths",
    "cycle lane",
    "cycle lanes",
    "rail trail",
    "rail-trail",
    "rails to trails",
    "greenway",
    "greenways",
    "multi-use path",
    "multiuse path",
    "mup ",
    " mup",
    "protected bike",
    "protected lane",
    "separated bike",
    "separated lane",
    "off-road bike",
    "paved trail",
    "paved trails",
    "paved path",
    "paved paths",
)

# Distinct from "gravel ride" — when the rider asks for gravel *paths/trails*
# we lean even harder on unpaved + trail signals and try to thread through
# multiple gravel anchors instead of just one.
GRAVEL_PATH_KEYWORDS: tuple[str, ...] = (
    "gravel path",
    "gravel paths",
    "gravel trail",
    "gravel trails",
    "gravel track",
    "gravel tracks",
    "dirt path",
    "dirt paths",
    "dirt trail",
    "dirt trails",
    "fire road",
    "fire roads",
    "double track",
    "doubletrack",
    "single track",
    "singletrack",
    "jeep trail",
    "jeep road",
)


def text_matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Whole-phrase substring match. Keywords with leading/trailing spaces
    enforce word boundaries (e.g. `" mup"` doesn't match `mountain`).
    """
    return any(keyword in text for keyword in keywords)


def normalize_origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def build_allowed_frontend_origins() -> list[str]:
    configured_origin = normalize_origin(settings.frontend_url)
    if not configured_origin:
        return []

    allowed = {configured_origin}
    parsed = urllib.parse.urlsplit(configured_origin)
    port = parsed.port

    if port:
        for host in LOCAL_FRONTEND_HOSTS:
            allowed.add(f"{parsed.scheme}://{host}:{port}")

    return sorted(allowed)


def sanitize_return_to(return_to: str | None) -> str:
    fallback = settings.frontend_url.rstrip("/") or settings.frontend_url
    if not return_to:
        return fallback

    parsed = urllib.parse.urlsplit(return_to)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return fallback

    configured_origin = normalize_origin(settings.frontend_url)
    return_origin = normalize_origin(return_to)
    if return_origin == configured_origin or (parsed.hostname or "") in LOCAL_FRONTEND_HOSTS:
        path = parsed.path or "/"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))

    return fallback


def with_query_params(url: str, **params: str | int) -> str:
    parsed = urllib.parse.urlsplit(url)
    query_params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query_params.update({key: str(value) for key, value in params.items()})
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urllib.parse.urlencode(query_params), parsed.fragment)
    )


def load_activity_payload(activity: Activity) -> dict:
    if not activity.raw_json:
        return {}

    try:
        payload = json.loads(activity.raw_json)
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def get_activity_start_coordinates(activity: Activity) -> tuple[float | None, float | None]:
    payload = load_activity_payload(activity)
    coords = payload.get("start_latlng")
    if not isinstance(coords, list) or len(coords) != 2:
        return None, None

    try:
        lat = float(coords[0])
        lng = float(coords[1])
    except (TypeError, ValueError):
        return None, None

    return lat, lng


def serialize_activity(activity: Activity) -> ActivityOut:
    payload = load_activity_payload(activity)
    start_lat, start_lng = get_activity_start_coordinates(activity)
    return ActivityOut(
        id=activity.id,
        strava_activity_id=activity.strava_activity_id,
        name=activity.name,
        sport_type=activity.sport_type,
        distance_m=activity.distance_m,
        moving_time_s=activity.moving_time_s,
        elapsed_time_s=activity.elapsed_time_s,
        total_elevation_gain_m=activity.total_elevation_gain_m,
        average_speed_mps=activity.average_speed_mps,
        average_heartrate=activity.average_heartrate,
        average_watts=activity.average_watts,
        weighted_average_watts=activity.weighted_average_watts,
        kilojoules=activity.kilojoules,
        suffer_score=activity.suffer_score,
        trainer=activity.trainer,
        commute=activity.commute,
        start_date=activity.start_date,
        start_lat=start_lat,
        start_lng=start_lng,
        location_city=payload.get("location_city"),
        location_state=payload.get("location_state"),
        location_country=payload.get("location_country"),
    )


def get_activity_summary_polyline(activity: Activity) -> str | None:
    payload = load_activity_payload(activity)
    route_map = payload.get("map")
    if not isinstance(route_map, dict):
        return None

    polyline = route_map.get("summary_polyline") or route_map.get("polyline")
    if isinstance(polyline, str) and polyline:
        return polyline
    return None


def build_location_label(payload: dict) -> str | None:
    parts = []
    for key in ("location_city", "location_state", "location_country"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return ", ".join(parts) or None


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_miles = 3958.8
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_miles * c


def average_or_none(values: list[float | int | None]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def weighted_average(weighted_values: list[tuple[float | None, float]]) -> float | None:
    valid = [(float(value), weight) for value, weight in weighted_values if value is not None and weight > 0]
    if not valid:
        return None
    total_weight = sum(weight for _, weight in valid)
    return sum(value * weight for value, weight in valid) / total_weight


def resolve_target_value(min_value: float | None, max_value: float | None, learned_value: float | None) -> float | None:
    if min_value is not None and max_value is not None:
        return (min_value + max_value) / 2

    if learned_value is not None:
        if min_value is not None:
            learned_value = max(learned_value, min_value)
        if max_value is not None:
            learned_value = min(learned_value, max_value)
        return learned_value

    return min_value if min_value is not None else max_value


def is_popularity_request_text(ride_brief: str) -> bool:
    text = ride_brief.strip().lower()
    if any(keyword in text for keyword in POPULARITY_KEYWORDS):
        return True

    if "popular" not in text:
        return False

    return any(
        keyword in text
        for keyword in ("cyclist", "cycling", "rider", "riders", "segment", "segments", "local", "locals", "strava")
    )


def parse_ride_brief(ride_brief: str) -> dict[str, str | float | int | None]:
    text = ride_brief.strip().lower()
    prefer_popular_routes = is_popularity_request_text(ride_brief)
    prefer_bike_paths = text_matches_any(text, BIKE_PATH_KEYWORDS)
    prefer_unpaved_paths = text_matches_any(text, GRAVEL_PATH_KEYWORDS)

    matched_styles = [
        style for style, keywords in STYLE_KEYWORDS.items() if any(keyword in text for keyword in keywords)
    ]
    desired_style = matched_styles[0] if matched_styles else None
    destination_category = next(
        (
            category
            for category, keywords in DESTINATION_KEYWORDS.items()
            if any(keyword in text for keyword in keywords)
        ),
        None,
    )
    if desired_style is None and destination_category in {"taco_shop", "coffee_shop", "bakery"}:
        desired_style = "casual"
    if desired_style is None and destination_category in {"lake", "river", "park"}:
        desired_style = "adventure"

    intensity_label = None
    inferred_target_effort = None
    for label, keywords, effort in INTENSITY_PROFILES:
        if any(keyword in text for keyword in keywords):
            intensity_label = label
            inferred_target_effort = effort
            break

    range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|and)\s*(\d+(?:\.\d+)?)\s*(?:miles?|mi)\b", text)
    min_distance_miles = None
    max_distance_miles = None
    explicit_distance_requested = False
    if range_match:
        explicit_distance_requested = True
        first = float(range_match.group(1))
        second = float(range_match.group(2))
        min_distance_miles = min(first, second)
        max_distance_miles = max(first, second)
    else:
        around_match = re.search(r"(?:around|about|roughly)\s*(\d+(?:\.\d+)?)\s*(?:miles?|mi)\b", text)
        under_match = re.search(r"(?:under|below|less than|up to|max(?:imum)?(?: of)?)\s*(\d+(?:\.\d+)?)\s*(?:miles?|mi)\b", text)
        over_match = re.search(r"(?:over|above|at least|min(?:imum)?(?: of)?)\s*(\d+(?:\.\d+)?)\s*(?:miles?|mi)\b", text)
        exact_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:miles?|mi)\b", text)

        if around_match:
            explicit_distance_requested = True
            center = float(around_match.group(1))
            min_distance_miles = max(1.0, center * 0.85)
            max_distance_miles = center * 1.15
        elif under_match:
            explicit_distance_requested = True
            max_distance_miles = float(under_match.group(1))
        elif over_match:
            explicit_distance_requested = True
            min_distance_miles = float(over_match.group(1))
        elif exact_match:
            explicit_distance_requested = True
            center = float(exact_match.group(1))
            min_distance_miles = max(1.0, center - 2)
            max_distance_miles = center + 2

    min_elevation_ft = None
    max_elevation_ft = None
    if any(keyword in text for keyword in ("flat", "low climbing")):
        max_elevation_ft = 1200.0
    if "rolling" in text:
        min_elevation_ft = 500.0
        max_elevation_ft = 2500.0 if max_elevation_ft is None else max_elevation_ft
    if any(keyword in text for keyword in ("climb", "climbing", "hilly")):
        min_elevation_ft = max(min_elevation_ft or 0, 1500.0)
    if any(keyword in text for keyword in ("mountain", "mountainous", "big climb")):
        min_elevation_ft = max(min_elevation_ft or 0, 2500.0)

    sport_type = None
    if "gravel" in text:
        sport_type = "GravelRide"
    elif "mountain bike" in text or "mtb" in text:
        sport_type = "MountainBikeRide"
    elif "road" in text:
        sport_type = "Ride"

    if desired_style == "brewery" and not explicit_distance_requested:
        max_distance_miles = min(max_distance_miles, 20.0) if max_distance_miles is not None else 20.0
        max_elevation_ft = min(max_elevation_ft, 1200.0) if max_elevation_ft is not None else 1200.0
    elif destination_category in {"taco_shop", "coffee_shop", "bakery"} and not explicit_distance_requested:
        max_distance_miles = min(max_distance_miles, 25.0) if max_distance_miles is not None else 25.0
        max_elevation_ft = min(max_elevation_ft, 1600.0) if max_elevation_ft is not None else 1600.0
    elif desired_style in {"social", "casual", "recovery"} and not explicit_distance_requested:
        max_distance_miles = min(max_distance_miles, 30.0) if max_distance_miles is not None else 30.0
        max_elevation_ft = min(max_elevation_ft, 1800.0) if max_elevation_ft is not None else 1800.0
    elif intensity_label in {"hard", "training"}:
        min_distance_miles = max(min_distance_miles or 0, 15.0)

    if (
        min_distance_miles is not None
        and max_distance_miles is not None
        and min_distance_miles > max_distance_miles
    ):
        max_distance_miles = min_distance_miles

    return {
        "desired_style": desired_style,
        "intensity_label": intensity_label,
        "target_effort": inferred_target_effort,
        "min_distance_miles": min_distance_miles,
        "max_distance_miles": max_distance_miles,
        "min_elevation_ft": min_elevation_ft,
        "max_elevation_ft": max_elevation_ft,
        "sport_type": sport_type,
        "destination_category": destination_category,
        "explicit_distance_requested": 1 if explicit_distance_requested else 0,
        "prefer_popular_routes": 1 if prefer_popular_routes else 0,
        "prefer_bike_paths": 1 if prefer_bike_paths else 0,
        "prefer_unpaved_paths": 1 if prefer_unpaved_paths else 0,
    }


def build_feedback_stats(feedback_items: list[RideFeedback]) -> dict[int, dict[str, float | str | None]]:
    feedback_by_activity: dict[int, list[RideFeedback]] = defaultdict(list)
    for item in feedback_items:
        feedback_by_activity[item.activity_id].append(item)

    stats: dict[int, dict[str, float | str | None]] = {}
    for activity_id, items in feedback_by_activity.items():
        stats[activity_id] = {
            "avg_effort": average_or_none([item.perceived_effort for item in items]),
            "avg_enjoyment": average_or_none([item.enjoyment for item in items]),
            "match_rate": average_or_none([1.0 if item.matched_intent else 0.0 for item in items]) or 0.0,
            "style_text": " ".join(item.ride_style.strip().lower() for item in items if item.ride_style),
            "note_text": " ".join(item.notes.strip().lower() for item in items if item.notes),
        }

    return stats


def infer_start_from_history(activities: list[Activity]) -> tuple[float, float] | None:
    buckets: dict[tuple[int, int], dict[str, float]] = {}
    for activity in activities:
        start_lat, start_lng = get_activity_start_coordinates(activity)
        if start_lat is None or start_lng is None:
            continue

        bucket = (round(start_lat * 100), round(start_lng * 100))
        if bucket not in buckets:
            buckets[bucket] = {"count": 0.0, "lat_total": 0.0, "lng_total": 0.0}
        buckets[bucket]["count"] += 1.0
        buckets[bucket]["lat_total"] += start_lat
        buckets[bucket]["lng_total"] += start_lng

    if not buckets:
        return None

    _, best = max(buckets.items(), key=lambda item: item[1]["count"])
    count = best["count"] or 1.0
    return best["lat_total"] / count, best["lng_total"] / count


def clamp_number(value: float, minimum: float | None, maximum: float | None) -> float:
    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def is_gravel_request(
    desired_style: str | None,
    requested_sport_type: str | None,
    ride_brief: str,
) -> bool:
    normalized_sport = (requested_sport_type or "").lower()
    if desired_style in {"gravel", "adventure"}:
        return True
    if normalized_sport in {"gravelride", "mountainbikeride", "mtb"}:
        return True
    text = ride_brief.lower()
    return any(keyword in text for keyword in ("gravel", "unpaved", "dirt", "fire road", "double track", "singletrack"))


def destination_label(category: str | None) -> str | None:
    if not category:
        return None
    value = DESTINATION_METADATA.get(category, {}).get("label")
    return str(value) if value else category.replace("_", " ")


def destination_threshold_miles(category: str | None) -> float:
    if not category:
        return 0.5
    value = DESTINATION_METADATA.get(category, {}).get("threshold_miles")
    return float(value) if value is not None else 0.5


def build_destination_search_radius_miles(
    *,
    category: str | None,
    target_distance_miles: float | None,
    min_distance_miles: float | None,
    max_distance_miles: float | None,
) -> float:
    if max_distance_miles is not None:
        base_distance = max_distance_miles
    elif target_distance_miles is not None:
        base_distance = target_distance_miles
    elif min_distance_miles is not None:
        base_distance = min_distance_miles + 6
    else:
        base_distance = 18.0

    multiplier_value = DESTINATION_METADATA.get(category or "", {}).get("radius_multiplier")
    multiplier = float(multiplier_value) if multiplier_value is not None else 0.6
    return clamp_number(base_distance * multiplier, 2.5, 28.0)


def sample_route_points(points: list[tuple[float, float]], max_points: int = 180) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points

    step = max(1, len(points) // max_points)
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def route_destination_match(
    route_points: list[tuple[float, float]],
    places: list[PlaceCandidate],
    *,
    sample: bool = True,
) -> tuple[PlaceCandidate | None, float | None]:
    if not route_points or not places:
        return None, None

    sampled_points = sample_route_points(route_points) if sample else route_points
    best_place = None
    best_distance = None
    for place in places:
        distance = min(
            haversine_miles(point_lat, point_lng, place.lat, place.lng)
            for point_lat, point_lng in sampled_points
        )
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_place = place
    return best_place, best_distance


def coerce_float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_int(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_latlng(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None

    lat = coerce_float(value[0])
    lng = coerce_float(value[1])
    if lat is None or lng is None:
        return None
    return lat, lng


def build_route_bounds(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not points:
        return None

    latitudes = [point[0] for point in points]
    longitudes = [point[1] for point in points]
    mid_lat = sum(latitudes) / len(latitudes)

    lat_span = max(latitudes) - min(latitudes)
    lng_span = max(longitudes) - min(longitudes)
    lng_scale = max(0.25, math.cos(math.radians(mid_lat)))

    lat_padding = clamp_number(max(0.004, lat_span * 0.08), 0.004, 0.02)
    lng_padding = clamp_number(max(0.004 / lng_scale, lng_span * 0.08), 0.004 / lng_scale, 0.03 / lng_scale)

    return (
        min(latitudes) - lat_padding,
        min(longitudes) - lng_padding,
        max(latitudes) + lat_padding,
        max(longitudes) + lng_padding,
    )


def segment_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    encoded_points = payload.get("points")
    if isinstance(encoded_points, str) and encoded_points:
        try:
            points = decode_polyline(encoded_points)
        except Exception:
            points = []
        if points:
            return points

    start = parse_latlng(payload.get("start_latlng"))
    end = parse_latlng(payload.get("end_latlng"))
    if start and end:
        return [start, end]
    if start:
        return [start]
    if end:
        return [end]
    return []


def min_distance_to_points(point: tuple[float, float], points: list[tuple[float, float]]) -> float:
    return min(haversine_miles(point[0], point[1], candidate[0], candidate[1]) for candidate in points)


def segment_route_overlap_percent(
    route_points: list[tuple[float, float]],
    segment_points: list[tuple[float, float]],
) -> float | None:
    if len(route_points) < 2 or len(segment_points) < 2:
        return None

    sampled_route = sample_route_points(route_points, max_points=220)
    sampled_segment = sample_route_points(segment_points, max_points=50)
    if not sampled_route or not sampled_segment:
        return None

    distances = [min_distance_to_points(point, sampled_route) for point in sampled_segment]
    if not distances:
        return None

    very_close_ratio = sum(1 for value in distances if value <= 0.05) / len(distances)
    close_ratio = sum(1 for value in distances if value <= 0.1) / len(distances)
    near_ratio = sum(1 for value in distances if value <= 0.18) / len(distances)
    average_distance = sum(distances) / len(distances)

    start_distance = min_distance_to_points(sampled_segment[0], sampled_route)
    end_distance = min_distance_to_points(sampled_segment[-1], sampled_route)
    overlap = ((very_close_ratio * 0.5) + (close_ratio * 0.3) + (near_ratio * 0.2)) * 100

    if overlap < 26 and average_distance > 0.12 and max(start_distance, end_distance) > 0.2:
        return None
    if max(start_distance, end_distance) <= 0.08:
        overlap = max(overlap, 58.0)

    return round(clamp_number(overlap, 0.0, 100.0), 0)


def compute_segment_popularity_score(
    athlete_count: int | None,
    effort_count: int | None,
    star_count: int | None,
) -> float | None:
    if athlete_count is None and effort_count is None and star_count is None:
        return None

    athlete_component = clamp_number(math.log10((athlete_count or 0) + 1) / 4.5, 0.0, 1.0)
    effort_component = clamp_number(math.log10((effort_count or 0) + 1) / 5.4, 0.0, 1.0)
    star_component = clamp_number(math.log10((star_count or 0) + 1) / 3.6, 0.0, 1.0)
    score = (athlete_component * 0.46) + (effort_component * 0.39) + (star_component * 0.15)
    return round(score * 100, 0)


def extract_segment_current_time(
    segment_payload: dict,
) -> tuple[int | None, str | None, str | None, int | None]:
    athlete_effort_count = None
    stat_sources = [
        ("Current Strava PR", segment_payload.get("athlete_segment_stats")),
        ("Current Strava PR", segment_payload.get("athlete_pr_effort")),
    ]

    for source_label, source_payload in stat_sources:
        if not isinstance(source_payload, dict):
            continue

        if athlete_effort_count is None:
            athlete_effort_count = coerce_int(source_payload.get("effort_count"))

        current_time = coerce_int(source_payload.get("pr_elapsed_time"))
        if current_time is None:
            current_time = coerce_int(source_payload.get("elapsed_time"))
            if current_time is not None:
                source_label = "Most recent Strava effort"

        if current_time is None:
            continue

        current_time_date = source_payload.get("pr_date") or source_payload.get("start_date_local") or source_payload.get(
            "start_date"
        )
        return current_time, source_label, str(current_time_date) if current_time_date else None, athlete_effort_count

    return None, None, None, athlete_effort_count


async def summarize_route_segments(
    route: GeneratedRoute,
    *,
    access_token: str,
    overlap_threshold: float = 32.0,
) -> tuple[list[IncludedSegmentOut], float | None]:
    bounds = build_route_bounds(route.points)
    if bounds is None:
        return [], None

    explored_segments = await client.explore_segments(access_token, bounds=bounds, activity_type="riding")
    if not explored_segments:
        return [], None

    matched_segments: list[dict[str, object]] = []
    for explored_segment in explored_segments:
        if not isinstance(explored_segment, dict):
            continue

        segment_points = segment_points_from_payload(explored_segment)
        overlap_percent = segment_route_overlap_percent(route.points, segment_points)
        if overlap_percent is None or overlap_percent < overlap_threshold:
            continue

        matched_segments.append(
            {
                "segment": explored_segment,
                "overlap_percent": overlap_percent,
            }
        )

    if not matched_segments:
        return [], None

    matched_segments.sort(
        key=lambda item: (
            float(item["overlap_percent"]),
            coerce_float((item["segment"] or {}).get("distance")) or 0.0,
        ),
        reverse=True,
    )
    top_matches = matched_segments[:4]
    detail_results = await asyncio.gather(
        *(client.get_segment(access_token, int((item["segment"] or {})["id"])) for item in top_matches),
        return_exceptions=True,
    )

    included_segments: list[IncludedSegmentOut] = []
    popularity_inputs: list[tuple[float | None, float]] = []
    for item, detail_result in zip(top_matches, detail_results):
        explorer_segment = item["segment"] or {}
        segment_payload = detail_result if isinstance(detail_result, dict) else explorer_segment
        athlete_count = coerce_int(segment_payload.get("athlete_count"))
        effort_count = coerce_int(segment_payload.get("effort_count"))
        star_count = coerce_int(segment_payload.get("star_count"))
        popularity_score = compute_segment_popularity_score(athlete_count, effort_count, star_count)
        current_time_seconds, current_time_source, current_time_date, athlete_effort_count = extract_segment_current_time(
            segment_payload
        )

        included_segments.append(
            IncludedSegmentOut(
                id=int(explorer_segment["id"]),
                name=str(segment_payload.get("name") or explorer_segment.get("name") or "Unnamed segment"),
                distance_miles=round((coerce_float(segment_payload.get("distance")) or coerce_float(explorer_segment.get("distance")) or 0.0) * 0.000621371, 2)
                if (
                    coerce_float(segment_payload.get("distance")) is not None
                    or coerce_float(explorer_segment.get("distance")) is not None
                )
                else None,
                avg_grade=coerce_float(segment_payload.get("avg_grade"))
                if coerce_float(segment_payload.get("avg_grade")) is not None
                else coerce_float(explorer_segment.get("avg_grade")),
                climb_category=coerce_int(segment_payload.get("climb_category"))
                if coerce_int(segment_payload.get("climb_category")) is not None
                else coerce_int(explorer_segment.get("climb_category")),
                athlete_count=athlete_count,
                effort_count=effort_count,
                star_count=star_count,
                route_overlap_percent=float(item["overlap_percent"]),
                popularity_score=popularity_score,
                current_time_seconds=current_time_seconds,
                current_time_source=current_time_source,
                current_time_date=current_time_date,
                athlete_effort_count=athlete_effort_count,
            )
        )

        if popularity_score is not None:
            popularity_inputs.append((popularity_score, float(item["overlap_percent"])))

    route_popularity_score = weighted_average(popularity_inputs)
    return included_segments, round(route_popularity_score, 0) if route_popularity_score is not None else None


def rank_destination_places(
    places: list[PlaceCandidate],
    *,
    target_distance_miles: float | None,
    category: str | None,
) -> list[PlaceCandidate]:
    if not places:
        return []

    if target_distance_miles is None:
        return sorted(places, key=lambda place: place.distance_miles)

    if category in {"lake", "river", "park"}:
        ideal_outbound = clamp_number(target_distance_miles * 0.32, 2.0, 18.0)
    else:
        ideal_outbound = clamp_number(target_distance_miles * 0.18, 1.5, 14.0)

    return sorted(
        places,
        key=lambda place: (abs(place.distance_miles - ideal_outbound), -place.distance_miles),
    )


def build_route_cells(points: list[tuple[float, float]], trim: bool = False) -> set[str]:
    if not points:
        return set()

    start_index = 0
    end_index = len(points)
    if trim and len(points) > 12:
        trim_size = max(1, int(len(points) * 0.08))
        start_index = trim_size
        end_index = len(points) - trim_size

    cells = set()
    for lat, lng in points[start_index:end_index]:
        cells.add(f"{round(lat / 0.002)}:{round(lng / 0.002)}")
    return cells


def build_historical_footprint(activities: list[Activity]) -> set[str]:
    ridden_cells: set[str] = set()
    for activity in activities:
        if activity.trainer:
            continue
        polyline = get_activity_summary_polyline(activity)
        if not polyline:
            continue
        ridden_cells.update(build_route_cells(decode_polyline(polyline), trim=True))
    return ridden_cells


def resolve_location_label(
    activities: list[Activity],
    selected_start: tuple[float, float] | None,
    explicit_label: str | None,
) -> str | None:
    if explicit_label:
        return explicit_label
    if not selected_start:
        return None

    nearest_label = None
    nearest_distance = None
    for activity in activities:
        payload = load_activity_payload(activity)
        label = build_location_label(payload)
        if not label:
            continue
        start_lat, start_lng = get_activity_start_coordinates(activity)
        if start_lat is None or start_lng is None:
            continue
        distance = haversine_miles(selected_start[0], selected_start[1], start_lat, start_lng)
        if nearest_distance is None or distance < nearest_distance:
            nearest_distance = distance
            nearest_label = label
    return nearest_label


def normalize_export_target(target: str | None) -> str:
    normalized = (target or "other").strip().lower()
    return normalized if normalized in ROUTE_EXPORT_DEFAULTS else "other"


def slugify_filename(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    return slug or "spin-scout-route"


def build_gpx_document(route_name: str, points: list[tuple[float, float]]) -> str:
    escaped_name = escape(route_name)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Spin Scout" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{escaped_name}</name>",
        f"    <time>{generated_at}</time>",
        "  </metadata>",
        "  <trk>",
        f"    <name>{escaped_name}</name>",
        "    <trkseg>",
    ]

    for lat, lng in points:
        lines.append(f'      <trkpt lat="{lat:.6f}" lon="{lng:.6f}"></trkpt>')

    lines.extend(
        [
            "    </trkseg>",
            "  </trk>",
            "</gpx>",
        ]
    )
    return "\n".join(lines)


def build_tcx_document(route_name: str, points: list[tuple[float, float]]) -> str:
    escaped_name = escape(route_name)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">',
        "  <Courses>",
        "    <Course>",
        f"      <Name>{escaped_name}</Name>",
        "      <Track>",
    ]

    cumulative_distance_m = 0.0
    previous_point: tuple[float, float] | None = None
    for lat, lng in points:
        if previous_point is not None:
            cumulative_distance_m += haversine_miles(previous_point[0], previous_point[1], lat, lng) * 1609.34
        previous_point = (lat, lng)

        lines.extend(
            [
                "        <Trackpoint>",
                "          <Position>",
                f"            <LatitudeDegrees>{lat:.6f}</LatitudeDegrees>",
                f"            <LongitudeDegrees>{lng:.6f}</LongitudeDegrees>",
                "          </Position>",
                f"          <DistanceMeters>{cumulative_distance_m:.1f}</DistanceMeters>",
                "        </Trackpoint>",
            ]
        )

    lines.extend(
        [
            "      </Track>",
            f"      <Notes>Generated by Spin Scout on {generated_at}</Notes>",
            "    </Course>",
            "  </Courses>",
            "</TrainingCenterDatabase>",
        ]
    )
    return "\n".join(lines)


app.add_middleware(
    CORSMiddleware,
    allow_origins=build_allowed_frontend_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    migrate_large_strava_ids()


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "routing_provider": settings.routing_provider,
        "routing_enabled": bool(settings.graphhopper_api_key),
    }


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Spin Scout API"}


@app.get("/api/auth/strava/login")
def strava_login(return_to: str | None = Query(default=None)) -> RedirectResponse:
    if not settings.strava_client_id or not settings.strava_redirect_uri:
        raise HTTPException(status_code=500, detail="Strava OAuth is not configured")

    safe_return_to = sanitize_return_to(return_to)
    params = urllib.parse.urlencode(
        {
            "client_id": settings.strava_client_id,
            "response_type": "code",
            "redirect_uri": settings.strava_redirect_uri,
            "approval_prompt": "auto",
            "scope": settings.strava_scopes,
            "state": safe_return_to,
        }
    )
    return RedirectResponse(url=f"https://www.strava.com/oauth/authorize?{params}")


@app.get("/api/auth/strava/callback")
async def strava_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    redirect_base = sanitize_return_to(state)

    if error:
        return RedirectResponse(url=with_query_params(redirect_base, connected=0, error=error))
    if not code:
        return RedirectResponse(url=with_query_params(redirect_base, connected=0, error="missing_code"))

    token_data = await client.exchange_code(
        client_id=settings.strava_client_id,
        client_secret=settings.strava_client_secret,
        code=code,
    )
    user = upsert_user_from_token_data(db, token_data)
    redirect_url = with_query_params(redirect_base, connected=1, user_id=user.id)
    return RedirectResponse(url=redirect_url)


@app.get("/api/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return db.query(User).order_by(User.firstname.asc().nulls_last(), User.id.asc()).all()


@app.get("/api/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.post("/api/users/{user_id}/sync")
async def sync_activities(user_id: int, per_page: int = Query(default=50, le=100), db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    access_token = await ensure_valid_token(
        user,
        client_id=settings.strava_client_id,
        client_secret=settings.strava_client_secret,
        db=db,
    )
    activities = await client.get_activities(access_token, per_page=per_page)
    count = upsert_activities(db, user, activities)
    return {"synced": count}


@app.get("/api/users/{user_id}/activities", response_model=list[ActivityOut])
def list_activities(user_id: int, db: Session = Depends(get_db)) -> list[ActivityOut]:
    activities = (
        db.query(Activity)
        .filter(Activity.user_id == user_id)
        .order_by(desc(Activity.start_date), desc(Activity.id))
        .all()
    )
    return [serialize_activity(activity) for activity in activities]


@app.get("/api/activities/{activity_id}", response_model=ActivityOut)
def get_activity(activity_id: int, db: Session = Depends(get_db)) -> ActivityOut:
    activity = db.get(Activity, activity_id)
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    return serialize_activity(activity)


@app.post("/api/users/{user_id}/feedback", response_model=RideFeedbackOut)
def create_feedback(user_id: int, payload: RideFeedbackCreate, db: Session = Depends(get_db)) -> RideFeedback:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    activity = db.get(Activity, payload.activity_id)
    if not activity or activity.user_id != user.id:
        raise HTTPException(status_code=404, detail="Activity not found for user")

    feedback = RideFeedback(user_id=user.id, **payload.model_dump())
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback


@app.get("/api/users/{user_id}/feedback", response_model=list[RideFeedbackOut])
def list_feedback(user_id: int, db: Session = Depends(get_db)) -> list[RideFeedback]:
    return (
        db.query(RideFeedback)
        .filter(RideFeedback.user_id == user_id)
        .order_by(desc(RideFeedback.created_at), desc(RideFeedback.id))
        .all()
    )


@app.post("/api/routes/export/{file_format}")
def export_generated_route(file_format: str, payload: RouteExportRequest) -> Response:
    format_value = file_format.strip().lower()
    if format_value not in {"gpx", "tcx"}:
        raise HTTPException(status_code=404, detail="Unsupported export format")

    points = decode_polyline(payload.route_polyline)
    if len(points) < 2:
        raise HTTPException(status_code=400, detail="Route geometry is missing or invalid.")

    route_name = payload.route_name.strip() or "Spin Scout Route"
    export_target = normalize_export_target(payload.target)
    export_label = ROUTE_EXPORT_DEFAULTS[export_target]["label"]
    filename = f"{slugify_filename(route_name)}-{slugify_filename(export_label)}.{format_value}"

    if format_value == "gpx":
        content = build_gpx_document(route_name, points)
        media_type = "application/gpx+xml"
    else:
        content = build_tcx_document(route_name, points)
        media_type = "application/vnd.garmin.tcx+xml"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/users/{user_id}/ride-plan", response_model=RidePlanOut)
async def generate_ride_plan(
    user_id: int,
    payload: RidePlanRequest,
    db: Session = Depends(get_db),
) -> RidePlanOut:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if settings.routing_provider.lower() != "graphhopper":
        raise HTTPException(status_code=503, detail="The configured routing provider is not supported.")
    if not settings.graphhopper_api_key:
        raise HTTPException(
            status_code=503,
            detail="Map-based route generation is not configured yet. Set GRAPHHOPPER_API_KEY on the API service and restart it.",
        )

    activities = (
        db.query(Activity)
        .filter(Activity.user_id == user_id)
        .order_by(desc(Activity.start_date), desc(Activity.id))
        .all()
    )
    if not activities:
        raise HTTPException(status_code=400, detail="Sync some rides before generating a ride plan")

    feedback_items = (
        db.query(RideFeedback)
        .filter(RideFeedback.user_id == user_id)
        .order_by(desc(RideFeedback.created_at), desc(RideFeedback.id))
        .all()
    )
    feedback_stats = build_feedback_stats(feedback_items)
    parsed_brief = parse_ride_brief(payload.ride_brief)

    desired_style = payload.desired_style or (
        str(parsed_brief["desired_style"]) if parsed_brief["desired_style"] else None
    )
    intensity_label = str(parsed_brief["intensity_label"]) if parsed_brief["intensity_label"] else None
    target_effort = payload.target_effort or (
        int(parsed_brief["target_effort"]) if parsed_brief["target_effort"] is not None else None
    )
    min_distance_miles = payload.min_distance_miles or (
        float(parsed_brief["min_distance_miles"]) if parsed_brief["min_distance_miles"] is not None else None
    )
    max_distance_miles = payload.max_distance_miles or (
        float(parsed_brief["max_distance_miles"]) if parsed_brief["max_distance_miles"] is not None else None
    )
    min_elevation_ft = payload.min_elevation_ft or (
        float(parsed_brief["min_elevation_ft"]) if parsed_brief["min_elevation_ft"] is not None else None
    )
    max_elevation_ft = payload.max_elevation_ft or (
        float(parsed_brief["max_elevation_ft"]) if parsed_brief["max_elevation_ft"] is not None else None
    )
    requested_sport_type = payload.sport_type or (
        str(parsed_brief["sport_type"]) if parsed_brief["sport_type"] else None
    )
    destination_category = (
        str(parsed_brief["destination_category"]) if parsed_brief["destination_category"] else None
    )
    explicit_distance_requested = bool(parsed_brief.get("explicit_distance_requested"))
    gravel_requested = is_gravel_request(desired_style, requested_sport_type, payload.ride_brief)
    prefer_popular_routes = bool(parsed_brief.get("prefer_popular_routes"))
    prefer_bike_paths = bool(parsed_brief.get("prefer_bike_paths"))
    prefer_unpaved_paths = bool(parsed_brief.get("prefer_unpaved_paths"))

    inferred_start = False
    selected_start = None
    if payload.start_lat is not None and payload.start_lng is not None:
        selected_start = (payload.start_lat, payload.start_lng)
    else:
        selected_start = infer_start_from_history(activities)
        inferred_start = selected_start is not None

    if selected_start is None:
        raise HTTPException(
            status_code=400,
            detail="Choose a start point on the map first, or sync rides that include GPS start coordinates.",
        )

    learned_examples: list[tuple[Activity, float]] = []
    for activity in activities:
        stats = feedback_stats.get(activity.id)
        if not stats:
            continue

        weight = 0.0
        avg_enjoyment = stats.get("avg_enjoyment")
        avg_effort = stats.get("avg_effort")
        match_rate = float(stats.get("match_rate") or 0.0)
        style_text = f"{stats.get('style_text') or ''} {stats.get('note_text') or ''}"

        if avg_enjoyment is not None:
            weight += max(0.0, float(avg_enjoyment) - 5.0)
        weight += match_rate * 3.0
        if desired_style and desired_style in style_text:
            weight += 3.0
        if target_effort is not None and avg_effort is not None:
            weight += max(0.0, 3.0 - abs(float(avg_effort) - target_effort))

        if weight > 0:
            learned_examples.append((activity, weight))

    if not learned_examples:
        learned_examples = [(activity, 1.0) for activity in activities if not activity.trainer]
    if not learned_examples:
        learned_examples = [(activity, 1.0) for activity in activities]

    learned_distance_miles = weighted_average(
        [
            (((activity.distance_m or 0) * 0.000621371) if activity.distance_m is not None else None, weight)
            for activity, weight in learned_examples
        ]
    )
    learned_elevation_ft = weighted_average(
        [
            (((activity.total_elevation_gain_m or 0) * 3.28084) if activity.total_elevation_gain_m is not None else None, weight)
            for activity, weight in learned_examples
        ]
    )
    learned_duration_min = weighted_average(
        [((activity.moving_time_s / 60) if activity.moving_time_s is not None else None, weight) for activity, weight in learned_examples]
    )
    learned_avg_watts = weighted_average([(activity.average_watts, weight) for activity, weight in learned_examples])
    learned_avg_heartrate = weighted_average([(activity.average_heartrate, weight) for activity, weight in learned_examples])
    learned_speed_mph = weighted_average(
        [
            (
                ((activity.distance_m or 0) * 0.000621371) / (activity.moving_time_s / 3600)
                if activity.distance_m is not None and activity.moving_time_s not in (None, 0)
                else None,
                weight,
            )
            for activity, weight in learned_examples
        ]
    )

    target_distance_miles = resolve_target_value(min_distance_miles, max_distance_miles, learned_distance_miles)
    target_elevation_ft = resolve_target_value(min_elevation_ft, max_elevation_ft, learned_elevation_ft)
    target_duration_min = learned_duration_min
    if target_duration_min is None and target_distance_miles is not None and learned_speed_mph:
        target_duration_min = (target_distance_miles / learned_speed_mph) * 60

    destination_places: list[PlaceCandidate] = []
    if destination_category:
        search_radius_miles = build_destination_search_radius_miles(
            category=destination_category,
            target_distance_miles=target_distance_miles,
            min_distance_miles=min_distance_miles,
            max_distance_miles=max_distance_miles,
        )
        try:
            destination_places = await places_client.search_destinations(
                category=destination_category,
                lat=selected_start[0],
                lng=selected_start[1],
                radius_miles=search_radius_miles,
                limit=6,
            )
        except PlacesError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if not destination_places:
            label = destination_label(destination_category) or "destination"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"I couldn't find a nearby {label} within about "
                    f"{search_radius_miles:.0f} miles of the selected start."
                ),
            )
        destination_places = rank_destination_places(
            destination_places,
            target_distance_miles=target_distance_miles,
            category=destination_category,
        )

    gravel_segments: list[PlaceCandidate] = []
    if gravel_requested:
        gravel_radius = clamp_number((target_distance_miles or max_distance_miles or 18.0) * 0.45, 2.0, 12.0)
        try:
            # When the brief specifically calls out "gravel paths/trails" we
            # widen the segment search so we can stitch through 2-3 anchors.
            gravel_limit = 6 if prefer_unpaved_paths else 4
            gravel_segments = await places_client.search_gravel_segments(
                lat=selected_start[0],
                lng=selected_start[1],
                radius_miles=gravel_radius,
                limit=gravel_limit,
            )
        except PlacesError:
            gravel_segments = []

    bike_path_segments: list[PlaceCandidate] = []
    if prefer_bike_paths:
        bike_path_radius = clamp_number(
            (target_distance_miles or max_distance_miles or 18.0) * 0.4,
            2.0,
            10.0,
        )
        try:
            bike_path_segments = await places_client.search_bike_paths(
                lat=selected_start[0],
                lng=selected_start[1],
                radius_miles=bike_path_radius,
                limit=6,
            )
        except PlacesError:
            bike_path_segments = []

    # ----- Plan and dispatch the candidate routing calls (services/candidates) -----
    plan_inputs = CandidatePlanInputs(
        routing_client=routing_client,
        start=selected_start,
        desired_style=desired_style,
        intensity_label=intensity_label,
        sport_type=requested_sport_type,
        target_distance_miles=target_distance_miles,
        min_distance_miles=min_distance_miles,
        max_distance_miles=max_distance_miles,
        destination_places=destination_places,
        destination_category=destination_category,
        gravel_segments=gravel_segments,
        gravel_requested=gravel_requested,
        explicit_distance_requested=explicit_distance_requested,
        bike_path_segments=bike_path_segments,
        prefer_bike_paths=prefer_bike_paths,
        prefer_unpaved_paths=prefer_unpaved_paths,
    )
    candidate_requests = plan_candidate_requests(plan_inputs)
    generated_results = await asyncio.gather(
        *(request.factory() for request in candidate_requests),
        return_exceptions=True,
    )

    ridden_cells = build_historical_footprint(activities)
    ride_context = RideContext(
        desired_style=desired_style,
        intensity_label=intensity_label,
        sport_type=requested_sport_type,
        target_distance_miles=target_distance_miles,
        min_distance_miles=min_distance_miles,
        max_distance_miles=max_distance_miles,
        target_elevation_ft=target_elevation_ft,
        min_elevation_ft=min_elevation_ft,
        max_elevation_ft=max_elevation_ft,
        target_duration_min=target_duration_min,
        destination_category=destination_category,
        destination_places=destination_places,
        explicit_distance_requested=explicit_distance_requested,
        gravel_requested=gravel_requested,
        prefer_popular_routes=prefer_popular_routes,
        destination_threshold_miles=destination_threshold_miles(destination_category),
        prefer_bike_paths=prefer_bike_paths,
        prefer_unpaved_paths=prefer_unpaved_paths,
    )

    scored_candidates: list[dict] = []
    route_failures: list[str] = []
    planner_warnings: list[str] = []

    for request, result in zip(candidate_requests, generated_results):
        if isinstance(result, Exception):
            message = str(result)
            if message:
                route_failures.append(message)
            continue

        route: GeneratedRoute = result
        if not route.points:
            continue

        # Resolve destination match once per candidate, before scoring.
        if destination_category and destination_places:
            if request.anchor_place and request.mode.startswith("destination"):
                matched_destination = request.anchor_place
                destination_distance_miles = 0.0
            else:
                matched_destination, destination_distance_miles = route_destination_match(
                    route.points,
                    destination_places,
                    sample=True,
                )
        else:
            matched_destination, destination_distance_miles = None, None

        scored_candidates.append(
            score_candidate(
                route=route,
                route_mode=request.mode,
                anchor_place=request.anchor_place,
                context=ride_context,
                ridden_cells=ridden_cells,
                route_cells=build_route_cells(route.points, trim=True),
                matched_destination=matched_destination,
                destination_distance_miles=destination_distance_miles,
            )
        )

    if not scored_candidates:
        detail = route_failures[0] if route_failures else "No new routes could be generated from the selected start."
        raise HTTPException(status_code=502, detail=detail)

    # ----- Hard filters: destinations remain blocking, gravel falls back gracefully -----
    if destination_category:
        scored_candidates, destination_warning = filter_destination_candidates(
            scored_candidates,
            context=ride_context,
        )
        if not scored_candidates:
            label = destination_label(destination_category) or "destination"
            raise HTTPException(
                status_code=502,
                detail=f"I found nearby {label} options, but none of the generated routes could get close enough to one.",
            )
        if destination_warning == "destination_distance_unsatisfied":
            label = destination_label(destination_category) or "destination"
            raise HTTPException(
                status_code=502,
                detail=(
                    f"I found nearby {label} options, but none of the generated routes could satisfy both the stop "
                    "and your requested distance."
                ),
            )

    if gravel_requested:
        scored_candidates, gravel_warning = filter_gravel_candidates(
            scored_candidates,
            has_gravel_segments=bool(gravel_segments),
        )
        if gravel_warning:
            planner_warnings.append(gravel_warning)

    if prefer_bike_paths:
        scored_candidates, bike_path_warning = filter_bike_path_candidates(
            scored_candidates,
            has_bike_path_segments=bool(bike_path_segments),
        )
        if bike_path_warning:
            planner_warnings.append(bike_path_warning)

    scored_candidates.sort(key=lambda candidate: candidate["score"], reverse=True)

    # Footprint-similarity dedupe on top of bearing dedupe — kills near-identical loops.
    unique_candidates: list[dict] = []
    for candidate in scored_candidates:
        route_cells = candidate["route_cells"]
        duplicate = False
        for existing in unique_candidates:
            existing_cells = existing["route_cells"]
            if route_cells and existing_cells:
                similarity = len(route_cells & existing_cells) / max(1, min(len(route_cells), len(existing_cells)))
                if similarity >= 0.82:
                    duplicate = True
                    break
        if not duplicate:
            unique_candidates.append(candidate)
    if not unique_candidates:
        unique_candidates = scored_candidates

    # Bearing-based dedupe makes the surfaced 3 actually feel different on the map.
    unique_candidates = deduplicate_by_bearing(unique_candidates, min_separation_deg=35.0, keep_at_least=3)

    # ----- Strava segment matching for popularity (now graceful on empty data) -----
    segment_candidate_limit = min(len(unique_candidates), 4 if prefer_popular_routes else 1)
    if segment_candidate_limit:
        access_token = None
        try:
            access_token = await ensure_valid_token(
                user,
                client_id=settings.strava_client_id,
                client_secret=settings.strava_client_secret,
                db=db,
            )
        except Exception:
            access_token = None
            if prefer_popular_routes:
                planner_warnings.append(
                    "I couldn't load Strava segment data this time, so popularity scoring fell back to map signals only."
                )

        if access_token:
            overlap_threshold = (
                POPULARITY_OVERLAP_THRESHOLD if prefer_popular_routes else 32.0
            )
            segment_results = await asyncio.gather(
                *(
                    summarize_route_segments(
                        candidate["route"],
                        access_token=access_token,
                        overlap_threshold=overlap_threshold,
                    )
                    for candidate in unique_candidates[:segment_candidate_limit]
                ),
                return_exceptions=True,
            )

            popularity_ready = 0
            for candidate, segment_result in zip(unique_candidates[:segment_candidate_limit], segment_results):
                if isinstance(segment_result, Exception):
                    continue

                included_segments, popularity_score = segment_result
                candidate["included_segments"] = included_segments
                candidate["popularity_score"] = popularity_score

                if included_segments:
                    segment_names = ", ".join(segment.name for segment in included_segments[:2])
                    candidate["popularity_summary"] = f"It threads through Strava-tracked segments like {segment_names}."

                if popularity_score is not None:
                    popularity_ready += 1
                    if prefer_popular_routes:
                        candidate["score"] = round(float(candidate["score"]) + popularity_score * 0.38, 2)
                        if popularity_score >= 70:
                            candidate["reasons"].append("It leans onto especially well-ridden local Strava segments.")
                        elif popularity_score >= 50:
                            candidate["reasons"].append("It stays on locally popular Strava segments more than the other generated options.")

            if prefer_popular_routes:
                if popularity_ready == 0:
                    planner_warnings.append(
                        "Strava segment popularity data was sparse on these routes — showing them anyway. "
                        "A different start point near busier roads typically returns more popularity signal."
                    )
                unique_candidates.sort(key=lambda candidate: candidate["score"], reverse=True)

    best_candidate = unique_candidates[0]
    best_route: GeneratedRoute = best_candidate["route"]
    location_label = resolve_location_label(activities, selected_start, payload.start_label)

    def make_generated_option(candidate: dict, index: int) -> GeneratedRouteOptionOut:
        route: GeneratedRoute = candidate["route"]
        novelty = candidate["novelty_score"]
        overlap = candidate["overlap_percent"]
        matched_destination: PlaceCandidate | None = candidate["destination_place"]

        if matched_destination and destination_category:
            prefix = f"{destination_label(destination_category).title()}-bound"
        elif gravel_requested and (route.unpaved_percent or 0) >= 18:
            prefix = "Gravel-forward"
        elif route.trail_percent is not None and route.trail_percent >= 25:
            prefix = "Trail-forward"
        elif route.bike_network_percent is not None and route.bike_network_percent >= 40:
            prefix = "Bike-network"
        elif novelty is not None and novelty >= 70:
            prefix = "High-novelty"
        else:
            prefix = "Balanced"

        summary_parts = []
        if matched_destination:
            summary_parts.append(matched_destination.label)
        if route.distance_miles is not None:
            summary_parts.append(f"{route.distance_miles:.1f} mi")
        if route.elevation_ft is not None:
            summary_parts.append(f"{route.elevation_ft:.0f} ft")
        if route.duration_min is not None:
            summary_parts.append(f"{route.duration_min} min")
        if route.major_road_percent is not None:
            summary_parts.append(f"{route.major_road_percent:.0f}% major roads")
        if novelty is not None:
            summary_parts.append(f"{novelty:.0f}% novel")
        if candidate["popularity_score"] is not None:
            summary_parts.append(f"{candidate['popularity_score']:.0f}/100 popular")

        return GeneratedRouteOptionOut(
            name=f"{prefix} route {index + 1}",
            score=float(candidate["score"]),
            reason=" ".join(candidate["reasons"]) if candidate["reasons"] else "Generated from map data with good overall fit.",
            summary=" · ".join(summary_parts) if summary_parts else None,
            destination_label=matched_destination.label if matched_destination else None,
            destination_category=destination_category,
            destination_distance_miles=candidate["destination_distance_miles"],
            distance_miles=route.distance_miles,
            elevation_ft=route.elevation_ft,
            duration_min=route.duration_min,
            route_polyline=route.route_polyline,
            route_start_lat=selected_start[0],
            route_start_lng=selected_start[1],
            trail_percent=route.trail_percent,
            bike_network_percent=route.bike_network_percent,
            unpaved_percent=route.unpaved_percent,
            major_road_percent=route.major_road_percent,
            novelty_score=novelty,
            overlap_percent=overlap,
            popularity_score=candidate["popularity_score"],
            surface_summary=route.surface_summary,
            provider=route.provider,
        )

    explanation: list[str] = []
    # Surface any soft warnings from filtering (gravel fallback, missing popularity data, ...)
    # before the generic explanation lines so the user reads them first.
    explanation.extend(planner_warnings)
    if inferred_start:
        explanation.append("No start point was selected, so I inferred your most common historical ride start area.")
    else:
        explanation.append("The generated route starts exactly at the point you selected on the map.")
    explanation.append("Generated from OpenStreetMap trail and road data through GraphHopper instead of reusing one of your old rides.")
    if best_candidate["destination_place"] is not None:
        matched_destination = best_candidate["destination_place"]
        explanation.append(
            f"It was routed to pass near {matched_destination.label} for your requested {destination_label(destination_category)} stop."
        )
    if target_distance_miles is not None:
        explanation.append(f"Targeting about {target_distance_miles:.1f} miles based on your brief and your strongest historical signals.")
    if target_elevation_ft is not None:
        explanation.append(f"Targeting roughly {target_elevation_ft:.0f} feet of climbing for this ride.")
    if best_candidate["novelty_score"] is not None and best_candidate["overlap_percent"] is not None:
        explanation.append(
            f"About {best_candidate['novelty_score']:.0f}% of the route is new compared with your synced ride footprint, with only {best_candidate['overlap_percent']:.0f}% overlap."
        )
    if prefer_popular_routes and best_candidate["popularity_score"] is not None:
        explanation.append(
            f"It also favored cyclist-popular Strava segments, landing about {best_candidate['popularity_score']:.0f}/100 on the local segment-popularity score."
        )
    if best_candidate["included_segments"]:
        explanation.append("Matched Strava segments below include your current recorded segment times when Strava has them.")
    if gravel_requested and best_route.unpaved_percent is not None and best_route.major_road_percent is not None:
        explanation.append(
            f"The gravel scorer favored routes with more unpaved terrain ({best_route.unpaved_percent:.0f}%) and less major-road exposure ({best_route.major_road_percent:.0f}%)."
        )
    if best_route.surface_summary:
        explanation.append(best_route.surface_summary)
    if learned_avg_watts is not None and intensity_label in {"hard", "training", "endurance"}:
        explanation.append(f"Your historical rides suggest an average power target around {learned_avg_watts:.0f} watts.")
    if learned_avg_heartrate is not None:
        explanation.append(f"Your prior rides suggest an average heart rate around {learned_avg_heartrate:.0f} bpm.")

    start_phrase = f"from {location_label}" if location_label else "from your selected start"
    metric_parts = []
    if best_route.distance_miles is not None:
        metric_parts.append(f"{best_route.distance_miles:.1f} miles")
    if best_route.elevation_ft is not None:
        metric_parts.append(f"{best_route.elevation_ft:.0f} ft of climbing")
    if best_route.duration_min is not None:
        metric_parts.append(f"about {best_route.duration_min} minutes")

    route_noun = "route" if destination_category else "loop"
    summary = f"Generated a new {route_noun} {start_phrase} using map and trail data."
    if best_candidate["destination_place"] is not None:
        summary += f" It heads past {best_candidate['destination_place'].label}."
    if metric_parts:
        summary += " This route aims for " + ", ".join(metric_parts) + "."
    if best_candidate["novelty_score"] is not None:
        summary += f" It is about {best_candidate['novelty_score']:.0f}% new relative to your synced rides."
    if prefer_popular_routes and best_candidate["popularity_score"] is not None:
        summary += f" It leans onto well-ridden local Strava segments with a popularity score around {best_candidate['popularity_score']:.0f}/100."
    if gravel_requested and best_route.major_road_percent is not None:
        summary += f" Major-road exposure is held to about {best_route.major_road_percent:.0f}%."

    title_prefix = destination_label(destination_category) or intensity_label or desired_style or "custom"
    title = f"{title_prefix.title()} Ride Plan"
    if best_route.distance_miles is not None:
        title += f" · {best_route.distance_miles:.1f} mi"

    alternative_options = [make_generated_option(candidate, index) for index, candidate in enumerate(unique_candidates[:5])]
    best_option = alternative_options[0]

    return RidePlanOut(
        title=title,
        summary=summary,
        ride_brief=payload.ride_brief,
        desired_style=desired_style,
        intensity_label=intensity_label,
        destination_label=best_candidate["destination_place"].label if best_candidate["destination_place"] else None,
        destination_category=destination_category,
        destination_distance_miles=best_candidate["destination_distance_miles"],
        target_distance_miles=round(target_distance_miles, 2) if target_distance_miles is not None else None,
        target_elevation_ft=round(target_elevation_ft, 0) if target_elevation_ft is not None else None,
        target_duration_min=round(target_duration_min) if target_duration_min is not None else None,
        target_avg_watts=round(learned_avg_watts, 0) if learned_avg_watts is not None else None,
        target_avg_heartrate=round(learned_avg_heartrate, 0) if learned_avg_heartrate is not None else None,
        route_activity_id=None,
        route_name=best_option.name,
        provider=best_route.provider,
        route_polyline=best_route.route_polyline,
        route_start_lat=selected_start[0],
        route_start_lng=selected_start[1],
        route_start_distance_miles=0.0,
        route_distance_miles=best_route.distance_miles,
        route_elevation_ft=best_route.elevation_ft,
        route_duration_min=best_route.duration_min,
        trail_percent=best_route.trail_percent,
        bike_network_percent=best_route.bike_network_percent,
        unpaved_percent=best_route.unpaved_percent,
        major_road_percent=best_route.major_road_percent,
        novelty_score=best_candidate["novelty_score"],
        overlap_percent=best_candidate["overlap_percent"],
        popularity_score=best_candidate["popularity_score"],
        popularity_summary=best_candidate["popularity_summary"],
        surface_summary=best_route.surface_summary,
        location_label=location_label,
        explanation=list(dict.fromkeys(explanation))[:7],
        included_segments=best_candidate["included_segments"],
        alternatives=alternative_options[1:5],
    )


@app.post("/api/users/{user_id}/recommendations", response_model=list[RecommendationOut])
def recommend_rides(
    user_id: int,
    payload: RecommendationRequest,
    db: Session = Depends(get_db),
) -> list[RecommendationOut]:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    feedback_subquery = (
        db.query(
            RideFeedback.activity_id,
            func.avg(RideFeedback.perceived_effort).label("avg_effort"),
            func.avg(RideFeedback.enjoyment).label("avg_enjoyment"),
            func.max(RideFeedback.ride_style).label("ride_style"),
        )
        .filter(RideFeedback.user_id == user_id)
        .group_by(RideFeedback.activity_id)
        .subquery()
    )

    query = (
        db.query(Activity, feedback_subquery.c.avg_effort, feedback_subquery.c.avg_enjoyment, feedback_subquery.c.ride_style)
        .outerjoin(feedback_subquery, Activity.id == feedback_subquery.c.activity_id)
        .filter(Activity.user_id == user_id)
    )

    if payload.sport_type:
        query = query.filter(Activity.sport_type == payload.sport_type)

    activities = query.all()
    if not activities:
        return []

    results: list[RecommendationOut] = []
    desired_style = payload.desired_style.strip().lower()
    selected_start = None
    if payload.start_lat is not None and payload.start_lng is not None:
        selected_start = (payload.start_lat, payload.start_lng)

    for activity, avg_effort, avg_enjoyment, ride_style in activities:
        distance_miles = (activity.distance_m or 0) * 0.000621371
        elevation_ft = (activity.total_elevation_gain_m or 0) * 3.28084
        start_lat, start_lng = get_activity_start_coordinates(activity)
        start_distance_miles = None

        if payload.max_distance_miles is not None and distance_miles > payload.max_distance_miles:
            continue
        if payload.min_distance_miles is not None and distance_miles < payload.min_distance_miles:
            continue
        if payload.max_elevation_ft is not None and elevation_ft > payload.max_elevation_ft:
            continue
        if payload.min_elevation_ft is not None and elevation_ft < payload.min_elevation_ft:
            continue
        if selected_start is not None:
            if start_lat is None or start_lng is None:
                continue
            start_distance_miles = haversine_miles(selected_start[0], selected_start[1], start_lat, start_lng)
            if (
                payload.max_start_distance_miles is not None
                and start_distance_miles > payload.max_start_distance_miles
            ):
                continue

        score = 0.0
        reasons: list[str] = []

        if ride_style and desired_style in ride_style.lower():
            score += 30
            reasons.append("matches prior feedback style")

        if avg_enjoyment is not None:
            score += float(avg_enjoyment) * 5
            reasons.append("higher enjoyment history")

        if avg_effort is not None and payload.target_effort is not None:
            effort_gap = abs(float(avg_effort) - payload.target_effort)
            score += max(0, 25 - effort_gap * 5)
            reasons.append("close to target effort")
        elif payload.target_effort is None and avg_effort is not None:
            score += 10

        if desired_style in {"brewery", "casual", "social", "easy"}:
            if distance_miles <= 40:
                score += 10
                reasons.append("distance fits casual ride")
            if elevation_ft <= 2500:
                score += 10
                reasons.append("lower climbing load")
        elif desired_style in {"hard", "limit", "training", "push"}:
            if distance_miles >= 30:
                score += 8
                reasons.append("substantial ride length")
            if elevation_ft >= 1500:
                score += 12
                reasons.append("strong climbing stimulus")
            if (activity.average_watts or 0) >= 180 or (activity.suffer_score or 0) >= 80:
                score += 12
                reasons.append("historically demanding ride")
        elif desired_style in {"gravel", "adventure"}:
            if (activity.sport_type or "").lower() in {"ride", "gravelride", "mountainbikeride"}:
                score += 10
                reasons.append("good sport type match")

        if activity.commute:
            score -= 10

        if activity.trainer:
            score -= 15

        if start_distance_miles is not None:
            score += max(0, 25 - start_distance_miles * 2)
            reasons.append("starts near your chosen point")

        if not reasons:
            reasons.append("general match from your history")

        results.append(
            RecommendationOut(
                activity_id=activity.id,
                name=activity.name,
                score=round(score, 2),
                reason=", ".join(dict.fromkeys(reasons)),
                distance_miles=round(distance_miles, 2) if activity.distance_m is not None else None,
                elevation_ft=round(elevation_ft, 0) if activity.total_elevation_gain_m is not None else None,
                sport_type=activity.sport_type,
                start_lat=start_lat,
                start_lng=start_lng,
                start_distance_miles=round(start_distance_miles, 2) if start_distance_miles is not None else None,
            )
        )

    results.sort(key=lambda x: x.score, reverse=True)
    return results[:10]
