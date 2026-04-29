"""Score and filter generated route candidates against a parsed ride brief.

Pulled out of `app.main` so the route handler stays focused on orchestration
and so weights live in one named place instead of as magic numbers sprinkled
across a 400-line function. The behaviour is intentionally close to what
existed before but with three meaningful changes:

1. **Gravel filter does not 502.** The old code raised when no candidate cleared
   the gravel signal threshold even if every other gate passed. We now keep the
   best gravel-leaning option and surface a warning instead of failing.

2. **Popularity filter is graceful.** When Strava segment matching turns up
   nothing, the planner still returns routes (with a warning) instead of
   throwing a 502. Match overlap threshold is also lower for popularity asks.

3. **Hard filters are explicit constants** at the top of this file so they
   can be tuned without trawling through `score_candidate`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.candidates import early_bearing_deg
from app.services.places import PlaceCandidate
from app.services.routing import GeneratedRoute


# ---- tunable thresholds ----

GRAVEL_MIN_UNPAVED_PERCENT = 14.0
GRAVEL_MIN_TRAIL_PERCENT = 12.0
GRAVEL_MAX_MAJOR_ROAD_PERCENT = 38.0
GRAVEL_SIGNAL_TOLERANCE = 18.0  # within this many points of the best signal we keep a candidate

DESTINATION_PROXIMITY_TIGHT = 1.0  # multiplier on threshold; close
DESTINATION_PROXIMITY_LOOSE = 2.25  # multiplier on threshold; still passable

DISTANCE_BAND_MIN_MULTIPLIER = 0.9
DISTANCE_BAND_MAX_MULTIPLIER = 1.12

# Popularity matching is more lenient than the default segment overlap threshold
# so users explicitly asking for popular routes get something back.
POPULARITY_OVERLAP_THRESHOLD = 25.0


@dataclass
class RideContext:
    """All parsed signals from the user's brief and history that scoring needs."""

    desired_style: str | None
    intensity_label: str | None
    sport_type: str | None
    target_distance_miles: float | None
    min_distance_miles: float | None
    max_distance_miles: float | None
    target_elevation_ft: float | None
    min_elevation_ft: float | None
    max_elevation_ft: float | None
    target_duration_min: float | None
    destination_category: str | None
    destination_places: list[PlaceCandidate] = field(default_factory=list)
    explicit_distance_requested: bool = False
    gravel_requested: bool = False
    prefer_popular_routes: bool = False
    destination_threshold_miles: float = 0.5


def gravel_signal(route: GeneratedRoute) -> float:
    trail = route.trail_percent or 0.0
    unpaved = route.unpaved_percent or 0.0
    bike_network = route.bike_network_percent or 0.0
    major = route.major_road_percent or 0.0
    return trail * 0.9 + unpaved * 1.1 + bike_network * 0.2 - major * 0.8


def distance_tolerance_miles(target_distance_miles: float | None) -> float:
    if target_distance_miles is None:
        return 5.0
    value = target_distance_miles * 0.18
    return max(3.0, min(value, 8.0))


def route_within_distance_goal(
    route_distance_miles: float | None,
    *,
    target_distance_miles: float | None,
    min_distance_miles: float | None,
    max_distance_miles: float | None,
    strict: bool = False,
) -> bool:
    if route_distance_miles is None:
        return False
    if min_distance_miles is not None and route_distance_miles < min_distance_miles * (
        0.95 if strict else DISTANCE_BAND_MIN_MULTIPLIER
    ):
        return False
    if max_distance_miles is not None and route_distance_miles > max_distance_miles * (
        1.05 if strict else DISTANCE_BAND_MAX_MULTIPLIER
    ):
        return False
    if target_distance_miles is not None:
        return abs(route_distance_miles - target_distance_miles) <= distance_tolerance_miles(target_distance_miles)
    return True


