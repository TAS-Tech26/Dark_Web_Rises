# Krutrim VM — provisioning request

What to hand to whoever owns the Krutrim account. Every field below maps to
something in Krutrim's VM creation form; the reasoning is included so the
choices can be sanity-checked rather than followed blindly.

**Workload:** a Docker Compose stack (FastAPI game server + CLIP scoring, CTFd,
MariaDB, Redis, Vite frontend, Caddy reverse proxy) serving a live event for up
to 700 concurrent players over websockets, behind a DNS name with TLS.

---

## 1. The spec

| Field | Value | Why |
|---|---|---|
| **vCPU** | **8** (absolute floor 8; more is fine) | At each round boundary ~175 CLIP RN50 encodes arrive within the same second, ~8 CPU-seconds of work. 4 cores absorb it in ~2s; fewer turns every round boundary into a visible stall. |
| **RAM** | **16 GB** | The game container alone reserves 6 GB. CTFd, MariaDB, Redis and the frontend sit beside it. |
| **Boot volume** | **50 GB** NVMe SSD | See §2 — measured against the actual image sizes, this is comfortable. |
| **OS image** | **Ubuntu 22.04 LTS or 24.04 LTS** | Krutrim's own firewall docs assume Ubuntu. Anything with the Docker Compose *plugin* (not `docker-compose` v1) available works. |
| **SKU** | `CPU-16x-64GB` (AMD EPYC 9554), ₹49/hour | Hyderabad region shows this at 16 vCPU / 64 GB. `CPU-8x-32GB` at ₹25/hour also clears the floor — see §9. |
| **Region** | Nearest the event venue | VPCs are region-specific and cannot span regions, so this is fixed at creation. |
| **GPU** | **Not needed** | Scoring is CPU-bound by design (no autocast — it is 228× slower on CPU with it enabled, so the code path is deliberately CPU). A GPU SKU buys nothing without code changes. |

**Please confirm the account quota allows 8 vCPU / 16 GB before creating** —
Krutrim bills quota overages separately and a quota rejection at creation time
is a silent blocker.

---

## 2. Storage

Krutrim volumes are NVMe SSD, minimum 4 GB, maximum 2048 GB, **Rs 7.88 per GB
per month**. They can be **expanded but never reduced**, and expansion is live
if the volume is not in use.

50 GB (≈ Rs 394/month) is enough, sized against the actual build rather than a
guess:

| | Size | Basis |
|---|---|---|
| `dwr` image | ~1.5 GB | `python:3.11-slim` + **CPU-only** torch 2.3.1 (`requirements.txt` pins `torch==2.3.1+cpu` off the pytorch CPU index) + RN50 weights baked in at build |
| CTFd image | ~1 GB | includes ~158 MB of challenge video |
| Frontend image + node_modules | ~1 GB | estimate |
| MariaDB + Redis + Caddy | ~0.5 GB | stock images |
| Repo clone with history | ~0.4 GB | the videos again |
| OS + Docker | ~8 GB | |
| Generated images | 0.5–1.4 GB **per event**, accumulating | nothing clears the volume between rehearsals |
| Docker build cache | 5–10 GB | the real variable — repeated CTFd rebuilds |

Steady state ≈ 20 GB, peaking near 35 GB mid-build. 50 GB leaves comfortable
headroom, and build cache is reclaimable with `docker builder prune` rather than
with money.

Volumes can be **expanded live but never reduced**, so guessing low costs a
resize, not data. If you would rather not think about it again, 100 GB is
Rs 788/month instead of Rs 394 — genuinely marginal either way.

---

## 3. Networking — the part that matters most

### 3a. IP address type: **Reserved (static)**, not Floating

Krutrim's creation form offers both on a public subnet:

- **Floating IP** — dynamic, *reassigned on restart*
- **Reserved IP** — static, **Rs 0.28/hour** (~Rs 200/month), billed whether
  attached or not

**It must be Reserved.** Two DNS A records will point at this address and a
Let's Encrypt certificate will be issued against those names. If the address
changes on a reboot, DNS points at nothing, the site is down, and certificate
renewal fails. Reserved IP is the single most important choice in this document.

Two operational notes on it: the IP must be detached before it can be
unreserved, and once unreserved the same address **cannot be reclaimed** unless
it happens to still be in the pool. So do not release it between the test and
the event.

### 3b. Subnet: **public**

Public IPs can only be assigned to VMs in a public subnet. Leaving the subnet
field empty makes Krutrim create a default public subnet with the VPC, which is
fine.

### 3c. Security group

Krutrim security groups are created under **Networking → Security Groups**,
attached at the VPC level, applied per VM. **If no inbound rules are defined,
all inbound traffic is denied by default** — so every rule below has to be
explicit. The protocol picker has named entries (SSH, HTTP, HTTPS) that
auto-populate the port, and port ranges can be comma-separated.

**Inbound:**

