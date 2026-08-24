# Event runbook — Dark Web Rises

Ordered. Steps 0–4 are prep; 5 onwards is the event itself.

Run every `docker compose` command from `Dark_Web_Rises_Phase1\`.

**Scale this is written for: 700 players, 175 teams of 4.**

---

## 0. Prove the providers before anything else

Do this days ahead, not the night before — every outcome here has a fix that
takes time (a model change, a quota increase, a credit top-up).

```powershell
python tools\provider_check.py                  # does one call parse?
python tools\loadtest_deepinfra.py --dry-run    # plan + cost, sends nothing
python tools\loadtest_deepinfra.py              # 700 images = one real round
python tools\loadtest_replicate.py              # 400 images, ramped
python tools\loadtest_compare.py                # prints the IMAGE_PROVIDERS to use
```

`provider_check.py` and the load tests answer different questions and you need
both. The first asks "does one response parse"; the load tests ask "does it
survive 175 requests arriving in the same second, four times in a row" — which
is the only traffic shape this event produces.

Put `loadtest_compare.py`'s recommended `IMAGE_PROVIDERS=` line into `.env`.
If it reports **NO VIABLE PRIMARY**, do not proceed to step 4 — read its
options and fix one of them first.

The harness is calibrated against a known distribution, so you can check the
instrument before trusting it:

```powershell
python tools\selftest_loadtest.py               # no network, no cost
```

---

## 1. Clear the test data out of CTFd

Challenges stay; accounts and solves go.

- **Admin → Users** — delete every test account (`bruh123`, `spiche`,
  `Team 1`…`Team 4`, and anything else that isn't your admin). Deleting a user
  removes its solves.
- **Admin → Submissions** — clear anything left over.
- **Admin → Scoreboard** — confirm it's empty.

Keep your own admin account.

> Don't use `docker compose down -v` for this. It wipes the challenges too and
> you'd have to re-import the export zip.

**Snapshot once it's clean:** Admin → Config → Backup → Export. That zip is
challenges-with-no-players, which is exactly what you want to restore from if
anything goes wrong later.

---

## 2. Reset Phase 1

```
del state\game_state.json
del state\phase1_scores.json
del state\roster_cache_phase2.csv
del ..\DWR_Phase_2\Dark_Web_Rises\CTFd\themes\core\static\leaderboard\scores.json
```

Leave the `state\` folder itself — it's a mount point.

A leftover `game_state.json` makes the app resume a finished game. The backend
now **refuses to start** in that case rather than silently jumping to game
over — you'll get a 409 on the dashboard naming the file — but deleting it
first is still the clean path.

The same refusal fires if `TOTAL_ROUNDS` changed since the checkpoint was
written, which is exactly what a one-round manual test leaves behind. If you
set `TOTAL_ROUNDS=1` to test, **set it back to 5 and delete the checkpoint**
before the event; a one-round game scores out of 100, not 500, and
`final_leaderboard.py --max-phase1` defaults to 500.

Also clear the generated images, or a rehearsal's 3500 files stay on the
volume alongside the real event's:

```
docker compose down
docker volume rm dark_web_rises_phase1_dwr-generated
```

---

## 3. Configuration

### The roster — do this first, it is the one that fails silently

The app falls back to `roster_cache.json` when Supabase is unreachable. That
fallback is a snapshot, and under Docker it is mounted **read-only**, so the
container can never refresh it. A stale cache boots perfectly cleanly and the
only symptom is that attendees missing from it cannot log in.

Refresh it **on the host** and check it:

```powershell
python tools\roster_build.py refresh
python tools\roster_build.py check --expect-teams 175 --expect-players 700
```

`check` must print *"Roster cache looks correct and current."* If it reports a
count mismatch, fix it now — not at 9am.

`EXPECTED_TEAMS` / `EXPECTED_PLAYERS` in `.env` make the app **refuse to
start** on a mismatch rather than booting short. Leave them set.

### CTFd (admin UI — no Docker command needed)

- **Config → Date & Time** — set **Start** and **End**. Without an End time
  `ctf_ended()` is always false, so the scoreboard never flips to the
  concluded panel and the leaderboard link never appears.
- **Config → User Mode** — confirm **Users** (switching later deletes accounts).
- **Config → Settings** — raise `incorrect_submissions_per_min` from 10 to
  ~40. It's per-account, and four teammates sharing one login share it.

### `.env` — set `PUBLIC_HOST` first

```
PUBLIC_HOST=192.168.1.50
```

One line. `docker-compose.yml` derives `CORS_ALLOWED_ORIGINS`, `VITE_API_BASE_URL`,
`VITE_CTFD_URL` and `PHASE2_CTFD_URL` from it, so the four-places-to-get-wrong
problem is now one place.

Get the address from `ipconfig` (IPv4 Address). It must not be `localhost` —
that means *the player's own phone* when their browser reads it.

### The rest of `.env` (then `docker compose up -d`)

- `IMAGE_PROVIDERS` — **the line `loadtest_compare.py` printed**, not a demo
  provider. `pollinations` or `huggingface` here means the event runs on a free
  service with no concurrency guarantee.
- `DWR_ADMINS` — off `eventadmin:change-this-password`
- `CTFD_SECRET_KEY` — off `change-me-before-the-event`
- `EXPECTED_TEAMS=175`, `EXPECTED_PLAYERS=700`
- `PHASE2_CTFD_URL` — the host's **LAN IP**, not localhost
- `FINAL_LEADERBOARD_URL` — same host, `/themes/core/static/leaderboard/index.html`
- `CORS_ALLOWED_ORIGINS` — every origin players load the frontend from

### `frontend-fusion\.env`

Nothing to do if you run the frontend through Docker — the compose service
passes `VITE_*` from `PUBLIC_HOST`, and Vite gives the process environment
precedence over that file.

Only edit it if you run `bun run dev` on the host by hand instead.

> Test from a phone on the same wifi before the event. That single check
> exercises `PUBLIC_HOST`, CORS, the websocket and the CTFd link at once, and
> it is the fastest way to find out you typed the wrong IP.

---

## 4. Start everything

```
docker compose --profile dev up -d
docker compose ps
```

**Note there is no `--build`.** That flag rebuilds *every* service that has a
build section — including CTFd, whose image takes ten minutes of pip downloads
on a slow connection and fails outright if any single wheel read times out.
Rebuild only what actually changed:

```
docker compose build dwr                    # after editing Phase 1 Python
docker compose --profile dev build frontend # after changing package.json
docker compose build ctfd                   # only after editing the CTFd fork
```

Which changes need which:

| you edited | what to do |
|---|---|
| `frontend-fusion/src/**` | nothing — bind-mounted, Vite hot-reloads |
| `.env` | `docker compose --profile dev up -d` (**not** `restart` — that reuses the old environment) |
| `state/`, `roster_cache.json` | nothing — bind-mounted |
| **Phase 1 Python** (`game.py`, `models/`, `services/`) | **`docker compose build dwr`** then `up -d` |
| `requirements.txt` | same, but slower — the dependency layer rebuilds too |

Phase 1's source is **copied into the image**, not mounted. Editing `game.py`
on the host changes nothing in the running container until you rebuild it.
Only `state/` and the roster are mounted, and those are data, not code.

`--profile dev` includes the frontend (Vite dev server on 5173). Without it
you get the backend, CTFd, MariaDB and Redis only, and you would run the
frontend yourself with `cd frontend-fusion` then `bun run dev`.

> The frontend service runs `vite dev`, which serves modules unbundled and
> compiled on demand — hundreds of requests per page load, plus a hot-reload
> websocket per client. That is fine for setup and rehearsal. Before event day,
> load it from a phone with 700 players' worth of scepticism, or build it
> properly and serve the output instead.

Wait for `dwr` to read `healthy` and give CTFd ~30 seconds.

Verify:

| | |
|---|---|
| Game backend | http://localhost:8000/health |
| Roster loaded | `docker compose logs dwr \| findstr "Server ready"` → **700 players / 175 teams** |
| Provider chain | `docker compose logs dwr \| findstr "provider chain"` → `deepinfra -> replicate` |
| No demo provider | `docker compose logs dwr \| findstr "DEMO PROVIDER"` → **nothing** |
| Round timing | `docker compose logs dwr \| findstr "Round timing"` → tells you how long the game runs |
| CTFd | http://localhost:8080 |
| Frontend | http://localhost:5173 (or `docker compose logs -f frontend`) |
| Frontend from a phone | `http://<PUBLIC_HOST>:5173` — the real test |

**Create the CTFd access token now** — Settings → Access Tokens. After all
imports are done, because an import replaces the token table. Keep it for
step 8.

Then check the CTFd side is actually ready:

```powershell
$env:CTFD_ADMIN_TOKEN = "ctfd_xxxxx"
python tools\ctfd_preflight.py
```

That checks the token, user mode, end time, submission rate limit and the
challenge point total — every one of which produces a wrong or missing final
scoreboard rather than an error you would notice.

---

## 5. Run Phase 1

Admin logs into the frontend and starts the game. At game-over the backend
writes, into `state\`:

- `roster_cache_phase2.csv` — qualified teams, `name,email,password`
- `phase1_scores.json` — their Phase 1 scores

At 175 teams, ~88 qualify.

> **Copy that CSV somewhere safe immediately.** It is the only plaintext copy
> of the team passwords — CTFd hashes them on import. Never move or delete it
> after importing, or the next game-over issues new passwords that no longer
> match the accounts.

**How long it takes:** `TIME_PER_ROUND` is the budget per *turn*, not per
round, so a round is up to 4 turns. The app logs the worst case at startup —
at the defaults it is around 31 minutes for 5 rounds. Turns end early when
players submit, so the real figure is lower.

---

## 6. Import the qualified teams

**Admin → Users → Import CSV** (Users, not Teams — you're in user mode).

Then confirm it actually took:

```powershell
python tools\ctfd_preflight.py --after-import --csv state\roster_cache_phase2.csv
```

A partial import is the failure mode here: it shows up at step 8 as "no CTFd
account" warnings, by which point the CSV may already have been moved.

Each qualified team logs in directly with the name and password from that file.
No registration. All four members can use it simultaneously.

---

## 7. Run the CTF

Players log in at the CTFd URL and solve challenges. When the End time passes,
the scoreboard automatically switches to "The CTF has concluded" with the
leaderboard button.

---

## 8. Publish the final leaderboard

```powershell
cd ..\DWR_Phase_2\Dark_Web_Rises
$env:CTFD_ADMIN_TOKEN = "ctfd_xxxxx"
py scripts\final_leaderboard.py
```

(PowerShell: `$env:`, not `set` — `set` is an alias for Set-Variable and won't
set an environment variable.)

Writes `scores.json` next to the leaderboard page. No Docker command — that
folder is bind-mounted. Refresh to see it.

Check the summary it prints:

- team count matches the number that qualified
- `CTF max` matches the total challenge points you expect
- no `no CTFd account` warnings — that means an import didn't take

Re-runnable any number of times; it overwrites, and the page cache-busts.

Scoring, if anyone asks:

```
combined = (phase1/max_phase1 + ctf/max_ctf) / 2 × 100
```

Each phase is worth 50 of the final 100 regardless of raw point totals.
`--max-phase1` defaults to 500 (5 rounds × 100); `max_ctf` is summed live from
the challenges.

---

## Rehearsal

Before the day, run the whole thing end to end without any of it being real:

```powershell
python tools\e2e_dryrun.py
```

175 teams, 700 simulated players over real websockets, through login, the
moderator's start, five rounds, game over, the phase 2 CSV, and the path
`final_leaderboard.py` reads. Image generation and CLIP scoring are stubbed —
the providers are covered separately by step 0, and stubbing them keeps this a
two-minute check rather than an hour and a bill.

It exits non-zero on any failure, so it can gate a deploy.

---

## If something breaks

`DOCKER_COMMANDS.md` has the diagnostic sequence. The most likely:

- **Page won't load right after `up -d`** — CTFd takes 15–30s to bind its port.
- **`.env` change seems ignored** — `restart` reuses the old environment; use
  `docker compose up -d`.
- **Wrong compose file** — if `docker compose ps` shows `permissions` or
  `nginx`, you're in the CTFd folder instead of `Dark_Web_Rises_Phase1\`.
- **`dwr` refuses to start with ROSTER SIZE MISMATCH** — working as intended.
  The roster is short. Run `tools\roster_build.py refresh` then `check`.
- **Images stop appearing mid-game** — check the admin dashboard's provider
  panel. A breaker showing `open` means that provider is being skipped and
  traffic has moved down the chain. `IMAGE_PROVIDER_OVERRIDE=<name>` in `.env`
  pins all traffic to one provider without a redeploy.
- **`No phase 1 scores at ...` at step 8** — Phase 1 has not reached game over,
  or you are pointing at the wrong event. The script now searches the likely
  locations and tells you the exact `--phase1-scores` line to re-run with.
