# Dark Web Rises — handover

Written for whoever picks this up next, human or model. Read the first two
sections before touching anything; the rest is reference.

**Event shape:** 700 participants, 175 teams of 4. Phase 1 is a Chinese-whispers
image game (prompt → generated image → next player describes it → repeat).
Qualifying teams are handed off to Phase 2, a CTFd fork, via a CSV of generated
team logins. A moderator drives the whole thing.

---

## 1. Read this first: state as of 21 Aug 2026

**The stack runs end to end.** Phase 1, CTFd, database, cache and the dev
frontend all come up under one compose file, a full round plays, and the phase 2
handoff produces the CSV. What follows is what is *not* yet done.

### Blocking before the event

| # | Item | Where | Why it matters |
|---|---|---|---|
| 1 | `TOTAL_ROUNDS=1` → `5` | `.env:44` | Set to 1 for a manual test. A one-round game scores out of 100, but `final_leaderboard.py --max-phase1` defaults to 500. Leave it at 1 and every Phase 1 score is silently scaled wrong on the final scoreboard. |
| 2 | `DEEPINFRA_API_KEY=` is empty | `.env:49` | Supplied from the shell so far, so it dies with the terminal. Either put it in `.env` or make sure whoever runs the event exports it. |
| 3 | Roster is 150 teams / 600 players | `roster_cache.json` | Event needs 175 / 700. `SUPABASE_URL` is still `https://your-project.supabase.co` — a placeholder. |
| 4 | `DWR_ADMINS=eventadmin:admin_password` | `.env:184` | The moderator login. Change it. |
| 5 | Revoke the leaked `HF_TOKEN` | github.com | See §6. Already public in git history. |
| 6 | Delete `state/game_state.json` | `state/` | Any leftover checkpoint. The app now refuses to start rather than silently ending the event, but you still have to clear it. |

### Decided, done, don't re-litigate

- **Image provider: DeepInfra, FLUX-1-schnell, 512×512.** Not klein-4b — see §3.
- **Session policy: one account, one device, first device holds it.** Phase 1 only.
- **Frontend runs behind the `dev` compose profile.** `--profile dev` or it doesn't start.

---

## 2. How to run it

```bash
cd Dark_Web_Rises_Phase1
docker compose build              # NOT `up --build` — see §7
docker compose --profile dev up -d
```

- Game: `http://localhost:5173` · Backend: `:8000` · CTFd: `:8080`
- Moderator starts the game from the admin dashboard (`DWR_ADMINS` credentials).

**Set `PUBLIC_HOST` in `.env` before anyone connects from a phone.** It is not
set. Without it the frontend tells every device to call `http://localhost:8000`,
which on a player's phone means *the phone*. Set it once to the host's LAN IP
and it propagates to `VITE_API_BASE_URL`, `VITE_CTFD_URL`, `CORS_ALLOWED_ORIGINS`
and `PHASE2_CTFD_URL`.

```
PUBLIC_HOST=192.168.1.50
```

`EVENT_RUNBOOK.md` has the full moderator sequence. `EVENT_READINESS.md` is the
longer engineering write-up.

### The moderator's path on the day

1. Start the stack, confirm the roster count matches the real attendee list.
2. Players log in; moderator watches the dashboard fill.
3. Moderator presses Start. Rounds play automatically.
4. At game over the app writes `state/roster_cache_phase2.csv` and shows
   qualifying teams the CTFd link.
5. Moderator bulk-imports that CSV into CTFd **in user mode**.
6. After Phase 2, moderator gets a CTFd access token and runs
   `final_leaderboard.py` to produce the combined scoreboard.

---

## 3. The findings that cost the most to learn

### klein-4b is served serially — this would have killed the event

FLUX-2-klein-4b produces *better* images and looked completely healthy under
load: 200 concurrent requests, **zero errors, zero 429s**. It is still
unusable.

