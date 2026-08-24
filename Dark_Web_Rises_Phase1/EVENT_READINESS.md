# Dark Web Rises — event readiness review

Target scale: **700 players, 175 teams of 4.** Reviewed against the code as it
stands in `Dark_Web_Rises_Phase1/` and `DWR_Phase_2/Dark_Web_Rises/`.

---

## The short version

The codebase is in good shape. The game logic, failover design and websocket
handling are well reasoned and hold up at 175 teams — I ran the whole event end
to end in one process (700 simulated players over real websockets, 3500 image
generations, 5 rounds, game over, phase 2 handoff) and every check passed once
the issues below were fixed.

Three things stand between here and a working event day, in order of severity:

1. **No provider credentials.** `DEEPINFRA_API_KEY` and `REPLICATE_API_TOKEN`
   are both empty in `.env`, and `IMAGE_PROVIDERS=pollinations`. The event is
   currently configured to run on a free public service with no concurrency
   guarantee. Nothing else on this list matters until this is fixed, and the
   load tests you asked for cannot run without it.
2. **The roster is 150 teams / 600 players, not 175 / 700.** Supabase is not
   configured (`SUPABASE_URL=https://your-project.supabase.co`), so the app
   boots from `roster_cache.json`, which holds 150 teams. 100 attendees would
   have no account.
3. **`final_leaderboard.py` could not find its input file** when run the way
   the runbook says to run it. This is fixed. Details below.

---

## Blocking prerequisites — nothing works without these

These are configuration, not code. I have deliberately **not** edited your live
`.env`, because changing `IMAGE_PROVIDERS` away from `pollinations` while the
real keys are empty would leave you with no working provider at all.

| Setting | Now | Needs to be |
|---|---|---|
| `DEEPINFRA_API_KEY` | empty | a funded key |
| `REPLICATE_API_TOKEN` | empty | a funded token |
| `IMAGE_PROVIDERS` | `pollinations` | `deepinfra,replicate` (or whatever `loadtest_compare.py` recommends) |
| `SUPABASE_URL` | `https://your-project.supabase.co` | the real project URL |
| `SUPABASE_KEY` | `your-publishable-key` | the real key |
| `DWR_ADMINS` | `eventadmin:change-this-password` | real credentials |
| `CTFD_SECRET_KEY` | `change-me-before-the-event` | a real secret |
| `PHASE2_CTFD_URL` | `http://localhost:8080` | the host's LAN IP |

`localhost` on an attendee's phone means *their* phone. That one catches
everyone at least once.

---

## What I found and fixed

### 1. `final_leaderboard.py` looked for its input outside the project — BLOCKING

