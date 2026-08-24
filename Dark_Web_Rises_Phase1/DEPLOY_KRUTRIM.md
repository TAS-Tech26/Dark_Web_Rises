# Deploying Dark Web Rises behind a DNS name

For the Krutrim deployment. Ordered, with a verification gate after each step.

**The one rule: add one variable at a time.** Get the stack healthy on the box
before DNS. Get DNS working on plain HTTP before TLS. Get TLS working before
swapping in the paid image provider. Every step below has a check with an
unambiguous pass signal, so when something breaks you know which change did it.
Most of the pain in a deployment like this comes from changing three things and
then debugging the intersection.

---

## 0. Size the instance

Compose asks for 4 cores and 6 GB for the game container alone, and CTFd,
MariaDB, Redis and the frontend all sit beside it.

| | |
|---|---|
| vCPU | 8 minimum |
| RAM | 16 GB |
| Disk | 50 GB |
| Egress | **required at first boot** — see step 4 |

Why 4 cores for the game: at a round boundary 175 teams finish within the same
second and each triggers a CLIP RN50 encode at ~45 ms. That is ~8 CPU-seconds
of work arriving at once, which 4 cores absorb in about 2 seconds. Fewer cores
turns each round boundary into a visible stall.

Disk is mostly generated images: 175 teams × 4 turns × 5 rounds = 3,500 images
at 150–400 KB, so 0.5–1.4 GB per event, and **nothing deletes them between
runs**. They accumulate across rehearsals.

---

## 1. Prerequisites on the box

```bash
docker --version          # needs the compose plugin, not docker-compose v1
docker compose version
git --version
```

Open inbound 80 and 443 only. Everything else stays behind the proxy — 8000,
8080 and 5173 should **not** be reachable from the internet.

---

## 2. Clone and configure

```bash
git clone -b dwr_full_dev https://github.com/TAS-Tech26/Dark_Web_Rises.git
cd Dark_Web_Rises/Dark_Web_Rises_Phase1

cp .env.example .env
python3 tools/roster_build.py synth --teams 175 --out roster_cache.json
```

`roster_cache.json` must exist **before** the first `up`. Compose bind-mounts it
as a file, and Docker creates a *directory* when a bind-mount source is missing
— the app then finds a directory where it expects JSON, refuses to start, and
leaves a stray `roster_cache.json/` that makes every retry fail identically. If
that has already happened, delete the directory.

Edit `.env`:

```ini
TOTAL_ROUNDS=5                  # 1 is a leftover test value
IMAGE_PROVIDERS=pollinations    # free; switch to deepinfra at step 8
DWR_ADMINS=<pick>:<pick>        # not eventadmin:admin_password
CTFD_SECRET_KEY=<generate>      # changing it later invalidates every session
```

Leave `PUBLIC_HOST` alone for now. Leave `DEEPINFRA_API_KEY` empty — an empty
value is handled gracefully; the provider chain drops it with a warning naming
the variable.

**Gate:** `grep -E '^(TOTAL_ROUNDS|IMAGE_PROVIDERS|DWR_ADMINS)=' .env` reads back
what you intended.

---

## 3. Build

```bash
docker compose build
```

**Not `up --build`.** That rebuilds every service including CTFd, whose pip
install has timed out at 639 s on a slow link. The Dockerfile sets
`PIP_DEFAULT_TIMEOUT=120`, `PIP_RETRIES=10` and a BuildKit cache mount so a
retry resumes rather than restarting, but build only what changed.

Expect the CTFd image to be the slow one. Ten to twenty minutes on a fresh box
is normal.

**Gate:** `docker images | grep -E 'dark-web-rises|ctfd|dwr-frontend'` shows all
three.

---

## 4. Run the tests before exposing anything

```bash
docker compose run --rm --no-deps dwr python tools/test_session_handling.py
docker compose run --rm --no-deps dwr python tools/test_checkpoint_guard.py
docker compose run --rm --no-deps dwr python tools/e2e_dryrun.py
```

