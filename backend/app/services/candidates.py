"""Plan a diverse, sensible set of route candidates to send to GraphHopper.

The previous strategy fanned out ~10 round-trip calls keyed only on a fixed
seed table. That produced large parallel API loads but tended to return
visually-similar loops because GraphHopper biases toward the same dominant
road families when only the seed varies. This module builds a smaller,
more diverse candidate list by varying *direction*, *distance*, and *anchor*
in deliberate combinations, and de-duplicating on first-mile bearing so the
3+ surfaced options actually feel different to the rider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Awaitable, Callable

from app.services.places import PlaceCandidate
from app.services.routing import (
    GeneratedRoute,
    GraphHopperClient,
    RoutingError,
    encode_polyline,
)


RouteFactory = Callable[[], Awaitable[GeneratedRoute]]


@dataclass(frozen=True)
class CandidateRequest:
    """A single planned GraphHopper request and the metadata used to score it."""

    mode: str  # "loop" | "destination_route" | "gravel_anchor"
    factory: RouteFactory
    anchor_place: PlaceCandidate | None = None
    heading_deg: float | None = None
    target_distance_miles: float | None = None
    note: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _bearing_deg(start: tuple[float, float], target: tuple[float, float]) -> float:
    """Compass bearing in degrees from start to target."""
    lat1, lng1 = math.radians(start[0]), math.radians(start[1])
    lat2, lng2 = math.radians(target[0]), math.radians(target[1])
    delta_lng = lng2 - lng1
    y = math.sin(delta_lng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lng)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def early_bearing_deg(
    start: tuple[float, float],
    points: list[tuple[float, float]],
    *,
    sample_meters: float = 600.0,
) -> float | None:
    """Compute the bearing of the first ~600m of a route from its start point.

    Used to dedupe candidates that ride out in the same direction and so feel
    like the same loop even when their full geometry diverges.
    """
    if len(points) < 2:
        return None

    cumulative_m = 0.0
    target_index = len(points) - 1
    for i in range(1, len(points)):
        # Local equirectangular approximation is fine for a few hundred meters.
        prev = points[i - 1]
        curr = points[i]
        d_lat = (curr[0] - prev[0]) * 111_320.0
        d_lng = (curr[1] - prev[1]) * 111_320.0 * math.cos(math.radians(curr[0]))
        cumulative_m += math.hypot(d_lat, d_lng)
        if cumulative_m >= sample_meters:
            target_index = i
            break

    return _bearing_deg(start, points[target_index])


def angular_distance_deg(a: float, b: float) -> float:
    """Smallest angle between two compass bearings."""
    diff = abs((a - b) % 360.0)
    return min(diff, 360.0 - diff)


# A small spread of bearings beats a large set of seeds for visual diversity.
DEFAULT_HEADINGS_DEG: tuple[float, ...] = (15.0, 105.0, 195.0, 285.0)
SECONDARY_HEADINGS_DEG: tuple[float, ...] = (60.0, 150.0, 240.0, 330.0)
DEFAULT_SEEDS: tuple[int, ...] = (11, 41, 73)


def pick_loop_distances(
    *,
    target_distance_miles: float | None,
    min_distance_miles: float | None,
    max_distance_miles: float | None,
    count: int = 3,
) -> list[float]:
    """Pick a tight set of distances clustered around the user's target.

    Avoids the old behaviour of producing 6 candidates strung from -12% to +20%
    of target — extra options at the extremes mostly get filtered out by the
    distance gate later, wasting routing calls.
    """
    if target_distance_miles is not None:
        base = target_distance_miles
    elif min_distance_miles is not None and max_distance_miles is not None:
        base = (min_distance_miles + max_distance_miles) / 2
    elif min_distance_miles is not None:
        base = min_distance_miles + 4
    elif max_distance_miles is not None:
        base = max(8.0, max_distance_miles * 0.85)
    else:
        base = 18.0

    # Symmetric spread around the target — small enough to all fall within tolerance.
    if count <= 1:
        spread = (1.0,)
    elif count == 2:
        spread = (0.96, 1.04)
    elif count == 3:
        spread = (0.93, 1.0, 1.08)
    else:
        spread = tuple(0.9 + (i / (count - 1)) * 0.2 for i in range(count))

    distances: list[float] = []
    seen: set[float] = set()
    for multiplier in spread:
        raw = base * multiplier
        if min_distance_miles is not None:
            raw = max(raw, min_distance_miles)
        if max_distance_miles is not None:
            raw = min(raw, max_distance_miles)
        rounded = round(max(6.0, raw), 2)
        key = round(rounded, 1)
        if key in seen:
            continue
        seen.add(key)
        distances.append(rounded)
    return distances or [round(base, 2)]


@dataclass
class CandidatePlanInputs:
    routing_client: GraphHopperClient
    start: tuple[float, float]
    desired_style: str | None
    intensity_label: str | None
    sport_type: str | None
    target_distance_miles: float | None
    min_distance_miles: float | None
    max_distance_miles: float | None
    destination_places: list[PlaceCandidate]
    destination_category: str | None
    gravel_segments: list[PlaceCandidate]
    gravel_requested: bool
    explicit_distance_requested: bool
    bike_path_segments: list[PlaceCandidate] = field(default_factory=list)
    prefer_bike_paths: bool = False
    prefer_unpaved_paths: bool = False


def plan_candidate_requests(inputs: CandidatePlanInputs) -> list[CandidateRequest]:
    """Build the diverse set of GraphHopper calls to dispatch.

    Diversity priorities, in order:
      1. Different headings out of the start (~4 cardinals).
      2. A tight cluster of distances around the user's target (3 by default).
      3. Anchor-based routes when destinations or gravel segments are in play.
    """
    requests: list[CandidateRequest] = []
    distances = pick_loop_distances(
        target_distance_miles=inputs.target_distance_miles,
        min_distance_miles=inputs.min_distance_miles,
        max_distance_miles=inputs.max_distance_miles,
        count=3,
    )
    headings = list(DEFAULT_HEADINGS_DEG)

    # Pair each heading with the *closest-to-target* distance, then add
    # a couple of distance-only variants for shape diversity beyond bearing.
    primary_distance = distances[len(distances) // 2]
    for index, heading in enumerate(headings):
        seed = DEFAULT_SEEDS[index % len(DEFAULT_SEEDS)]
        requests.append(_loop_request(inputs, primary_distance, seed, heading))

    # Add the off-target distances as un-headed loops for shape variety.
    for index, distance in enumerate(distances):
        if abs(distance - primary_distance) < 0.05:
            continue
        seed = DEFAULT_SEEDS[(index + 1) % len(DEFAULT_SEEDS)]
        secondary_heading = SECONDARY_HEADINGS_DEG[index % len(SECONDARY_HEADINGS_DEG)]
        requests.append(_loop_request(inputs, distance, seed, secondary_heading))

    # Destination-bound routes (out + back, optionally with a loop at the destination).
    destination_limit = 1 if inputs.explicit_distance_requested else 2
    for index, place in enumerate(inputs.destination_places[:destination_limit]):
        requests.append(_destination_request(inputs, place, seed=DEFAULT_SEEDS[index % len(DEFAULT_SEEDS)]))

    # Gravel anchor candidates that deliberately route through tagged unpaved segments.
    # When the rider asked for "gravel paths/trails" specifically (not just a
    # gravel ride) we generate multi-anchor candidates more aggressively.
    if inputs.gravel_requested:
        for segment in inputs.gravel_segments[:3]:
            requests.append(_gravel_anchor_request(inputs, [segment]))
        if len(inputs.gravel_segments) >= 2 and (inputs.target_distance_miles or 0) >= 12:
            requests.append(_gravel_anchor_request(inputs, inputs.gravel_segments[:2]))
        if (
            inputs.prefer_unpaved_paths
            and len(inputs.gravel_segments) >= 3
            and (inputs.target_distance_miles or 0) >= 18
        ):
            # Three-anchor route stitches together a much higher unpaved share
            # — this is the "gravel paths" power-user case.
            requests.append(_gravel_anchor_request(inputs, inputs.gravel_segments[:3]))

    # Bike-path anchor candidates: thread the route through nearby cycleways /
    # rail trails / multi-use paths so GraphHopper actually picks them up.
    if inputs.prefer_bike_paths and inputs.bike_path_segments:
        for segment in inputs.bike_path_segments[:3]:
            requests.append(_bike_path_anchor_request(inputs, [segment]))
        if len(inputs.bike_path_segments) >= 2 and (inputs.target_distance_miles or 0) >= 10:
            requests.append(_bike_path_anchor_request(inputs, inputs.bike_path_segments[:2]))

    return requests


def _loop_request(
    inputs: CandidatePlanInputs,
    distance_miles: float,
    seed: int,
    heading_deg: float | None,
) -> CandidateRequest:
    async def _build() -> GeneratedRoute:
        return await inputs.routing_client.generate_round_trip(
            start_lat=inputs.start[0],
            start_lng=inputs.start[1],
            distance_miles=distance_miles,
            seed=seed,
            desired_style=inputs.desired_style,
            intensity_label=inputs.intensity_label,
            sport_type=inputs.sport_type,
            heading=heading_deg,
        )

    return CandidateRequest(
        mode="loop",
        factory=_build,
        heading_deg=heading_deg,
        target_distance_miles=distance_miles,
    )


def _destination_request(
    inputs: CandidatePlanInputs,
    place: PlaceCandidate,
    *,
    seed: int,
) -> CandidateRequest:
    async def _build() -> GeneratedRoute:
        return await build_destination_route(
            routing_client=inputs.routing_client,
            place=place,
            start=inputs.start,
            target_distance_miles=inputs.target_distance_miles,
            desired_style=inputs.desired_style,
            intensity_label=inputs.intensity_label,
            sport_type=inputs.sport_type,
            seed=seed,
        )

    return CandidateRequest(
        mode="destination_route",
        factory=_build,
        anchor_place=place,
        target_distance_miles=inputs.target_distance_miles,
    )


async def build_destination_route(
    *,
    routing_client: GraphHopperClient,
    place: PlaceCandidate,
    start: tuple[float, float],
    target_distance_miles: float | None,
    desired_style: str | None,
    intensity_label: str | None,
    sport_type: str | None,
    seed: int,
) -> GeneratedRoute:
    """Out-and-back to a place, optionally with a loop at the destination
    sized to backfill the requested distance."""
    outbound = await routing_client.generate_route(
        points=[start, (place.lat, place.lng)],
        desired_style=desired_style,
        intensity_label=intensity_label,
        sport_type=sport_type,
    )
    return_leg = await routing_client.generate_route(
        points=[(place.lat, place.lng), start],
        desired_style=desired_style,
        intensity_label=intensity_label,
        sport_type=sport_type,
    )

    segments = [outbound]
    connector_distance = (outbound.distance_miles or 0.0) + (return_leg.distance_miles or 0.0)
    remaining = (target_distance_miles or 0.0) - connector_distance
    if remaining >= 6.0:
        try:
            destination_loop = await routing_client.generate_round_trip(
                start_lat=place.lat,
                start_lng=place.lng,
                distance_miles=remaining,
                seed=seed,
                desired_style=desired_style,
                intensity_label=intensity_label,
                sport_type=sport_type,
            )
            segments.append(destination_loop)
        except RoutingError:
            # If the destination loop fails we still return the out+back; the caller
            # will see an unusually short route in that case but won't 502.
            pass

    segments.append(return_leg)
    return _combine_route_segments(segments)


def _combine_route_segments(segments: list[GeneratedRoute]) -> GeneratedRoute:
    valid = [r for r in segments if r.points]
    if not valid:
        raise RoutingError("Destination route could not be assembled from map segments.")

    combined: list[tuple[float, float]] = []
    for route in valid:
        for point in route.points:
            if combined:
                last = combined[-1]
                # Crude coincidence check (~50ft) so we don't include
                # the duplicated waypoint when stitching legs together.
                if abs(point[0] - last[0]) < 1e-4 and abs(point[1] - last[1]) < 1e-4:
                    continue
            combined.append(point)

    distance_miles = sum(r.distance_miles or 0.0 for r in valid) or None
    elevation_ft = sum(r.elevation_ft or 0.0 for r in valid) or None
    duration_min = sum(r.duration_min or 0 for r in valid) or None

    surfaces = [r.surface_summary for r in valid if r.surface_summary]
    if not surfaces:
        surface_summary = None
    elif len(set(surfaces)) == 1:
        surface_summary = surfaces[0]
    else:
        surface_summary = "Mixed surfaces across the destination approach and loop."

    def weighted(attr: str) -> float | None:
        items = [(getattr(r, attr), r.distance_miles or 0.0) for r in valid if getattr(r, attr) is not None]
        items = [(v, w) for v, w in items if w > 0]
        if not items:
            return None
        total_weight = sum(w for _, w in items)
        return sum(v * w for v, w in items) / total_weight if total_weight else None

    def round_or_none(value: float | None, ndigits: int = 0) -> float | None:
        return round(value, ndigits) if value is not None else None

    return GeneratedRoute(
        route_polyline=encode_polyline(combined),
        points=combined,
        distance_miles=round_or_none(distance_miles, 2),
        elevation_ft=round_or_none(elevation_ft, 0),
        duration_min=duration_min,
        trail_percent=round_or_none(weighted("trail_percent"), 0),
        bike_network_percent=round_or_none(weighted("bike_network_percent"), 0),
        unpaved_percent=round_or_none(weighted("unpaved_percent"), 0),
        major_road_percent=round_or_none(weighted("major_road_percent"), 0),
        surface_summary=surface_summary,
        provider=valid[0].provider,
    )


def _gravel_anchor_request(
    inputs: CandidatePlanInputs,
    segments: list[PlaceCandidate],
) -> CandidateRequest:
    async def _build() -> GeneratedRoute:
        points = [inputs.start, *((s.lat, s.lng) for s in segments), inputs.start]
        return await inputs.routing_client.generate_route(
            points=points,
            desired_style=inputs.desired_style,
            intensity_label=inputs.intensity_label,
            sport_type=inputs.sport_type,
        )

    return CandidateRequest(
        mode="gravel_anchor",
        factory=_build,
        anchor_place=segments[0] if segments else None,
        metadata={"segment_count": len(segments)},
    )


def _bike_path_anchor_request(
    inputs: CandidatePlanInputs,
    segments: list[PlaceCandidate],
) -> CandidateRequest:
    """Route through one or more nearby cycleway segments so GraphHopper
    actually surfaces a path-led loop instead of defaulting to roads.

    We pin the routing profile to plain `bike` (not racingbike/mtb) because
    the bike profile in GraphHopper most strongly prefers `bike_network`
    edges and respects `cycleway` highway tags.
    """
    async def _build() -> GeneratedRoute:
        points = [inputs.start, *((s.lat, s.lng) for s in segments), inputs.start]
        return await inputs.routing_client.generate_route(
            points=points,
            # Force `casual` style so resolve_profile() returns the `bike` profile.
            desired_style="casual",
            intensity_label=None,
            sport_type=None,
        )

    return CandidateRequest(
        mode="bike_path_anchor",
        factory=_build,
        anchor_place=segments[0] if segments else None,
        metadata={"segment_count": len(segments)},
    )


def deduplicate_by_bearing(
    candidates: list[dict[str, Any]],
    *,
    min_separation_deg: float = 35.0,
    keep_at_least: int = 3,
) -> list[dict[str, Any]]:
    """Keep candidates whose initial bearing differs from already-kept ones.

    Operates on the scored-candidate dicts produced by `score_candidate`.
    Falls back to keeping the top `keep_at_least` if the bearing filter would
    cull too aggressively.
    """
    if not candidates:
        return []

    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        bearing = candidate.get("early_bearing_deg")
        if bearing is None:
            kept.append(candidate)
            continue
        if all(
            angular_distance_deg(bearing, existing.get("early_bearing_deg") or 0.0) >= min_separation_deg
            for existing in kept
            if existing.get("early_bearing_deg") is not None
        ):
            kept.append(candidate)

    if len(kept) >= keep_at_least:
        return kept

    # Top up by score from candidates we filtered out.
    extras = [c for c in candidates if c not in kept]
    return kept + extras[: max(0, keep_at_least - len(kept))]
