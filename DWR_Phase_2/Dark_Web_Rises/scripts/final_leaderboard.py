#!/usr/bin/env python3
"""Combine Phase 1 (Dark Web Rises) and Phase 2 (CTFd) into a final leaderboard.

Each phase is normalised to 0..1 against its own maximum, averaged, and scaled
to 0..100, so neither phase dominates just because it has more points on offer.

    combined = (phase1/max1 + ctf/maxCtf) / 2 * 100

Usage
-----
    set CTFD_ADMIN_TOKEN=ctfd_xxxxx
    python scripts/final_leaderboard.py

Everything has a default; override as needed:

    python scripts/final_leaderboard.py --ctfd-url http://localhost:8080 \
        --phase1-scores ../../../Dark_Web_Rises_Phase1/state/phase1_scores.json \
        --out final_leaderboard.json

Uses only the standard library, so it runs on the moderator's machine with no
`pip install` step.

How teams are matched across the two systems
--------------------------------------------
Phase 1 keys teams by integer team_id. CTFd keys them by its own team id, and
its scoreboard endpoint returns only `account_id`, `name` and `score` -- no
email. Matching on team *name* is unreliable: names are free text, teams can
rename themselves, and two systems can disagree on whitespace or case.

So the join goes through the email address `final_score.py` generates:

    team_id 7  ->  team7@example.invalid

which is read from /api/v1/teams (admin only). That address is deterministic
and immutable, which is exactly what a join key needs to be.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request

# Mirrors EMAIL_DOMAIN in final_score.py. Change both together.
EMAIL_RE = re.compile(r"^team(\d+)@example\.invalid$", re.IGNORECASE)

_HERE = os.path.dirname(os.path.abspath(__file__))

# Anchored to this file, NOT to the current directory.
#
# This used to be a bare relative path, and the "../../.." in it is correct
# only when resolved from this script's own directory:
#
#   scripts/ -> Dark_Web_Rises -> DWR_Phase_2 -> <repo root> -> Phase 1
#
# Resolved from the working directory the runbook tells you to be in
# (DWR_Phase_2/Dark_Web_Rises), the same three levels land one directory
# ABOVE the repo root -- outside the project entirely:
#
#   documented:  cd ..\DWR_Phase_2\Dark_Web_Rises ; py scripts\final_leaderboard.py
#   looked for:  C:\Users\<you>\Documents\Dark_Web_Rises_Phase1\state\phase1_scores.json
#   actual:      C:\Users\<you>\Documents\Dark_Web_Rises\Dark_Web_Rises_Phase1\state\...
#
# So the documented invocation always failed with "No phase 1 scores at ..."
# -- at step 8 of the runbook, with the room waiting for the final scoreboard.
# It only worked if you happened to run it from one directory deeper, which
# nothing tells you to do.
#
# Anchoring to __file__ makes it correct from any working directory, which is
# what DEFAULT_SCORES_OUT below already did.
DEFAULT_PHASE1_SCORES = os.path.normpath(os.path.join(
    _HERE, "..", "..", "..", "Dark_Web_Rises_Phase1", "state", "phase1_scores.json"
))

# Written next to index.html inside the core theme's static directory, which
# CTFd serves as-is. Anchored to this file's location rather than the current
# directory so the script works from anywhere.
DEFAULT_SCORES_OUT = os.path.join(
    _HERE, "..", "CTFd", "themes", "core", "static", "leaderboard", "scores.json",
)


def api_get(base_url: str, path: str, token: str):
    """GET {base_url}/api/v1/{path} and return the parsed `data` payload."""
    url = f"{base_url.rstrip('/')}/api/v1/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in (401, 403):
            raise SystemExit(
                f"CTFd rejected the token ({exc.code}) on {url}.\n"
                "Generate an admin token at Settings -> Access Tokens and set "
                "CTFD_ADMIN_TOKEN."
            ) from exc
        raise SystemExit(f"CTFd returned HTTP {exc.code} for {url}:\n{detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Could not reach CTFd at {url}: {exc.reason}\n"
            "Is the stack up? `docker compose ps`"
        ) from exc

    if not payload.get("success", True):
        raise SystemExit(f"CTFd reported failure for {url}: {payload}")
    return payload


def api_get_paginated(base_url: str, path: str, token: str):
    """Yield every row across CTFd's paginated responses.

    /api/v1/teams returns 50 per page by default. With ~150 qualified teams,
    reading only page 1 would silently drop two thirds of the field.
    """
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        payload = api_get(base_url, f"{path}{sep}page={page}", token)
        rows = payload.get("data", [])
        yield from rows

        meta = (payload.get("meta") or {}).get("pagination") or {}
        nxt = meta.get("next")
        if not nxt or not rows:
            return
        page = nxt


def load_phase1_scores(path: str) -> dict:
    """{team_id:int -> score:float} from final_score.py's phase1_scores.json.

    That file is written at game over and contains only the teams that
    qualified, which is exactly the set the final leaderboard should cover.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        # Look in the other places it plausibly is before giving up. This
        # runs at step 8 of the runbook with the room waiting, so spending a
        # few stat() calls to turn "not found" into "found it over here" is a
        # good trade. Anything found this way is reported, not used silently
        # -- picking up the wrong event's scores without saying so would be
        # worse than failing.
        candidates = [
            os.path.normpath(os.path.join(_HERE, "..", "..", "..",
                                          "Dark_Web_Rises_Phase1", "state",
                                          "phase1_scores.json")),
            os.path.normpath(os.path.join(_HERE, "..", "..", "..",
                                          "Dark_Web_Rises_Phase1",
                                          "phase1_scores.json")),
            os.path.abspath(os.path.join("..", "..", "Dark_Web_Rises_Phase1",
                                         "state", "phase1_scores.json")),
        ]
        found = [c for c in candidates
                 if os.path.exists(c) and os.path.abspath(c) != os.path.abspath(path)]
        hint = ""
        if found:
            hint = ("\n\nIt looks like the file is actually here:\n"
                    + "\n".join(f"  {c}" for c in found)
                    + f"\n\nRe-run with:\n  py scripts/final_leaderboard.py "
                      f"--phase1-scores \"{found[0]}\"")
        raise SystemExit(
            f"No phase 1 scores at {path}\n"
            "This file is written by Phase 1 when the game reaches game over. "
            "If Phase 1 has not finished yet, finish it first -- there is "
            "nothing to combine until then." + hint
        )
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")

    if not isinstance(raw, dict) or not raw:
        raise SystemExit(f"{path} is empty or not a team_id -> score object.")

    return {int(team_id): float(score) for team_id, score in raw.items()}