Self-contained: own temporary roster, own environment, no network, no API key,
no cost. `--no-deps` keeps CTFd and MariaDB from starting just to run a unit
test.

**Gate:** 13, 14 and 34 checks respectively, zero failures. If `tools/` is not
in the image, mount it: `-v "$PWD/tools:/app/tools"`.

---

## 5. Bring it up locally — no DNS yet

```bash
docker compose --profile dev up -d
docker compose logs -f dwr
```

**First boot downloads model weights.** `warm_up_model()` fetches the `openai`
RN50 weights through open_clip and runs `nltk.download('words')`, and it does
this *before* Uvicorn binds the port. Three consequences:

- The box needs outbound internet on first boot. If egress is restricted the
  log says so explicitly and tells you to pre-download the weights into the
  local model directory — that message is there because this failure is
  otherwise unreadable torch stack trace.
- **The port is closed until warm-up finishes.** Any health check or
  orchestrator with a short startup grace period will kill the container and
  restart it into the same slow warm-up, forever. Give it several minutes of
  start period.
- It is a one-off. Later boots load from cache.

Verify from the box itself, so nothing network-related is in play yet:

```bash
curl -sf http://localhost:8000/health && echo BACKEND-OK
curl -sf -o /dev/null http://localhost:5173/ && echo FRONTEND-OK
curl -sf -o /dev/null http://localhost:8080/ && echo CTFD-OK
```

**Gate:** three OKs. If you want to look at the UI before DNS exists, use an SSH
tunnel (`ssh -L 5173:localhost:5173 …`) rather than opening the port.

---

## 6. DNS on plain HTTP

Create **two** A records, both pointing at the box:

```
dwr.example.com       ->  <box IP>
ctfd.dwr.example.com  ->  <box IP>
```

The second one is for step 7. CTFd generates absolute URLs, so serving it under
a path like `/ctfd` needs `APPLICATION_ROOT` configured and breaks in the places
you check last — a subdomain avoids the whole class of problem. Create it now
because DNS propagation is the one thing you cannot hurry later, and the
certificate in step 7 cannot be issued until it resolves.

Then one line in `.env`:

```ini
PUBLIC_HOST=dwr.example.com
```

```bash
docker compose --profile dev up -d
```

That single variable now drives `CORS_ALLOWED_ORIGINS`, `VITE_API_BASE_URL`,
`VITE_CTFD_URL`, `PHASE2_CTFD_URL` **and** `VITE_ALLOWED_HOSTS`.

That last one matters: Vite rejects requests whose `Host` header it does not
recognise — `Blocked request. This host is not allowed.` — and it is the first
wall you hit after pointing DNS at a box, reading like a proxy fault rather than
a frontend setting. `vite.config.ts` now takes the list from `PUBLIC_HOST`
automatically, so there is nothing to edit. If you somehow still see it,
`allowedHosts: true` in that same block is the blunt fallback.

**Gate:** the login page loads over `http://dwr.example.com:5173` **and a player
can actually log in.** The second half is the real test — it is what proves the
websocket connected. A page that renders proves only that HTML was served.

---

## 7. TLS and the reverse proxy

**There is no TLS in this stack.** It publishes 8000, 5173 and 8080 as plain
HTTP and nothing terminates HTTPS. This step adds it. Skip the whole step if
players reach the game over a closed venue LAN by IP — there is no name to
certify and the traffic never leaves the local network. Do it if the box is
reachable from the internet, because players type passwords and every websocket
frame after that is readable otherwise.

### How it works, in one picture

Right now three doors are open. TLS means putting **one** process in front that
owns 80 and 443, holds the certificate, decrypts, and forwards plain HTTP
inward over Docker's private network:

```
                     ┌─ Caddy ──────────┐
browser ──https──>   │ :443  cert here  │──http──> frontend:5173
        ──wss───>    │ :80   ACME only  │──http──> dwr:8000
                     └──────────────────┘──http──> ctfd:8000
                          (public)          (private docker network)
```

