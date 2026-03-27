from datetime import datetime

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: int
    strava_athlete_id: int
    username: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    profile_medium: str | None = None
    profile: str | None = None

    class Config:
        from_attributes = True


class ActivityOut(BaseModel):
    id: int
    strava_activity_id: int
    name: str
    sport_type: str | None = None
    distance_m: float | None = None
    moving_time_s: int | None = None
    elapsed_time_s: int | None = None
    total_elevation_gain_m: float | None = None
    average_speed_mps: float | None = None
    average_heartrate: float | None = None
    average_watts: float | None = None
    weighted_average_watts: int | None = None
    kilojoules: float | None = None
    suffer_score: float | None = None
    trainer: bool | None = None
    commute: bool | None = None
    start_date: datetime | None = None
    start_lat: float | None = None
    start_lng: float | None = None
    location_city: str | None = None
    location_state: str | None = None
    location_country: str | None = None

    class Config:
        from_attributes = True


class RideFeedbackCreate(BaseModel):
    activity_id: int
    perceived_effort: int = Field(ge=1, le=10)
    enjoyment: int = Field(ge=1, le=10)
    matched_intent: bool
    ride_style: str | None = None
    notes: str | None = None


class RideFeedbackOut(BaseModel):
    id: int
    user_id: int
    activity_id: int
    perceived_effort: int
    enjoyment: int
    matched_intent: bool
    ride_style: str | None = None
    notes: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class RecommendationRequest(BaseModel):
    desired_style: str
    max_distance_miles: float | None = Field(default=None, gt=0)
    min_distance_miles: float | None = Field(default=None, gt=0)
    max_elevation_ft: float | None = Field(default=None, ge=0)
    min_elevation_ft: float | None = Field(default=None, ge=0)
    target_effort: int | None = Field(default=None, ge=1, le=10)
    sport_type: str | None = None
    start_lat: float | None = None
    start_lng: float | None = None
    max_start_distance_miles: float | None = Field(default=None, gt=0)


class RecommendationOut(BaseModel):
    activity_id: int
    name: str
    score: float
    reason: str
    distance_miles: float | None = None
    elevation_ft: float | None = None
    sport_type: str | None = None
    start_lat: float | None = None
    start_lng: float | None = None
    start_distance_miles: float | None = None
    moving_time_min: int | None = None
    average_watts: float | None = None
    average_heartrate: float | None = None
    location_label: str | None = None


class IncludedSegmentOut(BaseModel):
    id: int
    name: str
    distance_miles: float | None = None
    avg_grade: float | None = None
    climb_category: int | None = None
    athlete_count: int | None = None
    effort_count: int | None = None
    star_count: int | None = None
    route_overlap_percent: float | None = None
    popularity_score: float | None = None
    current_time_seconds: int | None = None
    current_time_source: str | None = None
    current_time_date: str | None = None
    athlete_effort_count: int | None = None


class GeneratedRouteOptionOut(BaseModel):
    name: str
    score: float
    reason: str
    summary: str | None = None
    destination_label: str | None = None
    destination_category: str | None = None
    destination_distance_miles: float | None = None
    distance_miles: float | None = None
    elevation_ft: float | None = None
    duration_min: int | None = None
    route_polyline: str | None = None
    route_start_lat: float | None = None
    route_start_lng: float | None = None
    trail_percent: float | None = None
    bike_network_percent: float | None = None
    unpaved_percent: float | None = None
    major_road_percent: float | None = None
    novelty_score: float | None = None
    overlap_percent: float | None = None
    popularity_score: float | None = None
    surface_summary: str | None = None
    provider: str | None = None


class RidePlanRequest(BaseModel):
    ride_brief: str = Field(min_length=1)
    desired_style: str | None = None
    max_distance_miles: float | None = Field(default=None, gt=0)
    min_distance_miles: float | None = Field(default=None, gt=0)
    max_elevation_ft: float | None = Field(default=None, ge=0)
    min_elevation_ft: float | None = Field(default=None, ge=0)
    target_effort: int | None = Field(default=None, ge=1, le=10)
    sport_type: str | None = None
    start_lat: float | None = None
    start_lng: float | None = None
    start_label: str | None = None
    max_start_distance_miles: float | None = Field(default=None, gt=0)


class RidePlanOut(BaseModel):
    title: str
    summary: str
    ride_brief: str
    desired_style: str | None = None
    intensity_label: str | None = None
    destination_label: str | None = None
    destination_category: str | None = None
    destination_distance_miles: float | None = None
    target_distance_miles: float | None = None
    target_elevation_ft: float | None = None
    target_duration_min: int | None = None
    target_avg_watts: float | None = None
    target_avg_heartrate: float | None = None
    route_activity_id: int | None = None
    route_name: str | None = None
    provider: str | None = None
    route_polyline: str | None = None
    route_start_lat: float | None = None
    route_start_lng: float | None = None
    route_start_distance_miles: float | None = None
    route_distance_miles: float | None = None
    route_elevation_ft: float | None = None
    route_duration_min: int | None = None
    trail_percent: float | None = None
    bike_network_percent: float | None = None
    unpaved_percent: float | None = None
    major_road_percent: float | None = None
    novelty_score: float | None = None
    overlap_percent: float | None = None
    popularity_score: float | None = None
    popularity_summary: str | None = None
    surface_summary: str | None = None
    location_label: str | None = None
    explanation: list[str] = Field(default_factory=list)
    included_segments: list[IncludedSegmentOut] = Field(default_factory=list)
    alternatives: list[GeneratedRouteOptionOut] = Field(default_factory=list)


class RouteExportRequest(BaseModel):
    route_name: str = Field(min_length=1)
    route_polyline: str = Field(min_length=1)
    target: str | None = None