def detect_account_mode(base_url: str, token: str) -> str:
    """Return "users" or "teams" -- whichever CTFd is configured for.

    Detected rather than assumed. The two modes put the accounts that carry
    our team<id>@example.invalid emails on different endpoints, and querying
    the wrong one returns rows that simply never match, producing a
    leaderboard of zeros with no error anywhere.
    """
    try:
        payload = api_get(base_url, "configs/user_mode", token)
    except SystemExit:
        return "teams"
    value = (payload.get("data") or {}).get("value")
    return "users" if str(value).strip().lower() == "users" else "teams"


def ctfd_scores_by_phase1_id(base_url: str, token: str, mode: str, phase1_ids):
    """Return ({phase1_team_id: ctf_score}, [warnings]).

    Joins the account endpoint (has email) against /api/v1/scoreboard (has
    score) on CTFd's account id, then maps email -> phase 1 team id.

    In users mode each qualified team is a single shared account under
    /api/v1/users; in teams mode it is a team under /api/v1/teams. The
    scoreboard's `account_id` refers to whichever of those is in play.
    """
    warnings = []
    endpoint = "users" if mode == "users" else "teams"

    email_by_ctfd_id = {}
    name_by_ctfd_id = {}

    if mode == "users":
        # /api/v1/users dumps UserSchema(view="user") unconditionally -- the
        # view is hardcoded, so emails are absent even for an admin token
        # (unlike /api/v1/teams, where the view is chosen dynamically).
        # `?view=admin` only widens which rows are returned, not the fields.
        #
        # Emails are still *searchable* by admins, so look each qualified team
        # up by its known address. The mapping then comes from the query
        # itself and never needs to appear in the response.
        for team_id in sorted(phase1_ids):
            email = f"team{team_id}@example.invalid"
            payload = api_get(
                base_url,
                f"users?field=email&q={urllib.parse.quote(email)}",
                token,
            )
            rows = payload.get("data") or []
            if not rows:
                continue  # reported later as "no CTFd account"
            if len(rows) > 1:
                warnings.append(
                    f"{email} matched {len(rows)} accounts; using the first."
                )
            account = rows[0]
            email_by_ctfd_id[account["id"]] = email
            name_by_ctfd_id[account["id"]] = account.get("name")
    else:
        for account in api_get_paginated(base_url, endpoint, token):
            email_by_ctfd_id[account["id"]] = (account.get("email") or "").strip()
            name_by_ctfd_id[account["id"]] = account.get("name")

    if not email_by_ctfd_id:
        raise SystemExit(
            f"No CTFd {endpoint} matched team<id>@example.invalid.\n"
            f"Has the qualified-team CSV been imported at "
            f"Admin -> {endpoint.capitalize()} -> Import CSV?\n"
            f"CTFd is in {mode} mode, so it must be imported there and not "
            f"under the other one."
        )

    # The scoreboard omits teams with no solves, so start everyone at zero and
    # let the scoreboard raise them. Otherwise a qualified team that solved
    # nothing would vanish from the final standings instead of placing last.
    score_by_ctfd_id = {ctfd_id: 0 for ctfd_id in email_by_ctfd_id}
    for entry in api_get(base_url, "scoreboard", token).get("data", []):
        score_by_ctfd_id[entry["account_id"]] = int(entry.get("score") or 0)

    scores = {}
    names = {}
    for ctfd_id, email in email_by_ctfd_id.items():
        match = EMAIL_RE.match(email)
        if not match:
            # In users mode this legitimately catches admin accounts, so it is
            # informational rather than a problem.
            warnings.append(
                f"CTFd account {name_by_ctfd_id.get(ctfd_id)!r} (id={ctfd_id}, "
                f"email={email!r}) does not match team<id>@example.invalid -- "
                "not from the Phase 1 import, ignoring."
            )
            continue
        phase1_id = int(match.group(1))
        if phase1_id in scores:
            warnings.append(
                f"Two CTFd teams map to phase 1 team {phase1_id}; keeping the "
                "higher score."
            )
            scores[phase1_id] = max(scores[phase1_id], score_by_ctfd_id[ctfd_id])
        else:
            scores[phase1_id] = score_by_ctfd_id[ctfd_id]
        names[phase1_id] = name_by_ctfd_id.get(ctfd_id) or f"Team {phase1_id + 1}"

    return scores, names, warnings