The application never learns TLS exists. `dwr` still speaks plain HTTP on 8000
exactly as before — it simply stops being reachable from outside. **That is half
the benefit**: after this step the only public port on the box is 443 (plus 80,
which exists only for certificate issuance).

### Where the certificate comes from

You do not buy or install one. Caddy asks Let's Encrypt for a certificate for
`dwr.example.com`; Let's Encrypt says "prove it" by connecting to that name on
**port 80**; Caddy answers with a token it was told to serve; the certificate is
issued, valid 90 days, renewed automatically from then on.

Two consequences that catch people:

- **DNS must resolve before you start.** A certificate certifies a *name*. No
  public authority issues one for a bare IP. `dig dwr.example.com` must return
  the box's address, and so must `dig ctfd.dwr.example.com`.
- **Port 80 must stay open** even though nobody browses it, or renewal fails
  silently 60 days from now.

### 7a. The Caddy service

```yaml
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    profiles: ["tls"]
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data        # certificates live here -- must persist
      - caddy-config:/config
    depends_on:
      - dwr
      - frontend
      - ctfd
    networks:
      - event
```

Add `caddy-data:` and `caddy-config:` to the `volumes:` block at the bottom of
the file. `caddy-data` is the one that matters — lose it and every restart
re-requests certificates, and Let's Encrypt rate-limits that to 5 per name per
week. That limit is very easy to hit while iterating.

### 7b. The Caddyfile

Both the frontend and the backend answer on `/`, so the split has to be exact.
`/admin/login` is a **frontend** page while `/admin/dashboard` and
`/admin/rungame` are **backend** endpoints — a blanket `/admin*` rule breaks the
moderator login screen.

```
dwr.example.com {
    @backend path /ws /health /admin/rungame /admin/dashboard /static/*
    reverse_proxy @backend dwr:8000
    reverse_proxy frontend:5173
}

ctfd.dwr.example.com {
    reverse_proxy ctfd:8000
}
```

That is the whole file. Caddy handles the websocket `Upgrade` automatically and
does not impose a short idle timeout, which is why it is the recommendation
here — see the nginx note in §10 if you use that instead.

### 7c. Close the direct ports

Delete the `ports:` block from `dwr`, `frontend` and `ctfd` in
`docker-compose.yml`. If you leave them published, the game is still reachable
over plain HTTP on 8000/5173/8080 and the TLS is decorative.

### 7d. The `.env` changes

```ini
PUBLIC_HOST=dwr.example.com
VITE_API_BASE_URL=
VITE_CTFD_URL=https://ctfd.dwr.example.com
PHASE2_CTFD_URL=https://ctfd.dwr.example.com
CORS_ALLOWED_ORIGINS=https://dwr.example.com
```

`VITE_API_BASE_URL=` is a **bare assignment** — nothing after the `=`. That is
the single most important line in this file.

Without it compose supplies `http://dwr.example.com:8000`, and from an
`https://` page that is mixed content: the browser blocks it and the failure
presents as "the backend is down". Empty means *same origin* — `getApiBaseUrl()`
returns `""`, `getWebSocketUrl()` derives `wss://` from the page's own protocol,
the scheme is always right, and CORS stops applying at all.

It works because `docker-compose.yml` reads it as `${VITE_API_BASE_URL-…}` with
**no colon**. The `:-` form treats empty as absent and substitutes the default
straight back, so with a colon the line silently does nothing. If you ever edit
that expression, keep the colon out.

The last three lines are needed because the derived defaults carry `:5173` and
`:8080`, and yours are on 443.

```bash
docker compose --profile dev --profile tls up -d
docker compose logs -f caddy        # watch the certificate arrive
```

**Gate:** open the site on `https://`, log in, and confirm in devtools →
Network → WS that the socket shows `wss://dwr.example.com/ws` with status
**101**. The 101 is the only proof that matters — a page that renders says
nothing about whether the socket connected. Then check
`https://ctfd.dwr.example.com` serves CTFd with a valid padlock.

