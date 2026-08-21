# Docker command reference — Dark Web Rises

Run everything from `Dark_Web_Rises_Phase1\`. Services: `dwr` (game backend),
`ctfd` (CTF), `db` + `cache` (CTFd's MariaDB and Redis).

---

## Daily start / stop

| Goal | Command |
|---|---|
| Start everything | `docker compose up -d` |
| Start one service | `docker compose up -d dwr` |
| Stop everything, keep data | `docker compose stop` |
| Stop and remove containers, keep data | `docker compose down` |
| See what's running | `docker compose ps -a` |

`down` is safe — challenges, accounts and scores live in named volumes, not in
the containers.

> **Never run `docker compose down -v`.** The `-v` deletes the volumes, which
> means every CTFd challenge, account and solve. There is no undo.

---

## After changing something

**This is the one that catches people out:** `restart` reuses the container's
original environment. Config changes need the container *recreated*.

| What you changed | Command |
|---|---|
| `.env` (any variable) | `docker compose up -d dwr` |
| `docker-compose.yml` | `docker compose up -d` |
| Python code, `requirements.txt`, `Dockerfile` | `docker compose up -d --build dwr` |
| CTFd Python or templates (e.g. `scoreboard.html`) | `docker compose up -d --build ctfd` |
| Nothing — just want a clean restart | `docker compose restart dwr` |

Confirm a variable actually reached the container:

```
docker compose exec dwr printenv ROSTER_SOURCE
```

### Changes made in the CTFd admin panel need no Docker command

Challenges, flags, hints, users, visibility, start/end times — all of it lives
in MariaDB, not in files. It takes effect the moment you save and survives
`restart`, `up -d`, rebuilds and `docker compose down`, because it's in the
`ctfd-db` volume rather than the container.

Only two things destroy it: `docker compose down -v`, and importing an export
zip (which replaces the database wholesale).

**Snapshot it before anything risky.** Admin → Config → Backup → Export gives
you a zip of the whole instance. Worth doing once you've finished authoring
challenges, and again right before the event.

### Challenge pages, without a full rebuild

The CTFd build context is ~158 MB of videos, so rebuilding per edit is slow.
Copy just the changed folder in:

```
docker compose cp ..\DWR_Phase_2\Dark_Web_Rises\CTFd\themes\core\static\challenge_3 ctfd:/opt/CTFd/CTFd/themes/core/static/
```

Then hard-refresh the browser (**Ctrl+F5** — a normal refresh serves the cached
copy). This survives `restart` but not a rebuild or `--force-recreate`, so do
one proper `docker compose up -d --build ctfd` once the pages settle.

---

## When something is broken

Work down this list; it's roughly most to least common.

**1. Is Docker Desktop running?**

```
docker ps
```

A named-pipe error (`dockerDesktopLinuxEngine ... cannot find the file
specified`) means the engine is down. Launch Docker Desktop and wait for the
tray whale to stop animating.

**2. Did you wait long enough?**

CTFd takes 15–30 seconds to answer on 8080: it waits for MariaDB, runs alembic
migrations, loads plugins, *then* binds the port. `up -d` returns immediately,
long before any of that finishes. A connection refused right after `up -d` is
usually just impatience.

**3. Read the logs.**

```
docker compose logs dwr --tail=50
docker compose logs ctfd --tail=50
docker compose logs -f dwr          # follow live
```

**4. Is the container actually up?**

```
docker compose ps -a
```

`-a` also shows exited containers, which plain `ps` hides. If one service died
while you were only restarting another, `docker compose up -d <service>` brings
it back.

**5. Shell inside a container.**

```
docker compose exec dwr sh
docker compose exec ctfd sh
```

---

## Health checks

| Check | How |
|---|---|
| Backend alive | http://localhost:8000/health → `{"status":"ok"}` |
| Backend roster loaded | `docker compose logs dwr \| findstr "Server ready"` → `600 players across 150 teams` |
| CTFd alive | http://localhost:8080 |
| Frontend | http://localhost:5173 (not Docker — `bun run dev` in `frontend-fusion\`) |

`dwr` showing `healthy` in `docker compose ps` is meaningful on its own: the app
refuses to start on a bad roster, so healthy means the roster loaded.

---

## Between test games

The checkpoint persists now, so a finished game is *resumed* — which looks like
"phase 1 is already over" the moment you start.

```
del state\game_state.json
docker compose restart dwr
```

Leave `state\` itself in place; it's a mount point.

> Once `state\roster_cache_phase2.csv` has been imported into CTFd, **do not
> move or delete it.** It is the only plaintext copy of the team passwords —
> CTFd stores them hashed. Deleting it makes the next game-over issue fresh
> passwords that no longer match the imported accounts.

---

## Full reset (deliberately destructive)

Only when you want to wipe CTFd completely and start from the export zip again:

```
docker compose down -v
docker compose up -d --build
```

This destroys every challenge, account and solve in CTFd. You'd then re-import
the export zip and re-import the qualified-team CSV.

---

## Disk space

The `dwr` image is ~3 GB (CPU torch + CLIP weights) and CTFd adds another ~1 GB.
Old images accumulate across rebuilds.

```
docker system df                 # what's using space
docker image prune               # remove dangling images — safe
docker system prune -a           # remove ALL unused images — next build is slow
```

`docker system prune -a` never touches volumes, so your CTFd data is safe, but
the next `--build` re-downloads and recompiles everything.

---

## Before the event

- `docker compose up -d --build` — make sure everything builds clean from scratch
- Confirm nothing depends on a path only on your laptop
- Change `DWR_ADMINS` and `CTFD_SECRET_KEY` off their placeholder values
- Set `PHASE2_CTFD_URL` and `VITE_API_BASE_URL` to the host's LAN IP, not `localhost`
- `del state\game_state.json` so the event starts fresh
- Build the frontend properly rather than running `bun run dev`