`DEFAULT_PHASE1_SCORES` was a bare relative path resolved against the *current
directory*. The runbook tells you to run it from
`DWR_Phase_2\Dark_Web_Rises\`, and from there the three `..` in that path land
one level above the repo root:

```
runbook says:   cd ..\DWR_Phase_2\Dark_Web_Rises ; py scripts\final_leaderboard.py
looked for:     C:\Users\sudha\Documents\Dark_Web_Rises_Phase1\state\phase1_scores.json   <- does not exist
actually at:    C:\Users\sudha\Documents\Dark_Web_Rises\Dark_Web_Rises_Phase1\state\phase1_scores.json
```

The number of `..` is right for anchoring to the *script's* directory, which is
almost certainly what was intended — `DEFAULT_SCORES_OUT` two lines below does
exactly that. It just was not applied here.

This fails at runbook step 8, with the room waiting for the final scoreboard,
and it fails as `No phase 1 scores at ...` — which reads like Phase 1 did not
write the file, sending you to look in the wrong place entirely.

**Fixed:** anchored to `__file__`, so it resolves correctly from any working
directory. I also added a fallback that searches the likely locations and, if
it finds the file elsewhere, prints the exact `--phase1-scores` line to re-run
with rather than just failing.

Verified before and after from the documented working directory and from an
unrelated one.

### 2. A stale roster boots silently — HIGH

`roster.py` falls back to the local cache when Supabase is unreachable. That is
the right design; an outage on the morning must not stop the app booting.

But two things combine badly:

- the cache is a snapshot, and
- `docker-compose.yml` mounts it **read-only**, so the app cannot refresh it
  even when Supabase *is* reachable (`write_cache` fails with EROFS, logs an
  error, carries on).

So the fallback ages, and nothing surfaces it. With today's 150-team cache and
175 teams in the room, the app starts perfectly: `Server ready: 600 players
across 150 teams`, health check green, no error anywhere. The only symptom is
100 people who cannot log in, and you learn about it from the queue.

**Fixed, three ways:**

- `game.py` now compares the loaded roster against `EXPECTED_TEAMS` /
  `EXPECTED_PLAYERS` and refuses to start on a mismatch, with a message that
  says which source the roster came from and what to do about it.
  `ROSTER_SIZE_STRICT=false` downgrades it to an error if you ever need to run
  short deliberately.
- `tools/roster_build.py` refreshes the cache **from the host**, where the file
  is writable, and validates it (`check` catches duplicate usernames, team-id
  gaps, oversized teams, cache age, and count mismatches — all of which either
  crash `GameServer` at construction or silently produce unplayable teams).
- The compose file and `.env.example` document the read-only consequence
  explicitly.

The guard defaults to **off**. I chose that deliberately: defaulting it to 175
would have made your stack stop booting today, on a change you did not ask
for. Turn it on in `.env` once the roster is final — runbook step 3.

### 3. A cancelled request could disable a provider permanently — MEDIUM

In `CircuitBreaker`, a `HALF_OPEN` probe claims `_probe_in_flight = True`.
Both `record_success()` and `record_failure()` clear it — but neither runs if
the probing coroutine is **cancelled**, because `CancelledError` derives from
`BaseException`, not `Exception`, and passes straight through
`ProviderChain.generate`'s handlers.

The flag then stays set with the state stuck at `HALF_OPEN`, and because the
cooldown check only runs in the `OPEN` branch, nothing ever reconsiders it. The
provider is skipped for the rest of the event, with no failure recorded
anywhere to explain why — the admin dashboard would show it sitting in
`half_open` with a clean failure count.

Nothing in the current code path cancels a generation mid-flight, so this is
latent rather than active. I fixed it anyway because the cost is two lines and
the failure mode is "your primary provider silently stops being used".

**Fixed:** `ProviderChain.generate` now catches `CancelledError`, releases the
probe without recording an outcome, and re-raises. Independently, a probe that
has been outstanding longer than 3× the request timeout is treated as lost, so
the breaker self-heals regardless of cause.

### 4. `TURN_TIMEOUT` does nothing — MEDIUM (a config trap)

`TURN_TIMEOUT` is read in `game.py`, passed through `start_games` →
`_play_all_rounds` → `Team.run_round`, and then never used. `Team._run_round`
bounds a turn with `TIME_PER_ROUND` alone.

Anyone tuning "how long a player gets" will reach for the variable called
`TURN_TIMEOUT`, set it, see no change, and conclude the config is not being
read at all.

There is a second-order problem: `TIME_PER_ROUND` is, despite its name, the
per-**turn** budget. A round is `MAX_MEMBERS_PER_TEAM` turns. At the defaults
that is 90s × 4 = up to 360s per round, and **up to ~31 minutes for five
rounds** — not the ~8 minutes the variable names suggest. That is a scheduling
number worth knowing before the day.

**Fixed:** `game.py` warns if `TURN_TIMEOUT` is set (explaining which variable
to use instead) and logs the derived worst-case round and game duration at
startup, so the schedule comes from arithmetic rather than from an assumption.
I left the parameter in the signatures rather than removing it, to avoid
breaking anything that calls those functions.

### 5. CTFd runs one gunicorn worker for ~350 concurrent users — MEDIUM

At 175 teams, ~88 qualify, and each is a shared login used by 4 people, so
Phase 2 is roughly 350 concurrent users on `WORKERS=1`.

The gevent worker class means this will not fall over — but greenlets give
concurrency, not parallelism. Every flag comparison, template render and
scoreboard query for all 350 users serialises onto one CPU core. The symptom is
not an error; it is the CTF feeling slow at exactly the moment everyone is
submitting.

**Fixed:** `WORKERS=${CTFD_WORKERS:-4}`. Multiple workers need `SECRET_KEY`
(the entrypoint refuses to start `WORKERS>1` without it) and `REDIS_URL` for
shared sessions and cache — this stack already sets both, so it is safe. Without
Redis, workers would each hold their own cache and users would see inconsistent
scoreboards depending on which worker answered.

I also gave `db` and `cache` healthchecks and switched CTFd's `depends_on` to
`condition: service_healthy`. A bare `depends_on` only waits for the container
to be created, not for MariaDB to finish initialising, so `flask db upgrade`
was racing it.

### 6. Smaller things

- **Generated images accumulate.** 3500 per event, never deleted, on a named
  volume that survives `docker compose down`. Rehearsals pile up alongside the
  real event. Documented in the compose file and runbook step 2, with the
  `docker volume rm` line.
- **Stale state files.** `state/game_state.json` currently holds a completed
  5-round game for one team. Left in place, a new event resumes from it and
  jumps straight to game over. Runbook step 2 already covers this — worth not
  skipping.
- **Comments referenced 150 and 300 teams.** Updated to 175 throughout
  `services/providers.py` and the compose file's capacity reasoning, since
  stale numbers in comments about capacity are exactly the kind of thing that
  gets trusted later.
- **Only 5 reference images** in `static/images/`. Not a bug — at 175 teams
  about 35 share each reference per round, and the CLIP embedding cache makes
  that cheap rather than expensive. Worth knowing it is deliberate.

---

## What I did not change

- **`.env`** — it holds your real secrets, and switching `IMAGE_PROVIDERS` off
  `pollinations` with empty provider keys would leave you with nothing working.
  The table at the top is the change list.
- **`roster_cache.json`** — it holds real attendee names and passwords from
  Supabase. Overwriting it with a synthetic 175-team roster would destroy real
  data. Use `tools/roster_build.py refresh` once Supabase is configured, or
  `tools/roster_build.py synth` for a throwaway test roster under a different
  filename.
- **The round-timing semantics.** Renaming `TIME_PER_ROUND` to `TIME_PER_TURN`
  would be clearer, but would silently ignore every existing `.env` that sets
  the old name. Logging the derived numbers achieves the same thing without
  that risk.

---

## The image-provider load tests

`tools/provider_check.py` already answers "does one response parse?". What was
missing is "does it survive the traffic this event actually makes?" — and those
are very different questions.

The traffic shape matters more than the volume. Rounds are synchronised, so
every team requests an image in the same moment:

```
175 teams × 4 turns = 700 images per round, arriving as 4 bursts of 175
× 5 rounds          = 3500 images per event
```

700 is therefore not an arbitrary number. It is exactly one round, and it is
the smallest sample containing a full four-burst sequence — enough to show
whether a provider *degrades across consecutive bursts* (rate-limit buckets
draining, queue depth accumulating, autoscaler lagging) rather than just how it
handles the first one. Sending 700 requests as a steady 40-concurrent trickle
would also produce "700 images" and would tell you almost nothing.

### `tools/loadtest_deepinfra.py` — 700 images

```powershell
python tools\loadtest_deepinfra.py --dry-run --cost-per-image 0.0009   # plan + bill, sends nothing
python tools\loadtest_deepinfra.py
```

Four bursts of 175, 15 seconds apart. DeepInfra is the configured primary, so
it is held to the higher bar (98% effective success rate): every request it
misses becomes load on Replicate that Replicate was never sized for.

### `tools/loadtest_replicate.py` — 400 images

```powershell
python tools\loadtest_replicate.py
```

Shaped differently on purpose. Replicate only ever sees failover traffic, so
"can it do 175 at once" is the wrong question for it. A ramp — 100 requests
each at 25, 50, 100 and 175 concurrent — answers the right one: *at what
concurrency does it stop coping?*

The last step decides the failover story. If Replicate holds at 175, a total
DeepInfra outage is survivable. If it collapses at 100, then a primary outage
is an event-level incident and the mitigation has to be something other than
"the chain will handle it" — which is much better to know in advance than to
discover mid-round.

It is also judged against the **8-second fallback budget**, not the primary's
20s. A provider can be a fine primary and a poor secondary; 12 seconds is
comfortable in a 20s budget and useless in an 8s one.

### The measurement decision worth knowing about

Both runs use a **120-second timeout, not the production 20s**.

If the harness used 20s, every slow request would be recorded as a bare
"timeout" and the latency distribution above 20s would be invisible — you would
learn that requests failed, but not whether they failed at 21 seconds or at 300.
Those imply completely different fixes.

So requests run to a long timeout, the true latency is measured, and the report
then derives what the production budget *would* have done to that distribution.
The reports carry both numbers:

```
succeeded              688   (98.3%)
succeeded within 20s   641   (91.6%)  <- what the live game would have got
                        47   succeeded here but would be killed by
                             IMAGE_GEN_TIMEOUT_SECONDS and failed over