200 requests took 98.3s — 0.49s apart, in near-perfect sequence. By Little's
Law that is an effective concurrency of **0.85**. Model inference is 420ms, so
**99.2% of the latency is provider-side queueing**, not compute. A 175-team
round would take 66s to drain against a 90s turn, with no margin.

No conventional metric shows this. Success rate: 100%. Error rate: 0. Only the
*shape* of the completion timeline gives it away — which is what
`tools/analyse_run.py` exists to detect (CV < 0.9 ⇒ serial FIFO queue).

If you ever reconsider klein-4b, DeepInfra would have to grant more workers
first. A capacity request was drafted and never sent.

### FLUX-1-schnell at 512×512 is the answer

Measured across ~1,900 real images:

| | |
|---|---|
| Throughput | 8 img/s (plan against **6** — run-to-run variance is ~25%) |
| Break-even for the event | 1.94 img/s → **3.2× margin** |
| Success rate | 100% |
| Request p95 | 10.4s under 120s of sustained load |
| Worst-case player wait | ~28.5s against a 90s turn |
| Cost, whole 3,500-image event | **$0.44** |

Works on the existing config. No tuning needed.

### The adapter was billing every image at 1024×1024

FLUX is billed by **area**: `$0.014 × (w/1024) × (h/1024)`. The adapter never
sent `width`/`height`, so every image cost the full $0.014 regardless of what
came back. At 512×512 it is $0.0035 — a **4× saving**, confirmed against
DeepInfra's own `inference_status.cost` field, which the adapter now records.

### DeepInfra concurrency is per-model and stacks

Their docs say 200 concurrent **per model**. I initially claimed it was
account-level; the user pushed back, and a parallel klein+schnell run proved
them right — both models got *faster* together, not slower. If you need more
headroom, spreading across two models is a real option.

---

## 4. Bugs found and fixed (don't reintroduce these)

### The ghost connection

`_cleanup_connection` guarded against tearing down a live session by comparing
`game.connected_sockets[uid]` with `team.connected_sockets[uid]` — which
`check_login` sets to *the same object*. The guard was always False and **never
once fired**. A closing socket therefore tore down whichever session was live:
still logged in on screen, no socket on the server, no frames ever again. It
looked exactly like a frontend bug.

Fixed by passing the actual socket in and comparing against that. Pinned by
`tools/test_session_handling.py` check 5.

### A stale checkpoint silently ended the event

With a leftover `state/game_state.json` saying the game was finished, pressing
Start would: play **no rounds**, broadcast GAME_OVER, write the phase 2 CSV,
generate CTFd passwords, and tell connected teams they had qualified. Nothing
looked like an error — the dashboard showed a completed game.

The mechanism: `_play_all_rounds` *did* notice and returned early, but
`start_games` calls `_finish_game()` from a `finally`, so returning early does
not abort the game — **it completes it**. The guard therefore had to go at the
start endpoint, where a human is still in the loop.

`/admin/rungame` now returns **409** with an explanation if the checkpoint is
complete or if `TOTAL_ROUNDS` changed under it. Partial checkpoints still
resume (crash recovery is intact); a corrupt one starts fresh rather than
blocking the event. `DWR_RESUME_COMPLETED=1` overrides, for the narrow case of
a real crash *after* the final round.

### `final_leaderboard.py` wrote outside the repo

Its default path was CWD-relative. Anchored to `__file__` with a fallback
search.

### The roster guard crashed on an empty string

A compose default of `${EXPECTED_TEAMS:-}` fed `int("")`. Fixed with
`_optional_int()`.

---

## 5. Session policy — Phase 1 only

**One account, one device, and the FIRST device keeps it.** A second login is
refused while the original session still answers. To switch devices, log out
first.

The subtlety that stops this becoming a help-desk queue: a registered socket is
not evidence of a live device — TCP can take minutes to notice a phone that
walked out of wifi range. So liveness is **asked about**, not assumed. The
server sends a probe and waits `SESSION_PROBE_TIMEOUT` (3s). No ack ⇒ the old
session is treated as gone and the player gets in. Errs toward *releasing* the
account, because locking a player out of their own account is worse than a
brief double session.