---

## 8. Switch to the real image provider

Only now, once everything else is proven:

```ini
IMAGE_PROVIDERS=deepinfra
DEEPINFRA_API_KEY=<key>
DEEPINFRA_MODEL=black-forest-labs/FLUX-1-schnell
DEEPINFRA_SIZE=512
```

`DEEPINFRA_SIZE=512` is not cosmetic. FLUX bills by area —
`$0.014 × (w/1024) × (h/1024)` — so leaving it unset bills every image at
1024×1024. At 512 it is $0.0035, a 4× saving, and the whole 3,500-image event
costs about $0.44.

```bash
docker compose up -d
docker compose run --rm --no-deps dwr python tools/provider_check.py
```

**Gate:** one image per configured provider, no errors. Then play one full round
end to end with a handful of browsers before trusting it.

---

## 9. Persistence

`./state` must live on storage that survives the container, and ideally the
instance. At game over it receives `roster_cache_phase2.csv` — the **only**
plaintext copy of the CTFd team passwords. CTFd stores them bcrypt-hashed, so
if that file is lost after import every qualified team is locked out with no
recovery path.

Copy it off the box the moment it is written.

---

## 10. Things that will quietly ruin the event

**Never run more than one instance of `dwr`.** Game state is in-process memory.
Two instances means players split across processes that cannot see each other:
teams half-present, scores that do not add up, and no error anywhere. Compose
pins `replicas: 1`; a cloud load balancer with an instance group will happily
undo that. Scale cores, never instances.

**Idle timeouts on anything in the path.** Many proxies and cloud load balancers
default to 60 s. A player thinking about a prompt sends nothing, and gets
disconnected mid-round. Caddy does not do this, which is one reason it is the
recommendation. nginx does — it needs the upgrade headers spelled out *and* a
long read timeout:

```nginx
location /ws {
    proxy_pass http://dwr:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

If Krutrim's own load balancer sits in front of the box as well, check its idle
timeout too. It is the one you cannot see from inside the machine.

**`vite dev` is not a production frontend.** The `dev` profile serves unbundled
modules compiled on demand — hundreds of requests per page load plus an HMR
websocket per client. It is the right tool for proving DNS and TLS. It is not
what should face 700 people on venue wifi. If Krutrim is where the event
actually runs, budget separate work for a production build served as static
files behind the same proxy.

**Editing Python changes nothing** until `docker compose build dwr`. The source
is copied into the image, not mounted. The frontend *is* mounted, so it hot
reloads.

**A 409 on Start** means a leftover `state/game_state.json`. That is the guard
working: a checkpoint from a finished game used to play zero rounds and jump
straight to game over, writing the phase 2 CSV and telling connected teams they
had qualified. Delete the file.

---

## 11. Freeze checklist, day before

```bash
python3 tools/roster_build.py check --expect-teams 175 --expect-players 700
```

That command is the one that earns its keep. If Supabase is unreachable on the
morning the app falls back to the frozen cache, and a 150-team cache boots
perfectly cleanly, logs "Server ready", and 100 attendees simply cannot log in.
Nothing fails and nothing warns — you find out from the queue at the help desk.

- [ ] `TOTAL_ROUNDS=5`
- [ ] roster check passes at 175 / 700
- [ ] if TLS: certificate valid, `caddy-data` on persistent storage, port 80
      still open (renewal needs it), and 8000/5173/8080 **not** published
- [ ] `state/game_state.json` deleted
- [ ] generated-image volume cleared: `docker volume rm dark_web_rises_phase1_dwr-generated`
- [ ] admin password is not the default
- [ ] `provider_check.py` passes against DeepInfra
- [ ] one full round played end to end on the real URL, from a phone on mobile data — not the office wifi, and not localhost
- [ ] CTFd reachable and the challenge import verified
- [ ] someone other than the deployer has run through the moderator sequence in `EVENT_RUNBOOK.md`
