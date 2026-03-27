from __future__ import annotations

from datetime import datetime
import json

import httpx
from sqlalchemy.orm import Session

from app.models import Activity, User


class StravaClient:
    BASE_URL = "https://www.strava.com/api/v3"
    TOKEN_URL = "https://www.strava.com/oauth/token"

    async def exchange_code(self, *, client_id: str, client_secret: str, code: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            return response.json()

    async def refresh_token(self, *, client_id: str, client_secret: str, refresh_token: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            response.raise_for_status()
            return response.json()

    async def get_activities(self, access_token: str, per_page: int = 50, page: int = 1) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.BASE_URL}/athlete/activities",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"per_page": per_page, "page": page},
            )
            response.raise_for_status()
            return response.json()

    async def explore_segments(
        self,
        access_token: str,
        *,
        bounds: tuple[float, float, float, float],
        activity_type: str = "riding",
    ) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.BASE_URL}/segments/explore",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "bounds": ",".join(f"{value:.6f}" for value in bounds),
                    "activity_type": activity_type,
                },
            )
            response.raise_for_status()
            payload = response.json()
            segments = payload.get("segments")
            return segments if isinstance(segments, list) else []

    async def get_segment(self, access_token: str, segment_id: int) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.BASE_URL}/segments/{segment_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}


async def ensure_valid_token(user: User, *, client_id: str, client_secret: str, db: Session) -> str:
    now_ts = int(datetime.utcnow().timestamp())
    if user.expires_at > now_ts + 60:
        return user.access_token

    client = StravaClient()
    token_data = await client.refresh_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=user.refresh_token,
    )
    user.access_token = token_data["access_token"]
    user.refresh_token = token_data["refresh_token"]
    user.expires_at = token_data["expires_at"]
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.access_token


def upsert_user_from_token_data(db: Session, token_data: dict) -> User:
    athlete = token_data["athlete"]
    user = db.query(User).filter(User.strava_athlete_id == athlete["id"]).first()

    if user is None:
        user = User(
            strava_athlete_id=athlete["id"],
            username=athlete.get("username"),
            firstname=athlete.get("firstname"),
            lastname=athlete.get("lastname"),
            city=athlete.get("city"),
            state=athlete.get("state"),
            country=athlete.get("country"),
            profile_medium=athlete.get("profile_medium"),
            profile=athlete.get("profile"),
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=token_data["expires_at"],
        )
        db.add(user)
    else:
        user.username = athlete.get("username")
        user.firstname = athlete.get("firstname")
        user.lastname = athlete.get("lastname")
        user.city = athlete.get("city")
        user.state = athlete.get("state")
        user.country = athlete.get("country")
        user.profile_medium = athlete.get("profile_medium")
        user.profile = athlete.get("profile")
        user.access_token = token_data["access_token"]
        user.refresh_token = token_data["refresh_token"]
        user.expires_at = token_data["expires_at"]

    db.commit()
    db.refresh(user)
    return user


def upsert_activities(db: Session, user: User, activities: list[dict]) -> int:
    inserted_or_updated = 0

    for item in activities:
        activity = (
            db.query(Activity)
            .filter(
                Activity.user_id == user.id,
                Activity.strava_activity_id == item["id"],
            )
            .first()
        )

        start_date = None
        if item.get("start_date"):
            try:
                start_date = datetime.fromisoformat(item["start_date"].replace("Z", "+00:00"))
            except ValueError:
                start_date = None

        payload = {
            "name": item.get("name", "Untitled Activity"),
            "sport_type": item.get("sport_type"),
            "distance_m": item.get("distance"),
            "moving_time_s": item.get("moving_time"),
            "elapsed_time_s": item.get("elapsed_time"),
            "total_elevation_gain_m": item.get("total_elevation_gain"),
            "average_speed_mps": item.get("average_speed"),
            "max_speed_mps": item.get("max_speed"),
            "average_heartrate": item.get("average_heartrate"),
            "max_heartrate": item.get("max_heartrate"),
            "average_watts": item.get("average_watts"),
            "weighted_average_watts": item.get("weighted_average_watts"),
            "kilojoules": item.get("kilojoules"),
            "suffer_score": item.get("suffer_score"),
            "trainer": item.get("trainer"),
            "commute": item.get("commute"),
            "start_date": start_date,
            "raw_json": json.dumps(item),
        }

        if activity is None:
            activity = Activity(
                user_id=user.id,
                strava_activity_id=item["id"],
                **payload,
            )
            db.add(activity)
        else:
            for key, value in payload.items():
                setattr(activity, key, value)

        inserted_or_updated += 1

    db.commit()
    return inserted_or_updated