Refusal is its own status (`Login.ALREADY_CONNECTED`), not `DENIED` — a player
told "invalid credentials" will retype until the lockout fires.

**This must not be copied into Phase 2.** Phase 2 is a team game on one shared
CTFd account: four people need four concurrent sessions on the same login.

---

## 6. Git, secrets, and the repo layout

### Branches on `github.com/TAS-Tech26/Dark_Web_Rises`

| Branch | What's on it |
|---|---|
| `feature/Phase1_model` | Phase 1 at the repo root — the working branch |
| `phase2` | The CTFd 3.8.6 fork at the repo root |
| `dwr_full_dev` | **New.** Both phases in one clone-and-run tree |

`dwr_full_dev` exists because Phase 1's compose builds CTFd from
`CTFD_SOURCE_DIR=../DWR_Phase_2/Dark_Web_Rises` — the runnable unit is the
parent folder, so a clone of either branch alone does not start. Its history is
unrelated to the other two branches, deliberately.

Built by `assemble_full_repo.ps1` (in the parent folder). Re-run it to refresh
`dwr_full_dev` from the working folders; it stops before committing and audits
the staged tree first.

### Secrets

- **`HF_TOKEN` is in git history** — committed in `27d70c1`, removed in
  `b7106ca`, and `27d70c1` is already pushed on two branches. Removing the file
  did not remove the token. **Revoke it.** Rewriting history is not worth it
  for a token that has been public for weeks.
- **`ctf-export/dwr_ctf.zip` must never be committed.** It is a full CTFd
  database export: `db/flags.json` holds all 7 challenge flags in plaintext,
  `db/config.json` holds `mail_username`/`mail_password`, plus an API token and
  password hashes. Gitignored at the root of `dwr_full_dev`.
- **`DWR_Phase_2/Dark_Web_Rises/.data/`** is a live 137 MB MariaDB volume — the
  running CTF's flags, accounts and solves. Gitignored, excluded from the copy.

### Two repo hazards

- **`DWR_Phase_2/.git`** (the outer one) is a broken clone with an empty
  working tree and 1048 pending deletions. **Never push from that directory** —
  it would delete the CTFd fork off the `phase2` branch. Safe to delete once
  `dwr_full_dev` is confirmed good.
- **CRLF churn.** In `Dark_Web_Rises_Phase1`, `git status` shows ~136 modified
  files of which only **13** have real changes; the rest are line-ending flips
  that diff as "49 insertions, 49 deletions" with no character changed. Stage
  explicitly rather than `git add .`. `dwr_full_dev` has a `.gitattributes`
  that fixes this permanently — it was free to add on a fresh tree.

---

## 7. Gotchas that already bit us

**Rebuild after Python changes.** Phase 1 source is *copied into* the image, not
bind-mounted. Editing `game.py` and running `up -d` changes nothing.

| Change | Needed |
|---|---|
| Phase 1 Python | `docker compose build dwr` then `up -d` |
| Frontend source | nothing — bind-mounted, hot reload |
| `.env` | `up -d` (compose recreates) |
| CTFd source | `docker compose build ctfd` |

**Never `docker compose up -d --build`.** It rebuilds *every* service including
CTFd, whose pip install has timed out at 639s on a slow connection. The
Dockerfile now sets `PIP_DEFAULT_TIMEOUT=120`, `PIP_RETRIES=10` and a BuildKit
cache mount so a retry resumes — but build only what changed.

**`--profile dev` is required for the frontend.** Without it compose brings up
4 services and reports any running frontend container as an *orphan*. If you
see that warning, check the `frontend` service actually exists in
`docker-compose.yml` — it was accidentally overwritten once and the symptom
looks identical.

**Don't run git over a mounted Windows folder from a remote session.** It
leaves 0-byte `.git/index.lock` files that the user's own git cannot remove,
producing "Another git process seems to be running". Clear with
`Remove-Item .git\index.lock`.

