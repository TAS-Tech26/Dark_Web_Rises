# Dark Web Rises -- Backend

FastAPI + websocket backend for the "Dark Web Rises" team game (AI-image
Chinese-Whispers). See `docs/DWR_Code_Review_Report.docx` (one level up, in
the shared `Code_Review` folder) for the full code review this README's
deployment notes accompany.

## Requirements

- **Python 3.10 or newer** (3.11+ preferred). `enumerations.py` includes a
  compatibility shim for `enum.StrEnum` on 3.10; on anything older than 3.10
  the app will not start.
- See `requirements.txt`. Install with:
  ```
  pip install -r requirements.txt
  ```

## Configuration

Copy `.env.example` to `.env` and fill in real values:

- `CORS_ALLOWED_ORIGINS` -- comma-separated list of every origin the
  frontend is served from (Azure Static Web Apps URL, Krutrim Cloud URL,
  your domain, local dev). **Required for any non-localhost deployment** --
  without it the browser will block the frontend from reaching this API.
- `MAX_CONCURRENT_IMAGE_REQUESTS`, `IMAGE_GEN_TIMEOUT_SECONDS`,
  `IMAGE_GEN_MAX_RETRIES`, `IMAGE_GEN_BACKOFF_SECONDS`, `MAX_PROMPT_LENGTH`
  -- tuning for the Pollinations AI image-generation calls. Defaults are
  sane for the ~1200-concurrent-user target; see the code review for the
  reasoning.
- `LOG_LEVEL` -- `INFO` by default.
- `HF_TOKEN` is **no longer used**. Image generation now goes through the
  public Pollinations API; the old Hugging Face Inference Client code path
  was dead code and has been removed.

## The in-memory roster

Usernames/passwords and team assignments are hardcoded in `game.py`
(`player_data` / `admin_data`) and held in memory for the lifetime of the
process, per the current product requirements -- there is no database yet.

**Before every event:**

1. Replace the placeholder usernames/passwords in `game.py` with the real
   roster.
2. Make sure `game_state.json` (if present from a previous run/test) is
   deleted, or intentionally left in place only if you are resuming a
   crashed mid-event process. The app logs a loud warning at startup if
   this file already exists, precisely so a stale checkpoint from a
   previous event doesn't silently make a brand-new game "resume" with old
   scores and skip straight to game over.

Passwords are salted+hashed in memory (`models/users.py`, stdlib
PBKDF2-HMAC-SHA256) the moment the server starts -- they are never held or
compared as plain text after that point.

## Architecture constraint: single process only

Game state (`GameServer`, `Team`, connected sockets, scores, the admin
session-token table) lives entirely in the memory of one Python process.
**This app cannot be horizontally scaled across multiple worker processes
or machines** -- doing so would split players across processes with
inconsistent state (e.g. a player's team on worker A wouldn't see them as
connected from worker B). Run exactly one Uvicorn worker:

```
uvicorn game:app --host 0.0.0.0 --port 8000 --workers 1
```

Scale vertically (more CPU/RAM on the one instance) if needed.

### Sizing for ~1200 concurrent users

Three things dominate, all documented in the second-pass code review:

1. **Round-end CLIP scoring is the bottleneck**, not the websockets. With
   ~300 teams finishing a round at the same moment, each scoring call is two
   RN50 image encodes. Scoring runs on a dedicated, bounded thread pool
   (`CLIP_WORKER_THREADS`, default = CPU count) rather than the shared
   default executor. Budget CPU cores accordingly; the inter-round break
   absorbs some of it.
2. **Image delivery.** `IMAGE_DELIVERY=url` (the default) writes generated
   images to `static/generated/` and sends a short path. The legacy
   `base64` mode inlines 200-400 KB per player per turn, which is several
   gigabytes of websocket egress over a full event, all serialised through
   the one event loop. Put a reverse proxy or CDN in front of `/static` if
   you can. Note that `static/generated/` grows for the duration of the
   event -- clear it between events.
3. **Login is CPU-bound.** Password verification is PBKDF2 at 200k
   iterations (~56 ms per attempt). It now runs on a worker thread rather
   than inline on the event loop -- 1200 attendees logging in over a couple
   of minutes would otherwise block the loop for over a minute in
   aggregate. Per-socket login rate limits (`MIN_SECONDS_BETWEEN_LOGINS`,
   `MAX_LOGIN_ATTEMPTS_PER_SOCKET`) stop a single client monopolising it.

### Path resolution

All filesystem paths resolve from `paths.py` relative to the project root,
not the process working directory. This matters on Azure App Service (whose
`startup.sh` runs from `/home/site`, not `/home/site/wwwroot`) and for
systemd units without an explicit `WorkingDirectory=`. Set `DWR_STATE_DIR`
to a persistent volume if you want `game_state.json` to survive a restart --
on Azure App Service only `/home` is durable.

## Offline / air-gapped deployment (Krutrim Cloud, local server)

Two dependencies want internet access on first run:

- The CLIP (`RN50`, `openai` weights) image-similarity model
  (`services/ai_handling.py`), used to score round images. It is now
  **lazily loaded on first use** (not at import time), so the app and its
  `/health` endpoint come up immediately either way -- but the very first
  round of the very first game will fail if the weights can't be
  downloaded. Pre-download them into the machine's `open_clip`/HF cache
  before the event if the deployment target has no outbound internet.
- The NLTK `words` corpus, used for stricter prompt validation on longer
  prompts. The download is now bounded to 15 seconds and failure degrades
  gracefully (heuristic-only prompt validation) instead of blocking or
  crashing startup.

## Health check

`GET /health` returns `{"status": "ok"}` unauthenticated and immediately
(no dependency on game state or the AI services). Point your platform's
liveness/readiness probe (Azure App Service, Krutrim Cloud, or a local
reverse proxy) at this endpoint.

## Running the tests

```
pip install -r requirements.txt   # includes pytest / pytest-asyncio
pytest
```

156 tests run in under 30 seconds with no network access, no CLIP weights
and no NLTK corpus (see `tests/conftest.py` for the stubs). Coverage:

| File | Covers |
| --- | --- |
| `test_users.py` | password hashing and constant-time verification |
| `test_server.py` | login/logout, capacity, username normalisation, brute-force lockout and its expiry, reconnection, socket-drop propagation, admin token issue/expiry/revocation, leaderboard ranking |
| `test_team.py` | round scoring, idle-team-scores-zero, score clamping, per-turn attempt counters, socket resilience, image-generation failure, malformed queue input, grace periods, rotation |
| `test_ai_handling.py` | prompt classification, reference/generated image handling, URL encoding, retry policy, response size cap, score clamping, static path traversal |
| `test_game_loop.py` | full multi-round orchestration, failure containment, frame type consistency, checkpoint resume |
| `test_websocket_integration.py` | live app: login flow, reconnection, malformed payloads, frame-size and login-rate limits, admin token flow, checkpoint helpers |

Gameplay against the *real* image-generation API and the *real* CLIP model
is deliberately not exercised here -- each round runs for real wall-clock
minutes and calls a live third party. `test_game_loop.py` covers that flow
logically with `Team.run_round` stubbed; validate the real thing against a
staging deployment before an event.

## Future: database migration

`reader.py` contains a Supabase-based roster loader for a planned future
phase and is not currently imported by the running app (the import is
commented out in `game.py`). It's left in place, along with the `supabase`
dependency in `requirements.txt`, for that migration; it does not affect
the current in-memory-roster / JSON-checkpoint deployment.
