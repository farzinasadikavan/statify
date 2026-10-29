# Statify

A Spotify-connected podcast dashboard built with FastAPI and React/TypeScript.

## Run

```sh
bash start.sh
```

Open http://127.0.0.1:8765. The prepared environment and frontend build are included in this workspace.

For a fresh installation (Python 3.10+, Node 20.19+):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend
npm install
npm run build
cd ..
bash start.sh
```

## Connect

In your Spotify Developer app, register `http://127.0.0.1:8765/api/spotify/callback`. Enter the public Client ID in the app's connection settings, then select **Continue with Spotify**. No client secret is used. The app owner needs Premium for Spotify Development Mode; other sign-in accounts may need to be allowlisted.

This version requires a fresh sign-in to authorize `user-library-read`, `user-library-modify`, `user-read-playback-position`, and `user-read-private`. Spotify sign-in automatically loads your profile and followed podcasts. Your profile name and image appear in the sidebar. No manual imports or demo data are included.

## Features

- Followed podcasts with reported episode count, available catalog duration, estimated listened content, and estimated remaining content.
- Podcast descriptions, artwork, languages, and episode details.
- Spotify search with ten results per page; open a result to calculate its full available catalog duration.
- Follow a podcast from its details. Following writes to the signed-in Spotify library only when the user clicks Follow.
- Automatic syncing every 15 minutes while the app is open, manual refresh, token refresh, and rate-limit handling.

## Progress definitions

Spotify returns `resume_point.fully_played` and `resume_point.resume_position_ms` when it supplies progress. Played episodes count as their full duration; other episodes use the resume position clamped to the episode length. Remaining content is duration minus this estimate. Replays are not accumulated. Skipping, rewinding, or manually marking episodes played can make this different from actual listening time.

If any retrieved episode has missing progress, the show's aggregate listened/remaining values are **Unavailable**, not zero. Individual episodes retain available progress values. Catalog duration and progress cover only retrieved episodes; a partial flag identifies mismatched reported/retrieved counts. Catalog changes and market restrictions may affect coverage. These values are not lifetime listening hours.

Official reference: https://developer.spotify.com/documentation/web-api/reference/get-a-shows-episodes

## Implementation notes

This remains a single-user app bound to 127.0.0.1. Account tokens, profile, followed podcasts, and episode/progress snapshots are saved atomically in `data/statify.sqlite3` and restored at startup. The database directory is owner-only (0700) and database file is owner-readable/writable only (0600). Tokens are stored in this protected database, not browser storage. Expired access tokens refresh automatically; revoked Spotify authorization still requires sign-in. Signing out removes saved account credentials and cached Spotify data while retaining the public Client ID. Back up the database only to a trusted location; it contains account credentials. Do not run multiple Statify server processes against the same database. The public Client ID is remembered in browser storage. Never expose this server to the internet without adding access control. Access logs are disabled to avoid printing OAuth callback codes.

Legacy import endpoints and screens have been removed. The old project's history database has not been deleted or copied into this workspace.

## Verification

```sh
.venv/bin/python -m pip install pytest httpx
.venv/bin/python -m pytest backend -q
cd frontend
npm run build
```

Tests mock Spotify to check PKCE/state, token refresh, pagination safety, rate limits, progress estimates and missing values, search, following, disconnect cancellation, and removal of import endpoints. Real Spotify access requires the user's authorization.
