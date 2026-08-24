# Running the event stack with Docker

This gets both halves of the event running side by side on one machine:

| Service | What it is | URL |
|---|---|---|
| `dwr` | Phase 1 — the Dark Web Rises game backend | http://localhost:8000 |
| `ctfd` | Phase 2 — CTFd, running the challenges | http://localhost:8080 |
| `db`, `cache` | MariaDB + Redis, used by CTFd only | not exposed |

The challenge pages are served by CTFd itself, e.g.
http://localhost:8080/themes/core/static/challenge_1/vid_player.html

## Folder layout

Phase 2 is a separate checkout, sitting beside this one:

```
Documents/Dark_Web_Rises/
  Dark_Web_Rises_Phase1/          <- this repo; run all docker commands from here
    docker-compose.yml
  DWR_Phase_2/
    Dark_Web_Rises/               <- the actual CTFd fork (note the nesting)
      CTFd/themes/core/static/challenge_1/ ...
```

Mind the extra level: `DWR_Phase_2/` contains a folder *also* called
`Dark_Web_Rises/`, and that inner folder is the CTFd checkout. `CTFD_SOURCE_DIR`
in `.env` must point at the inner one.

**`docker-compose.yml` here is the single entry point for the whole stack.**
His fork ships its own `docker-compose.yml` — ignore it. Running both starts two
CTFd instances competing for ports and separate databases, and the challenges you
import will appear in whichever one you weren't looking at.

Everything comes up with one command, run from `Dark_Web_Rises_Phase1/`:

```bash
docker compose up -d --build
```

## First run

```bash
cp .env.example .env      # then fill in real values — see below
docker compose up -d --build
```

The first build takes roughly 5–10 minutes and produces a ~3 GB image. Most of
that is CPU-only torch plus the CLIP RN50 weights, which are deliberately baked
into the image rather than downloaded on first use — see "Why the image is big".

Watch it come up:

```bash
docker compose logs -f dwr
```

Then open http://localhost:8080 and complete CTFd's setup wizard once. Its
answers persist in the `ctfd-db` volume, so you only do this on a fresh stack.

## What must be set in `.env`

`.env` is gitignored and never enters the image. Copy `.env.example` and set at
minimum:

- **`CORS_ALLOWED_ORIGINS`** — comma-separated, every origin the frontend is
  served from. Required for anything that isn't localhost; without it the
  browser silently blocks the frontend from reaching the API. If CTFd links out
  to the game, CTFd's origin belongs in this list too.
- **`IMAGE_PROVIDERS`** and whichever provider key it selects
  (`DEEPINFRA_API_KEY`, `REPLICATE_API_TOKEN`, …).
- **`CTFD_SECRET_KEY`** — signs CTFd sessions. Change it before the event.
  Rotating it logs everyone out.

## The one rule that will bite you

**The `dwr` service runs exactly one process and cannot be scaled horizontally.**

Game state — `GameServer`, teams, connected websockets, scores, the admin
session-token table — lives entirely in one Python process's memory. Two
instances means players land on different processes that cannot see each other:
a teammate connected to instance A won't appear as connected on instance B.

So: no `--workers 2`, no `deploy.replicas: 2`, no putting two containers behind
a round-robin load balancer. If it's too slow, give the one container more CPU
(the `deploy.resources.limits` block in `docker-compose.yml`). Round-end CLIP
scoring is the bottleneck, not the websockets — with ~300 teams finishing
simultaneously each scoring call is two RN50 image encodes.

## Where Phase 1 and Phase 2 meet

The two services share the `event` network, so inside Docker they reach each
other by service name — `http://dwr:8000` and `http://ctfd:8000` — regardless of
the host port mapping. Use those names, not `localhost`, in any server-side
integration.

### The qualification handoff (Phase 1 → Phase 2)

This part is already built. At game over, `final_score.py` takes the top 50% of
teams by cumulative score (ties at the cutoff all advance) and writes two files
into `state/`:

| File | Contents |
|---|---|
| `roster_cache_phase2.csv` | `name,email,password` — one row per qualified team |
| `phase1_scores.json` | `team_id → phase 1 score`, for carrying points over |

The game server also returns each team its own password, so it can be shown on
that team's results screen.

The moderator then imports the CSV at **Admin → Teams → Import CSV** in CTFd.
CTFd's `load_teams_csv` feeds rows straight into `TeamSchema`, so `name`,
`email`, `password` are exactly the right columns. Emails are
`team<id>@example.invalid` — never delivered, just the unique column CTFd
requires; `.invalid` is reserved by RFC 2606 so nothing can escape to a real
domain.

Because the CTF runs in **team mode**, players then register their own user
accounts and use **Join Team** with the team name and that password. So the one
credential shown at the end of Phase 1 is all a team needs.

