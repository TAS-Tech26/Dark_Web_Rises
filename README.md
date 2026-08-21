# Dark Web Rises - full stack

Both phases in the layout the tooling expects. Clone this and it runs. Clone
either phase on its own and it does not, because Phase 1's compose file
builds CTFd from a path outside its own directory.

    Dark_Web_Rises_Phase1/          the game: FastAPI + websockets + CLIP scoring
    DWR_Phase_2/Dark_Web_Rises/     the CTFd fork Phase 1 hands qualifiers to

That nesting is load bearing. `docker-compose.yml` resolves CTFd through:

    CTFD_SOURCE_DIR=../DWR_Phase_2/Dark_Web_Rises

Note the extra level and the capital P in `DWR_Phase_2`. Move either
directory and the CTFd build context breaks.

## Running it

    cd Dark_Web_Rises_Phase1
    cp .env.example .env          # then fill it in, see below
    docker compose build
    docker compose --profile dev up -d

The `dev` profile adds the Vite frontend with hot reload. Without it you get
the backend, CTFd, the database and the cache only.

## What is deliberately absent

Nothing here contains a secret, which also means nothing here runs until you
supply these:

| Setting | Where it comes from |
| --- | --- |
| `DEEPINFRA_API_KEY` | the DeepInfra dashboard |
| `SUPABASE_URL` / `SUPABASE_KEY` | the project holding the attendee roster |
| `CTFD_SECRET_KEY` | generate one; changing it invalidates every session |
| `CTFD_ACCESS_TOKEN` | CTFd admin panel, needed only for the final scoreboard |

The CTFd challenge export is outside this repository on purpose: it contains
the flags in plaintext. So is `.data/`, the live database volume.

## Before an event

`Dark_Web_Rises_Phase1/EVENT_RUNBOOK.md` has the full sequence. The three
that get forgotten:

1. `TOTAL_ROUNDS` back to 5 if a one-round test left it at 1. A one-round
   game scores out of 100, and `final_leaderboard.py --max-phase1` defaults
   to 500.
2. Delete `Dark_Web_Rises_Phase1/state/game_state.json`. A checkpoint from a
   finished game used to send the app straight to game over without playing
   anything. It now refuses to start and returns 409 instead.
3. Refresh the roster and confirm the count matches the event.
