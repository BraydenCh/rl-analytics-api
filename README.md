# RL Analytics API

Backend API for a Rocket League analytics platform. This service handles authentication flows, replay ingestion, match/stat retrieval, and account-linking logic backed by Supabase.

## Features

- Epic Games OAuth callback handling and session cookie management
- Steam account linking and unlinking
- Xbox account linking flow (Microsoft OAuth + Xbox token exchange)
- Replay file upload and parsing via `bin/rrrocket`
- Match persistence, retrieval, and user-scoped match history
- Aggregated player/user stat endpoints

## Tech Stack

- Python 3
- FastAPI
- Uvicorn
- Supabase (async client)
- HTTPX

## Project Structure

```text
.
├── api/
│   ├── app_state.py
│   ├── main.py
│   ├── routes/
│   │   ├── auth/
│   │   ├── matches.py
│   │   └── stats.py
│   └── utils/
│       └── epic_auth_utils.py
├── bin/
│   └── rrrocket
├── local_storage/
├── requirements.txt
└── README.md
```

## Prerequisites

- Python 3.10+ recommended
- A Supabase project with tables/views used by this API
- Epic OAuth application credentials
- (Optional) Microsoft/Xbox OAuth credentials for Xbox linking

## Environment Variables

Create a `.env` file in the repository root:

```env
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_service_or_anon_key

EPIC_CLIENT_ID=your_epic_client_id
EPIC_CLIENT_SECRET=your_epic_client_secret
EPIC_REDIRECT_URI=http://localhost:8000/auth/callback/epic

XBOX_CLIENT_ID=your_xbox_client_id
XBOX_CLIENT_SECRET=your_xbox_client_secret
XBOX_REDIRECT_URI=http://localhost:8000/auth/xbox/callback
```

Notes:

- CORS and several redirect values currently target `http://localhost:3000` (frontend).
- Steam OpenID callback defaults to `http://localhost:8000/auth/steam/callback` in code.

## Local Development Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the API:

```bash
uvicorn api.main:app --reload --host localhost --port 8000
```

4. Open interactive docs:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Core API Endpoints

Health and session:

- `GET /` - Service health check
- `GET /user_info` - Current authenticated user profile + linked platform IDs
- `POST /auth/logout` - Clear app session cookie

Epic and platform auth:

- `GET /auth/callback/epic` - Epic OAuth callback
- `GET /auth/login/steam` - Begin Steam OpenID flow
- `GET /auth/steam/callback` - Complete Steam linking flow
- `POST /auth/steam/unlink` - Soft-unlink active Steam link
- `GET /auth/login/xbox` - Begin Microsoft OAuth flow for Xbox
- `GET /auth/xbox/callback` - Complete Xbox linking flow

Replay and matches:

- `POST /upload_replay/` - Upload and parse a replay file
- `GET /matches/` - List recent matches
- `GET /matches/{match_id}` - Get a single match with player stats
- `DELETE /matches/{match_id}` - Delete a match uploaded by the authenticated user

User and player analytics:

- `GET /user_matches` - Matches for the authenticated user
- `GET /user_uploads` - Matches uploaded by the authenticated user
- `GET /user_stats` - Aggregated stats for the authenticated user
- `GET /players/{player_id}/matches` - Matches for a specific player
- `GET /players/{player_id}/stats` - Aggregated stats for a specific player

## Operational Notes

- `local_storage/` is used for temporary replay file handling.
- The API depends on DB structures such as `users`, `players`, `linked_accounts`, `matches`, `player_match_stats`, `user_match_uploads`, and `player_career_stats`.
- Ensure `bin/rrrocket` is executable on your machine.

## Current Status

The service is functionally centered in `api/main.py`, with route modules present for ongoing modularization work.