**`device_bash` cannot delete.** `rm` fails on mounted files. Move things into
a `_to_delete/` folder and let the user remove it. There are leftovers in
`Dark_Web_Rises_Phase1\state\_to_delete\` right now.

---

## 8. Architecture notes worth knowing

- **Single process, in-memory game state. Do not scale horizontally.** Two
  instances means players split across processes that cannot see each other.
  Scale vertically — compose pins `replicas: 1` and `cpus: "4.0"`.
- **Scoring** is CLIP RN50-quickgelu at 224×224 in a bounded ThreadPoolExecutor.
  No autocast: it is 228× slower on CPU. ~45ms per encode, ~8 CPU-seconds per
  round boundary, absorbed in ~2s on 4 cores.
- **`MAX_CONCURRENT_IMAGE_REQUESTS=40`** is an `asyncio.Semaphore` held *around*
  the provider call, so queue time is **not** exposed to
  `IMAGE_GEN_TIMEOUT_SECONDS`. Lowering it improves latency and reduces
  throughput — the two pull in opposite directions, so measure the one you care
  about.
- **Provider chain** is a circuit breaker (CLOSED/OPEN/HALF_OPEN) with
  per-request failover across `IMAGE_PROVIDERS`.
- **anyio's default thread limiter is 40**, and it gates httpx DNS resolution.
  Above ~40 concurrent requests this is an invisible ceiling; the load-test
  harness raises it (`tune_runtime_for_concurrency`).
- The harness is deliberately **HTTP/1.1** — HTTP/2's
  `SETTINGS_MAX_CONCURRENT_STREAMS` would silently cap it near 100.

---

## 9. Tooling inventory

All under `Dark_Web_Rises_Phase1/tools/`.

**Tests — free, no network, run these after any change**

| Script | Covers |
|---|---|
| `test_session_handling.py` | one-device policy, probe liveness, ghost-connection regression — **13 checks** |
| `test_checkpoint_guard.py` | stale-checkpoint refusal incl. a live 409 endpoint test — **14 checks** |
| `e2e_dryrun.py` | 175 teams / 700 players over real websockets — **34 checks**. `--real-images` with a hard `--max-images` cap for a paid run |

All three were passing at handover.

**Load testing — these spend money**

| Script | Purpose |
|---|---|
| `image_load_test.py` | the engine: burst / sweep / steady, real adapters, reports actual billed cost |
| `loadtest_deepinfra.py` | 4 bursts × 175 = 700 images |
| `loadtest_replicate.py` | sweep 25/50/100/175, judged against the 8s fallback budget |
| `loadtest_pollinations.py` | free provider, gentle 5/10/20/40 ramp |
| `loadtest_compare.py` | prints the recommended `IMAGE_PROVIDERS=` line |
| `analyse_run.py` | rebuilds the completion timeline — **this is what caught klein-4b** |
| `selftest_loadtest.py` | calibrates the harness against an exact injected multiset |
| `check_client_concurrency.py` | proves genuine overlap against a local server (client-side counters lie) |

**Operational**

| Script | Purpose |
|---|---|
| `roster_build.py` | `check` / `refresh` / `synth` the roster |
| `ctfd_preflight.py` | verifies CTFd is importable before the handoff |
| `provider_check.py` | one image per provider, cheap smoke test |

---

## 10. Open questions

1. **klein vs schnell image quality.** klein-4b looked notably better and the
   sample folders were never compared side by side. Only worth revisiting if
   DeepInfra grants more workers.
2. **DeepInfra capacity request** for klein-4b — drafted, never sent.
3. **Replicate** was scripted for a 400-image test that was never run. DeepInfra
   alone is sufficient, so this is a fallback question, not a blocking one.
4. **Challenge videos in git.** `dwr_full_dev` carries ~158 MB of mp4 under
   `themes/core/static/challenge_1/`. Under GitHub's limits, but every clone
   pays it. Git LFS would need a fresh branch — it has to be set up before the
   first commit.
