# Running Dark Web Rises from a fresh clone

For anyone who just pulled `dwr_full_dev` and wants to run the tests or play a
round locally. Nothing here costs money.

---

## The two files the repo does not contain

Both are gitignored on purpose, and the stack will not start without them.

### 1. `roster_cache.json` — generate this FIRST

Compose bind-mounts it as a file:

```yaml
- ./roster_cache.json:/app/roster_cache.json:ro
```

When Docker bind-mounts a host path that does not exist, it creates a
**directory** there. The app then finds a directory where it expects JSON,
refuses to start, and you are left with a stray `roster_cache.json/` folder
that makes every retry fail the same way. If that has already happened, delete
the directory before continuing.

```bash
cd Dark_Web_Rises_Phase1
python tools/roster_build.py synth --teams 175 --out roster_cache.json
```

`--out` is not optional — `synth` defaults to `roster_cache_synthetic.json`,
which the mount will not find. The script is stdlib-only, so it runs before you
have installed anything.

It is gitignored because the real one holds attendee credentials in plaintext:
the server needs them unhashed at boot. Never commit it.

### 2. `.env`

`env_file: - .env` is a hard error when the file is missing.

```bash
cp .env.example .env
```

Then set **one** line, so you can run without spending anyone's credit:

```
IMAGE_PROVIDERS=pollinations
```

Pollinations is free and needs no key. Everything except provider-specific
timing behaves identically. Leave `DEEPINFRA_API_KEY` empty — an empty value is
handled gracefully, the chain just drops that provider with a warning.

---

## Full sequence

```bash
git clone -b dwr_full_dev https://github.com/TAS-Tech26/Dark_Web_Rises.git
cd Dark_Web_Rises/Dark_Web_Rises_Phase1

cp .env.example .env
#   then set IMAGE_PROVIDERS=pollinations

python tools/roster_build.py synth --teams 175 --out roster_cache.json

docker compose build            # NOT `up --build`, see below
docker compose --profile dev up -d
```

- Game `http://localhost:5173` · Backend `:8000` · CTFd `:8080`
- Moderator login is `DWR_ADMINS` in `.env`.

---

## Running the tests

All three are self-contained: they build their own temporary roster, set their
own environment, touch no network, need no API key and cost nothing.

| Script | Checks | What it pins down |
|---|---|---|
| `tools/test_session_handling.py` | 13 | one account / one device, probe liveness, the ghost-connection regression |
| `tools/test_checkpoint_guard.py` | 14 | a stale checkpoint cannot silently end the event |
| `tools/e2e_dryrun.py` | 34 | 175 teams, 700 players over real websockets, through to the phase 2 CSV |

They need torch and open_clip. Rather than installing those locally, run them
in the image that already has them:

```bash
docker compose build dwr
docker compose run --rm --no-deps dwr python tools/test_session_handling.py
docker compose run --rm --no-deps dwr python tools/test_checkpoint_guard.py
docker compose run --rm --no-deps dwr python tools/e2e_dryrun.py
```

`--no-deps` keeps it from starting CTFd and MariaDB just to run a unit test.

If `tools/` turns out to be excluded from the image, mount it in:

```bash
docker compose run --rm --no-deps -v "$PWD/tools:/app/tools" dwr \
    python tools/test_session_handling.py
```

`e2e_dryrun.py` also has `--real-images --max-images N`, which spends real
money at a real provider. The cap is enforced at the single generation choke
point, so N is a hard ceiling rather than a target. Do not use it without
agreeing the budget first.

---

## Four things that look like bugs and are not

**No frontend.** It sits behind a compose profile. `docker compose --profile dev
up -d`, or it does not start. Without the flag you get 4 services and a warning
about an orphan container.

**Editing Python changes nothing.** Phase 1 source is copied into the image, not
bind-mounted.

| Changed | Needed |
|---|---|
| Phase 1 Python | `docker compose build dwr` then `up -d` |
| Frontend source | nothing — bind-mounted, hot reload |
| `.env` | `up -d` (compose recreates) |
| CTFd source | `docker compose build ctfd` |

**Never `docker compose up -d --build`.** It rebuilds every service including
CTFd, whose pip install has timed out at 639s on a slow connection. Build only
what changed.

**A 409 when you press Start** means a leftover `state/game_state.json`. That is
the guard doing its job, not a failure — a checkpoint from a finished game used
to play zero rounds and jump straight to game over, writing the phase 2 CSV and
telling connected teams they had qualified. Delete the file and start again.

---

## If you are testing from a phone or another machine

Set `PUBLIC_HOST` in `.env` to the host's LAN IP:

```
PUBLIC_HOST=192.168.1.50
```

Without it the frontend tells every device to call `http://localhost:8000`,
which on a phone means *the phone*. One variable covers `VITE_API_BASE_URL`,
`VITE_CTFD_URL`, `CORS_ALLOWED_ORIGINS` and `PHASE2_CTFD_URL`.

---

## Do not commit back

- `.env` — real configuration
- `roster_cache.json` — plaintext player credentials
- `state/` — the checkpoint, the phase 2 CSV (plaintext CTFd team passwords) and
  the carried-over scores

All three are gitignored. `git status` should be clean after a run; if it is
not, look at what changed before staging anything.

See `HANDOVER.md` for project state and `EVENT_RUNBOOK.md` for the moderator
sequence on the day.