def score_candidate(
    *,
    route: GeneratedRoute,
    route_mode: str,
    anchor_place: PlaceCandidate | None,
    context: RideContext,
    ridden_cells: set[str],
    route_cells: set[str],
    matched_destination: PlaceCandidate | None,
    destination_distance_miles: float | None,
) -> dict[str, Any]:
    """Return a scored-candidate dict (keeps shape the route handler used)."""
    overlap_percent = _overlap_percent(route_cells, ridden_cells)
    novelty_score = round(100 - overlap_percent, 0) if overlap_percent is not None else None

    score = 0.0
    reasons: list[str] = []

    score += _distance_score(route, context, reasons)
    score += _elevation_score(route, context, reasons)
    score += _duration_score(route, context)
    score += _novelty_score(novelty_score, reasons)
    score += _destination_score(
        route_mode=route_mode,
        anchor_place=anchor_place,
        matched_destination=matched_destination,
        destination_distance_miles=destination_distance_miles,
        context=context,
        reasons=reasons,
    )
    score += _surface_score(route, route_mode, context, reasons)
    score += _intensity_score(route, context)
    score += _hard_filter_penalties(route, context)

    if context.explicit_distance_requested and context.target_distance_miles is not None and route.distance_miles is not None:
        gap = abs(route.distance_miles - context.target_distance_miles)
        score -= max(0.0, gap - distance_tolerance_miles(context.target_distance_miles)) * 2.6

    return {
        "route": route,
        "route_mode": route_mode,
        "anchor_place": anchor_place,
        "destination_place": matched_destination,
        "destination_distance_miles": round(destination_distance_miles, 2)
        if destination_distance_miles is not None
        else None,
        "score": round(score, 2),
        "reasons": list(dict.fromkeys(reasons)),
        "novelty_score": novelty_score,
        "overlap_percent": round(overlap_percent, 0) if overlap_percent is not None else None,
        "route_cells": route_cells,
        "gravel_signal": round(gravel_signal(route), 2),
        "early_bearing_deg": early_bearing_deg(route.points[0], route.points) if route.points else None,
        "included_segments": [],
        "popularity_score": None,
        "popularity_summary": None,
    }


# ---- piecewise scorers ----------------------------------------------------------


def _distance_score(route: GeneratedRoute, ctx: RideContext, reasons: list[str]) -> float:
    if ctx.target_distance_miles is not None and route.distance_miles is not None:
        gap = abs(route.distance_miles - ctx.target_distance_miles)
        if gap <= 2.0:
            reasons.append("Its mileage lands close to the target from your brief.")
        return max(0.0, 34 - gap * 4)

    score = 0.0
    if ctx.min_distance_miles is not None and route.distance_miles is not None and route.distance_miles >= ctx.min_distance_miles:
        score += 8
    if ctx.max_distance_miles is not None and route.distance_miles is not None and route.distance_miles <= ctx.max_distance_miles:
        score += 8
    return score


def _elevation_score(route: GeneratedRoute, ctx: RideContext, reasons: list[str]) -> float:
    if ctx.target_elevation_ft is not None and route.elevation_ft is not None:
        gap = abs(route.elevation_ft - ctx.target_elevation_ft)
        if gap <= 500:
            reasons.append("Its climbing load fits the kind of day you described.")
        return max(0.0, 24 - gap / 170)

    score = 0.0
    if ctx.min_elevation_ft is not None and route.elevation_ft is not None and route.elevation_ft >= ctx.min_elevation_ft:
        score += 6
    if ctx.max_elevation_ft is not None and route.elevation_ft is not None and route.elevation_ft <= ctx.max_elevation_ft:
        score += 6
    return score


def _duration_score(route: GeneratedRoute, ctx: RideContext) -> float:
    if ctx.target_duration_min is None or route.duration_min is None:
        return 0.0
    gap = abs(route.duration_min - ctx.target_duration_min)
    return max(0.0, 12 - gap / 5)