| Protocol | Port | Source | Purpose |
|---|---|---|---|
| TCP | 22 | my IP/32 if static, else 0.0.0.0/0 | SSH. Krutrim's docs warn against opening 22 to the world — restrict it if I can give you a fixed address. |
| TCP | 80 | 0.0.0.0/0 | **Certificate issuance and renewal.** Nobody browses it, but it must stay open permanently or renewal fails silently ~60 days later. |
| TCP | 443 | 0.0.0.0/0 | The event itself — game and CTF, both over HTTPS/WSS. |

**Do not open 8000, 5173 or 8080.** Those are the app's internal ports. They
get published during initial setup and are deliberately closed once the reverse
proxy is in place; if they are reachable from outside, the TLS is decorative.

**Outbound: allow all.** Krutrim's docs do not state the default egress
behaviour, so **please confirm outbound is open**. The box genuinely needs it:

- Docker image pulls, pip and npm during the build — this is the big one, and
  the CTFd image's pip install has previously timed out at 639s on a slow link
- Let's Encrypt (ACME) for certificate issuance and 90-day renewal
- the DeepInfra API for every generated image during the event
- `git clone` of the application repo

### 3d. Is there a load balancer in front?

If Krutrim puts a managed load balancer between the internet and this instance,
**I need to know its idle timeout.** Many default to 60 seconds. A player
thinking about a prompt sends nothing for a minute and gets disconnected
mid-round. This is the one setting invisible from inside the machine.

If it can be raised, 3600s. If it can't, I need to know so I can plan around it.

---

## 4. SSH access

Krutrim's form asks you to **upload or paste a public SSH key at creation
time**. There is no obvious way to attach one afterwards without console access.

**Preferred:** I generate the keypair and send you only the *public* half to
paste in. That way no private key ever changes hands.

```
# I run this, and send you the contents of ~/.ssh/krutrim_dwr.pub
ssh-keygen -t ed25519 -f ~/.ssh/krutrim_dwr -C "dwr-deploy"
```

If you want your own access too, put both public keys in — or paste one and use
the **Startup Script** field (§5) to append the other to
`~/.ssh/authorized_keys`.

**Please also tell me the default login username** for the image you pick
(`ubuntu` on most Ubuntu images, but confirm it) and the reserved IP.

---

## 5. Optional: startup script

Krutrim's creation form takes a bash script that runs at first boot. Dropping
this in saves a manual step and guarantees the *compose plugin* rather than the
old v1 binary:

```bash
#!/bin/bash
set -e
apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
usermod -aG docker ubuntu
```

Entirely optional — I can run it by hand over SSH just as easily.

---

## 6. Do not enable the custom firewall (UFW)

Krutrim documents a UFW-based host firewall and warns it "may break SSH access
to your VM or other open ports." The security group in §3c already does this
job at the network level. A second firewall inside the box adds a lockout risk
and nothing else. **Leave UFW off.**

---

## 7. Costs

Krutrim bills hourly, metered in 15-minute increments rounded up. **Stopped VMs
incur no compute charge**, but storage and reserved IPs bill continuously —
which is what we want, because releasing the IP would lose the address the DNS
points at.

| Item | Rate |
|---|---|
| `CPU-16x-64GB` compute | Rs 49/hour, **only while running** |
| 50 GB NVMe volume | Rs 7.88/GB/month → **~Rs 394/month** |
| Reserved public IP | Rs 0.28/hour → **~Rs 204/month** |

Rough total for the project: one ~24h setup and test day (Rs 1,176) plus an ~8h
event day (Rs 392), against a standing ~Rs 600/month for volume and IP.

Krutrim advertises no egress charges, so the event's outbound traffic should not
add a line item.

A useful consequence of the billing model: between the end-to-end test and the
event itself, **stop the VM**. Compute stops billing, the volume and the
reserved IP persist, and everything comes back up as it was.

---

## 8. What I need back

- [ ] Reserved (static) public IP address
- [ ] SSH login username
- [ ] Confirmation the private key I sent the public half of is attached
- [ ] Confirmation outbound internet is open
- [ ] Region the instance was created in
- [ ] Whether a managed load balancer sits in front, and its idle timeout
- [ ] Confirmation 8000 / 5173 / 8080 are **not** open inbound

---

## 9. Two things the docs don't answer

Worth checking in the console rather than assuming:

1. **CPU SKU names are not in the public docs** — the flavour list appears only
   in the console. As of now Hyderabad shows `CPU-8x-32GB` at Rs 25/hour and
   `CPU-16x-64GB` at Rs 49/hour, both AMD EPYC 9554, both up to 10 Gbps. Either
   clears the documented floor; the 16x is chosen for headroom on the one load
   nobody has measured (the Vite dev server in front of 700 clients).
2. **Whether the boot volume survives VM deletion** is not documented. Assume it
   does not, and treat the box as rebuildable — which it is, since everything is
   in git and Compose apart from `.env`, the roster, and `state/`.
3. **Whether a VM's flavour can be resized in place** is not documented. If it
   can, it is a useful escape hatch; if not, the SKU is fixed at creation.