```

That second line is the one to act on.

### `tools/loadtest_compare.py`

Reads both runs and prints the `IMAGE_PROVIDERS=` line to put in `.env`, scoring
each provider in both seats against the budget it would run under there. If it
reports **NO VIABLE PRIMARY** it lists the options in order of what they cost
you.

### `tools/selftest_loadtest.py`

A load test is a measuring instrument, and an uncalibrated instrument is worse
than none — it produces confident numbers that are wrong and you act on them.
This runs the harness against a synthetic provider whose latency and failure mix
are known exactly, then checks the report matches. It catches the specific ways
this could lie: concurrency not actually reached because the connection pool is
too small (so "175 concurrent" is really 10 and you measured a queue); latency
timed around the semaphore wait rather than the request; failures swallowed.

No network, no cost. Run it once before trusting a real run.

---

## Also new

### `tools/e2e_dryrun.py`

The whole event in one process: 175 teams, 700 simulated players over real
websockets, login, the moderator's start, five rounds, game over, the phase 2
CSV, and the path `final_leaderboard.py` reads. 34 checks, exits non-zero on
any failure.

Image generation and CLIP scoring are stubbed. That is deliberate, not a
shortcut — the providers are covered properly by the load tests, and sending
3500 real requests here would cost money to exercise code paths that have
nothing to do with the provider. Everything else is the shipping code.

It specifically covers the hand-offs between systems that are developed and
tested separately, which is where the failures have actually been: the CSV
shape CTFd's import requires, unique names and emails (CTFd aborts a bulk
import partway through on a duplicate, leaving some teams imported and some
not), the qualification cut, whether the carried scores are cumulative or a
single round, and the scores-file path.

Takes about two minutes at full scale with compressed timings; `--realtime`
uses production ones.

### `tools/ctfd_preflight.py`

The CTFd half, which the dry run cannot reach. Checks the things that produce a
wrong or missing final scoreboard rather than an error you would notice:

- the admin token still works (an access token created *before* a CSV import is
  destroyed by that import — CTFd replaces the token table, and you get a 403
  at step 8 and assume the token is wrong rather than gone)
- user mode is Users, not Teams (in Teams mode the import lands where
  `final_leaderboard.py` does not look, and it produces a leaderboard of zeros
  with no error at all)
- an end time is set (without one `ctf_ended()` is never true and the
  leaderboard button never appears)
- `incorrect_submissions_per_min` is raised — it is per-account, and four
  teammates share one login, so at the default 10 each player gets 2.5 wrong
  guesses a minute
- the challenge point total is non-zero (it is the phase 2 denominator)
- with `--after-import`: every CSV row became an account, and the scoreboard
  endpoint returns every account rather than a capped page

### `tools/roster_build.py`

`check` / `refresh` / `synth`, described in section 2 above.

---

## Recommended order

```powershell
# days ahead — every failure here has a fix that takes time
python tools\selftest_loadtest.py                    # calibrate the harness (free)
python tools\provider_check.py                       # does one call parse?
python tools\loadtest_deepinfra.py --dry-run         # plan + bill
python tools\loadtest_deepinfra.py                   # 700 images
python tools\loadtest_replicate.py                   # 400 images
python tools\loadtest_compare.py                     # -> IMAGE_PROVIDERS line