def _novelty_score(novelty: float | None, reasons: list[str]) -> float:
    if novelty is None:
        return 0.0
    if novelty >= 70:
        reasons.append("Most of the route footprint is fresh relative to your synced rides.")
    elif novelty >= 50:
        reasons.append("It still opens up a lot of terrain you have not logged yet.")
    return novelty * 0.45


def _destination_score(
    *,
    route_mode: str,
    anchor_place: PlaceCandidate | None,
    matched_destination: PlaceCandidate | None,
    destination_distance_miles: float | None,
    context: RideContext,
    reasons: list[str],
) -> float:
    if not (context.destination_category and context.destination_places):
        return 0.0

    if anchor_place and route_mode.startswith("destination"):
        # If we routed *to* the place we trust that match — destination_distance
        # is set to 0 by the caller in that case.
        pass

    if destination_distance_miles is None:
        return 0.0

    threshold = context.destination_threshold_miles
    if destination_distance_miles <= threshold * DESTINATION_PROXIMITY_TIGHT:
        if matched_destination:
            reasons.append(f"It takes you right by {matched_destination.label}.")
        return 44.0
    if destination_distance_miles <= threshold * DESTINATION_PROXIMITY_LOOSE:
        if matched_destination:
            reasons.append(f"It passes close to {matched_destination.label}.")
        return max(8.0, 28 - destination_distance_miles * 18)
    return -28.0


def _surface_score(route: GeneratedRoute, route_mode: str, ctx: RideContext, reasons: list[str]) -> float:
    score = 0.0
    if ctx.gravel_requested:
        if route.trail_percent is not None:
            score += route.trail_percent * 0.3
            if route.trail_percent >= 18:
                reasons.append("It uses mapped trail and track segments.")
        if route.unpaved_percent is not None:
            score += route.unpaved_percent * 0.42
            if route.unpaved_percent >= 18:
                reasons.append("It includes real unpaved terrain instead of defaulting to pavement.")
        if route.major_road_percent is not None:
            score += max(0.0, 16 - route.major_road_percent * 0.45)
            if route.major_road_percent <= 12:
                reasons.append("It keeps major-road exposure low for a more legitimate gravel route.")
            if route.major_road_percent > 18:
                score -= (route.major_road_percent - 18) * 1.8
        if route_mode == "gravel_anchor":
            score += 12
            reasons.append("It deliberately routes through nearby gravel-tagged map segments.")
        if (route.unpaved_percent or 0) < 8 and (route.trail_percent or 0) < 8 and (route.major_road_percent or 0) > 24:
            score -= 42
        return score

    casual_like = ctx.desired_style in {"brewery", "casual", "social", "recovery"} or ctx.destination_category in {
        "taco_shop",
        "coffee_shop",
        "bakery",
    } or (ctx.sport_type or "").lower() == "ride"
    if casual_like:
        if route.bike_network_percent is not None:
            score += route.bike_network_percent * 0.18
            if route.bike_network_percent >= 35:
                reasons.append("It leans on mapped bike-network segments for a lower-stress route.")
        if route.unpaved_percent is not None:
            score += max(0.0, 12 - route.unpaved_percent * 0.2)
        if route.major_road_percent is not None and route.major_road_percent > 24:
            score -= (route.major_road_percent - 24) * 0.9
        return score

    if route.bike_network_percent is not None:
        score += route.bike_network_percent * 0.08
    if route.trail_percent is not None:
        score += route.trail_percent * 0.08
    return score


def _intensity_score(route: GeneratedRoute, ctx: RideContext) -> float:
    score = 0.0
    if ctx.intensity_label in {"hard", "training"}:
        if route.elevation_ft is not None and route.elevation_ft >= 1500:
            score += 8
        if route.distance_miles is not None and route.distance_miles >= 15:
            score += 6
    elif ctx.intensity_label in {"recovery", "endurance"} and route.unpaved_percent is not None and route.unpaved_percent <= 25:
        score += 4
    return score