def total_challenge_points(base_url: str, token: str) -> int:
    """Sum of every challenge's value, including hidden and locked ones.

    Computed rather than hardcoded: challenges get added and repointed right up
    to the event, and a stale constant silently skews the normalisation.

    `?view=admin` is essential. Without it CTFd's list endpoint returns only
    challenges in the visible state -- so a challenge hidden *after* the CTF
    (or hidden mid-event once solved) drops out of the total, the denominator
    shrinks, and normalised scores climb above 1.0.
    """
    total = 0
    for ch in api_get_paginated(base_url, "challenges?view=admin", token):
        total += int(ch.get("value") or 0)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--ctfd-url",
        default=os.getenv("CTFD_URL", "http://localhost:8080"),
        help="Base CTFd URL, e.g. http://localhost:8080 (default: %(default)s)",
    )
    ap.add_argument(
        "--token",
        default=os.getenv("CTFD_ADMIN_TOKEN"),
        help="CTFd admin API token. Prefer the CTFD_ADMIN_TOKEN env var so it "
             "stays out of your shell history.",
    )
    ap.add_argument(
        "--phase1-scores",
        default=os.getenv("PHASE1_SCORES", DEFAULT_PHASE1_SCORES),
        help="phase1_scores.json from Phase 1 (default: %(default)s)",
    )
    ap.add_argument(
        "--max-phase1",
        type=float,
        default=float(os.getenv("MAX_POINTS_PHASE1", "500")),
        help="Maximum attainable Phase 1 score (default: %(default)s)",
    )
    ap.add_argument(
        "--max-ctf",
        type=float,
        default=None,
        help="Maximum attainable CTF score. Default: summed from CTFd's "
             "challenges, which is safer than a hardcoded constant.",
    )
    ap.add_argument(
        "--out",
        default=os.getenv("SCORES_OUTPUT_PATH", DEFAULT_SCORES_OUT),
        help="Where to write the combined leaderboard. Defaults to sitting "
             "next to the leaderboard page CTFd serves, so the page finds it "
             "without any copying. (default: %(default)s)",
    )
    args = ap.parse_args()

    if not args.token:
        raise SystemExit(
            "No CTFd admin token. Create one at Settings -> Access Tokens, then:\n"
            '  $env:CTFD_ADMIN_TOKEN = "ctfd_xxxxx"   (PowerShell)\n'
            "  set CTFD_ADMIN_TOKEN=ctfd_xxxxx        (cmd.exe)\n"
            "  export CTFD_ADMIN_TOKEN=ctfd_xxxxx     (bash)\n"
            "\n"
            "Note: in PowerShell, `set` is an alias for Set-Variable and does\n"
            "NOT set an environment variable -- it fails silently, and you end\n"
            "up back here. Use the $env: form.\n"
            "\n"
            "Or pass it directly:  py final_leaderboard.py --token ctfd_xxxxx"
        )

    phase1 = load_phase1_scores(args.phase1_scores)
    mode = detect_account_mode(args.ctfd_url, args.token)
    ctf, names, warnings = ctfd_scores_by_phase1_id(
        args.ctfd_url, args.token, mode, phase1.keys()
    )

    max_ctf = args.max_ctf if args.max_ctf else total_challenge_points(
        args.ctfd_url, args.token
    )
    if not max_ctf:
        raise SystemExit(
            "Total challenge points came to 0. Pass --max-ctf explicitly."
        )
    if args.max_phase1 <= 0:
        raise SystemExit("--max-phase1 must be greater than 0.")

    # A normalised score above 1.0 means the denominator is wrong -- the scale
    # is meant to be 0..100 and this pushes teams past it. Rather than emit
    # nonsense, raise the maximum to what was demonstrably attainable and say
    # so loudly.
    top_ctf = max(ctf.values(), default=0)
    if top_ctf > max_ctf:
        warnings.append(
            f"a team scored {top_ctf} in the CTF but the challenge total came "
            f"to only {max_ctf}. Using {top_ctf} as the maximum so scores stay "
            f"within 0-100. Usually means challenges were deleted or hidden "
            f"after being solved; pass --max-ctf to set it explicitly."
        )
        max_ctf = top_ctf

    top_phase1 = max(phase1.values(), default=0)
    if top_phase1 > args.max_phase1:
        warnings.append(
            f"a team scored {top_phase1:g} in phase 1 but --max-phase1 is "
            f"{args.max_phase1:g}. Using {top_phase1:g} instead."
        )
        args.max_phase1 = top_phase1

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    missing = sorted(set(phase1) - set(ctf))
    if missing:
        print(
            f"warning: {len(missing)} qualified team(s) have no CTFd account and "
            f"score 0 for phase 2: {missing[:10]}"
            f"{' ...' if len(missing) > 10 else ''}",
            file=sys.stderr,
        )

    rows = []
    for team_id, phase1_raw in phase1.items():
        ctf_raw = ctf.get(team_id, 0)
        n1 = phase1_raw / args.max_phase1
        n2 = ctf_raw / max_ctf
        rows.append(
            {
                "team_id": team_id,
                "team": names.get(team_id) or f"Team {team_id + 1}",
                "phase1_raw": round(phase1_raw, 2),
                "phase1_normalized": round(n1, 4),
                "ctf_raw": ctf_raw,
                "ctf_normalized": round(n2, 4),
                "combined": round((n1 + n2) * 50, 2),
            }
        )

    rows.sort(key=lambda r: (-r["combined"], r["team_id"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    # The three arrays below are what index.html renders. It ranks by array
    # order (index + 1), so each is sorted descending on its own metric --
    # the CTF board is not in combined order.
    def by(key):
        return sorted(rows, key=lambda r: (-r[key], r["team_id"]))

    export = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "max_points_phase1": args.max_phase1,
        "max_points_ctf": max_ctf,
        "teams_ranked": len(rows),
        "round1": [
            {
                "team": r["team"],
                "raw_score": r["phase1_raw"],
                "normalized": r["phase1_normalized"],
            }
            for r in by("phase1_raw")
        ],
        "ctf": [
            {
                "team": r["team"],
                "raw_score": r["ctf_raw"],
                "normalized": r["ctf_normalized"],
            }
            for r in by("ctf_raw")
        ],
        "combined": [{"team": r["team"], "score": r["combined"]} for r in rows],
        # Full detail, unused by the page but handy for checking the maths.
        "leaderboard": rows,
    }

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(export, fh, indent=2)
        fh.write("\n")

    print(f"Combined {len(rows)} teams. Phase 1 max {args.max_phase1:g}, "
          f"CTF max {max_ctf:g}.")
    print(f"Wrote {out_path}")
    print("\nTop 10 (combined):")
    for row in rows[:10]:
        print(
            f"  {row['rank']:>3}. {row['team']:<14} "
            f"{row['combined']:>6.2f}   "
            f"(phase1 {row['phase1_raw']:g}, ctf {row['ctf_raw']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