> **The CSV is the only plaintext copy of those passwords.** CTFd bcrypt-hashes
> them on import, and the game hands them out once. Lose the file after import
> and every qualified team is locked out with no recovery path. This is why
> `DWR_STATE_DIR` points at a bind-mounted `./state` rather than the container's
> own filesystem — otherwise `docker compose down` would destroy it. Copy it
> somewhere safe as soon as it appears.

Re-running is safe: `build_phase2_roster` reads back any existing CSV and keeps
the passwords already issued, so a restart or a second game-over won't
invalidate accounts already imported into CTFd.

### The final combined leaderboard

`DWR_Phase_2/Dark_Web_Rises/scripts/final_leaderboard.py` consumes
`phase1_scores.json` and CTFd's API, normalises each phase against its own
maximum, and averages them onto 0..100:

```
combined = (phase1/max_phase1 + ctf/max_ctf) / 2 * 100
```

Run it after the CTF closes:

```
set CTFD_ADMIN_TOKEN=ctfd_xxxxx
cd ..\DWR_Phase_2\Dark_Web_Rises
python scripts\final_leaderboard.py
```

Token comes from CTFd **Settings → Access Tokens**. Standard library only, so
no `pip install`.

Teams are joined across the two systems on the `team<id>@example.invalid`
address that `final_score.py` generates, read from `/api/v1/teams`. Not on team
name — names are free text, teams can rename themselves, and the scoreboard
endpoint doesn't expose email at all. The CTF maximum is summed live from
`/api/v1/challenges` rather than hardcoded, since challenge values get edited
right up to the event.

Still manual: **clear test data before importing.** CTFd team names must be
unique, so leftovers from testing will collide.

### Other seams worth deciding on early:

1. **Identity.** The game roster is hardcoded in `game.py` (`player_data` /
   `admin_data`) and hashed into memory at boot. CTFd has its own user and team
   tables. Nothing reconciles them today. Either agree on a shared username
   convention so scores can be matched up afterwards, or pick one system as the
   source of truth.
2. **Scoring.** If CTF solves should feed the game score (or vice versa), that
   crosses the process boundary — the game's scores are in memory, CTFd's are in
   MariaDB. Simplest workable version is CTFd calling an endpoint on `dwr` via a
   plugin; there is no such endpoint yet.
3. **Custom CTFd code.** If Phase 2 ships plugins or a theme, uncomment the
   plugin/theme volume mounts in `docker-compose.yml` and commit them under
   `ctfd/` so they travel with the repo.

## Sending the stack between the two of you

Two different kinds of thing travel two different ways. This trips people up,
so it's worth being explicit.

### Code — travels by git

`Dockerfile`, `docker-compose.yml`, `.dockerignore`, the game code, and any CTFd
plugins committed under `ctfd/`. Whoever pulls runs:

```bash
git pull
docker compose up -d --build
```

That rebuilds the game image from source, so you both get the same thing.

**Do not zip the project folder and send it.** It contains `.env`, a `.venv/`
built for the wrong OS, `roster_cache.json` with attendee credentials in plain
text, and `frontend-fusion/node_modules`. Git already excludes all of it.

### Challenges — do NOT travel by git

CTF challenges, flags, uploaded files, users, teams and solves live in MariaDB
and the uploads volume — inside Docker, not in the repo. Nothing in a `git pull`
or a `docker compose build` moves them.

Use CTFd's own export instead. On his machine:

**Admin panel → Config → Backup → Export** — produces a single `.zip` of the
whole instance. He sends you that file.

On yours:

**Admin panel → Config → Backup → Import** — upload the zip.

> **Import wipes the target instance completely.** Everything currently in your
> CTFd is replaced by the contents of the zip. Export your own copy first if
> there's anything in there you want.

**Keep export zips out of git.** They contain every flag in plaintext, plus
whatever accounts existed when the export was taken. Current one is parked at
`Dark_Web_Rises/ctf-export/`, one level up from this repo, so neither checkout
can pick it up.

**Pull his latest commits before importing.** The export carries challenge
*entries*; the pages they link to arrive by `git pull`. Import a zip listing
challenge 6 into a checkout that predates challenge 6's HTML and the challenge
appears in the UI but its link 404s.

**Clear his test data after importing.** An export taken from a development
instance carries its accounts, solves and submissions along with the challenges,
and those land on your scoreboard. Check Admin → Users, Teams, and Submissions
and delete anything left over from testing before the event.

### The challenge pages, and why CTFd is built rather than pulled

Phase 2 is a **fork of CTFd 3.8.6**, not stock CTFd. Three commits sit on top of
upstream:

