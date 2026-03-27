# Spin Scout

Local-first Strava-connected cycling planner.

## What this starter does

- Strava OAuth login
- Saves athlete + token info to Postgres
- Imports recent activities from Strava
- Stores ride feedback
- Generates new route loops from map and trail data
- Uses ride history to set targets and avoid already-ridden footprints
- Exports generated routes as GPX or TCX for major cycling apps and head units

## Run

1. Copy `.env.example` to `.env`
2. Fill in your Strava app credentials
3. Add a `GRAPHHOPPER_API_KEY` for map-based route generation
4. Run `docker compose up --build`
5. Open `http://localhost:5173`