# rehearsal
python tools\roster_build.py refresh
python tools\roster_build.py check --expect-teams 175 --expect-players 700
python tools\e2e_dryrun.py

# event morning
docker compose up -d --build
python tools\ctfd_preflight.py
# ... run Phase 1, import the CSV ...
python tools\ctfd_preflight.py --after-import --csv state\roster_cache_phase2.csv
```

`EVENT_RUNBOOK.md` has this woven into the ordered steps.

---

## Files changed

| File | Change |
|---|---|
| `DWR_Phase_2/.../scripts/final_leaderboard.py` | anchored the scores path to `__file__`; added a search fallback |
| `Dark_Web_Rises_Phase1/game.py` | roster size guard; `TURN_TIMEOUT` warning; startup timing log |
| `Dark_Web_Rises_Phase1/services/providers.py` | breaker probe leak on cancellation; 150 → 175 in comments |
| `Dark_Web_Rises_Phase1/docker-compose.yml` | CTFd workers; db/cache healthchecks; `EXPECTED_*`; capacity comments |
| `Dark_Web_Rises_Phase1/.env.example` | `EXPECTED_*`, `CTFD_WORKERS`, concurrency and clock guidance |
| `Dark_Web_Rises_Phase1/EVENT_RUNBOOK.md` | 175 teams; provider proving; preflight; rehearsal |

## Files added

All under `Dark_Web_Rises_Phase1/tools/`:

| File | |
|---|---|
| `image_load_test.py` | the load-test engine |
| `loadtest_deepinfra.py` | 700 images, one round's shape |
| `loadtest_replicate.py` | 400 images, ramped |
| `loadtest_compare.py` | decides the chain order from the data |
| `selftest_loadtest.py` | calibrates the harness, free |
| `e2e_dryrun.py` | the whole event at 175 teams |
| `ctfd_preflight.py` | the CTFd half |
| `roster_build.py` | check / refresh / synth the roster |