- `CTFd/themes/core/static/challenge_{1,2,4,5}/` — the challenge pages, plus
  ~158 MB of videos and images
- `CTFd/themes/core/templates/scoreboard.html` — a modified CTFd template that
  shows the final scoreboard after the game ends
- a hardcoded `SECRET_KEY` in his `docker-compose.yml`

Because the pages sit under a theme's `static/` directory, CTFd serves them
directly — no separate web server is needed, and there is deliberately none in
this compose file:

```
http://<host>:8080/themes/core/static/challenge_1/vid_player.html
```

The modified `scoreboard.html` is the reason the `ctfd` service uses `build:`
instead of `image: ctfd/ctfd:3.8.6`. That published image is stock upstream; pull
it and you get a CTFd with the wrong scoreboard and 404s on every challenge link.
The fork has to be built from his source tree.

So the split is:

- **CTFd fork, challenge pages, scoreboard** → his git repo, cloned at
  `DWR_Phase_2/Dark_Web_Rises/`
- **challenge entries** (title, description, flag, points) → CTFd export zip
- **Phase 1 backend and this stack** → this repo

The pages are the one part *not* covered by the export zip; the flags are the one
part *not* covered by git. You need both.

### Iterating on challenge pages without rebuilding

A rebuild copies a ~158 MB build context, which gets tedious fast. There's a
commented-out bind mount on the `ctfd` service that overlays his source at
`/opt/CTFd`. This is safe because the image keeps its virtualenv at `/opt/venv`,
outside the mount point. Uncomment it and `docker compose restart ctfd` picks up
page edits with no rebuild.

Rebuild properly before the event so the image is self-contained.

**Watch the URLs in the challenge descriptions.** If he authored them pointing
at a path inside his CTFd install, or at `file:///`, they will not work on your
machine or on any player's device. They need to point at the `challenges`
service on a host players can actually reach — see
`ctfd/challenge-pages/README.md`. Fix these *before* he exports, so the corrected
links come across in the zip.

### Making "the exact version he ran" actually true

Three things have to match, and only one of them is handled by git:

1. **CTFd version** — you build his fork, so you get exactly his CTFd (3.8.6 plus
   his commits) as long as you're on the same commit. `git -C
   ../DWR_Phase_2/Dark_Web_Rises log --oneline -1` on both machines should match.
   This matters beyond cosmetics: a CTFd export will not import into an *older*
   CTFd than the one it came from.
2. **The game image** — rebuilt from the committed `Dockerfile`, so it follows
   the code. `requirements.txt` is fully pinned already.
3. **Config** — `.env` is deliberately not in git. If a setting matters for the
   stack to work rather than being a secret, add it to `.env.example` with a
   comment so it actually reaches the other person.

### Quick sanity check after importing

```bash
docker compose ps                 # all four services up
docker compose logs --tail=50 ctfd
```

Then log into CTFd and confirm the challenge count and file attachments look
right. Attachments are the usual casualty of a bad export — check that a
challenge with a downloadable file actually serves it.

## Before the event

- Replace the placeholder roster in `game.py` with the real one.
- Delete any stale `game_state.json`. The app logs a loud warning if one exists
  at startup, precisely so an old checkpoint doesn't make a brand-new game
  "resume" with previous scores and skip to game over.
- Pin `ctfd/ctfd:latest` to a specific version tag so both machines run the
  same build.

## Why the image is big

CPU-only torch, torchvision, and the CLIP RN50 weights dominate. The Dockerfile
pre-fetches the RN50 weights and the nltk `words` corpus at build time on
purpose: both are otherwise fetched lazily on first use, and `_load_nltk_words()`
has a hard 15-second timeout that a slow venue network will blow through — mid
event, during the first round.

Note the model is `RN50-quickgelu`, not plain `RN50`. OpenAI's weights were
trained with QuickGELU; loading them under the plain `RN50` entry uses the wrong
activation and produces quietly wrong similarity scores rather than an error.

## What is deliberately not in the image

`.dockerignore` keeps these out: `.env`, `roster_cache.json` (plaintext attendee
credentials), `game_state.json`, `.venv/`, and `static/generated/`. Generated
round images live in the `dwr-generated` volume instead, so they survive a
restart without ending up in a shared image layer.

## Common problems

**`docker compose up` fails with "env file .env not found"** — you skipped
`cp .env.example .env`.

**Frontend loads but the websocket won't connect** — almost always
`CORS_ALLOWED_ORIGINS` missing the origin the frontend is actually served from.

**Port 8000 already in use** — something else on the host has it; change the
left-hand side of `"8000:8000"`.

**CTFd shows a database error on first boot** — MariaDB is still initialising.
`docker compose restart ctfd` after a few seconds.