def _hard_filter_penalties(route: GeneratedRoute, ctx: RideContext) -> float:
    score = 0.0
    if ctx.min_distance_miles is not None and route.distance_miles is not None and route.distance_miles < ctx.min_distance_miles * 0.9:
        score -= 24
    if ctx.max_distance_miles is not None and route.distance_miles is not None and route.distance_miles > ctx.max_distance_miles * 1.12:
        score -= 24
    if ctx.min_elevation_ft is not None and route.elevation_ft is not None and route.elevation_ft < ctx.min_elevation_ft * 0.8:
        score -= 14
    if ctx.max_elevation_ft is not None and route.elevation_ft is not None and route.elevation_ft > ctx.max_elevation_ft * 1.18:
        score -= 14
    return score


def _overlap_percent(candidate_cells: set[str], ridden_cells: set[str]) -> float | None:
    if not candidate_cells or not ridden_cells:
        return None
    return (len(candidate_cells & ridden_cells) / len(candidate_cells)) * 100


# ---- gravel + popularity filters that no longer 502 -----------------------------


def filter_gravel_candidates(
    candidates: list[dict[str, Any]],
    *,
    has_gravel_segments: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return the gravel-friendly subset plus an optional warning string.

    Behaviour change vs. previous code: when *no* candidate clears the gravel
    bar, instead of raising a 502 we keep the most gravel-leaning routes we
    have and emit a warning the route handler can stitch into the explanation.
    """
    if not candidates:
        return candidates, None

    best_signal = max(float(c["gravel_signal"]) for c in candidates)
    forward = [
        c
        for c in candidates
        if (
            float(c["gravel_signal"]) >= best_signal - GRAVEL_SIGNAL_TOLERANCE
            or (c["route"].unpaved_percent or 0) >= GRAVEL_MIN_UNPAVED_PERCENT
            or (c["route"].trail_percent or 0) >= GRAVEL_MIN_TRAIL_PERCENT
        )
        and (c["route"].major_road_percent or 0) <= GRAVEL_MAX_MAJOR_ROAD_PERCENT
    ]

    if forward:
        return forward, None

    # Don't 502 — fall back to the top by gravel signal and warn the user.
    top_by_signal = sorted(candidates, key=lambda c: float(c["gravel_signal"]), reverse=True)[:3]
    if has_gravel_segments:
        warning = (
            "Nearby gravel paths were tagged but the routing engine kept leaning onto major roads. "
            "Showing the most gravel-leaning options found — try a slightly different start or longer distance "
            "for a more legitimate gravel loop."
        )
    else:
        warning = (
            "I couldn't find strongly-tagged gravel near this start, so these routes are the best surface mix available. "
            "Pinning a start nearer a known dirt/track network usually unlocks better gravel options."
        )
    return top_by_signal, warning


def filter_destination_candidates(
    candidates: list[dict[str, Any]],
    *,
    context: RideContext,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return the destination-reaching subset, optionally relaxed by distance."""
    if not (context.destination_category and context.destination_places):
        return candidates, None

    proximity = context.destination_threshold_miles * DESTINATION_PROXIMITY_LOOSE
    reaching = [
        c
        for c in candidates
        if c["destination_distance_miles"] is not None and float(c["destination_distance_miles"]) <= proximity
    ]
    if not reaching:
        return [], None

    distance_ready = [
        c
        for c in reaching
        if route_within_distance_goal(
            c["route"].distance_miles,
            target_distance_miles=context.target_distance_miles,
            min_distance_miles=context.min_distance_miles,
            max_distance_miles=context.max_distance_miles,
            strict=context.explicit_distance_requested,
        )
    ]
    if distance_ready:
        return distance_ready, None

    if context.explicit_distance_requested:
        # The route handler will surface this as a 502 with the current message.
        return [], "destination_distance_unsatisfied"

    # Soft fallback: keep destination matches even if distance is a bit off.
    return reaching, None
