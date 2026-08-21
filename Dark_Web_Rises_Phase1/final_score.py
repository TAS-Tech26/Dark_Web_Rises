"""Phase 2 qualification: work out who advances, and produce the two files
the CTFd side needs.

Outputs:
  * a CSV of the qualified teams -- ``name,email,password``, one row per team,
    which is what CTFd's bulk user import expects;
  * a JSON map of ``team_id -> phase 1 score`` for those same teams, so their
    round 1 points can be carried over.

Passwords are generated here, one per team, and are also handed back to the
caller so the game server can show each team its own password on the results
screen. Re-running against an existing CSV keeps the passwords already in it:
once a CSV has been imported into CTFd, regenerating would invalidate every
account.
"""

import argparse
import csv
import json
import math
import secrets
import sys
from pathlib import Path

# Ambiguous glyphs (0/O, 1/l/I) are left out: these get read off a screen and
# retyped on a phone at an event, and "was that an l or a 1" costs support time.
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_LENGTH = 12

CSV_FIELDNAMES = ["name", "email", "password"]

# The address is never used -- CTFd just requires the column to be present and
# unique per user. .invalid is reserved by RFC 2606 precisely for this, so a
# stray mail cannot escape to a real domain.
EMAIL_DOMAIN = "example.invalid"


def load_json(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def team_scores(game_state: dict, round_no) -> dict:
    """Return {team_id (int): score (float)} for the given round."""
    rounds = game_state.get("rounds", {})
    if round_no is None:
        round_no = game_state.get("last_completed_round")
    if round_no is None:
        raise SystemExit("No round specified and game_state has no 'last_completed_round'.")

    # round keys may be str or int depending on how the file was written
    raw = rounds.get(str(round_no), rounds.get(round_no))
    if raw is None:
        raise SystemExit(
            f"Round {round_no} not found in game_state. Available: {sorted(rounds)}"
        )
    return {int(team_id): float(score) for team_id, score in raw.items()}


def qualifying_teams(scores: dict) -> tuple[list, float | None]:
    """Top 50% of scored teams (ceil), including anyone tied at the cutoff."""
    if not scores:
        return [], None

    # highest score first; team_id ascending as a stable secondary key
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    cutoff_index = math.ceil(len(ranked) / 2)
    cutoff_score = ranked[cutoff_index - 1][1]

    qualified = [team_id for team_id, score in ranked if score >= cutoff_score]
    return qualified, cutoff_score


def team_email(team_id: int) -> str:
    """Deterministic per-team address. Doubles as the key used to match a team
    back to its row when reusing passwords from an existing CSV."""
    return f"team{team_id}@{EMAIL_DOMAIN}"


def default_team_name(team_id: int) -> str:
    """Mirrors game.py's fallback (TEAM_NAME_<i>, else "Team <i+1>")."""
    return f"Team {team_id + 1}"


def generate_password(taken=(), length: int = PASSWORD_LENGTH) -> str:
    """A fresh password, guaranteed not to collide with `taken`.

    secrets, not random: these are credentials, and random's Mersenne Twister
    is seeded predictably enough that a whole event's passwords could be
    reconstructed from a couple of leaked ones.
    """
    taken = set(taken)
    while True:
        candidate = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if candidate not in taken:
            return candidate


def read_existing_passwords(csv_path: Path) -> dict:
    """Return {email: password} from a previously written CSV.

    Missing or unreadable file -> empty mapping. A crash mid-event must not be
    made worse by refusing to rebuild the roster.
    """
    if not csv_path.exists():
        return {}
    try:
        with csv_path.open(newline="", encoding="utf-8") as fh:
            return {
                row["email"]: row["password"]
                for row in csv.DictReader(fh)
                if row.get("email") and row.get("password")
            }
    except (OSError, csv.Error, KeyError):
        return {}


def build_phase2_roster(csv_path, scores_path, team_names=None,
                        game_state_path=None, round_no=None, scores=None):
    """Write the phase 2 CSV and the phase 1 score map.

    Returns (qualified_team_ids, cutoff_score, {team_id: password}).

    ``scores`` lets a caller supply its own {team_id: score} mapping -- the
    game server passes the *cumulative* final standings, which is what the
    phase 2 cut is made on. Left as None (the CLI case) the scores are read
    from ``game_state_path`` for a single round.

    ``team_names`` is {team_id: name}; anything missing falls back to
    "Team <n>" so a name that never made it into the environment still
    produces a usable row.
    """
    csv_path, scores_path = Path(csv_path), Path(scores_path)
    team_names = team_names or {}

    if scores is None:
        game_state = load_json(Path(game_state_path))
        if round_no is None:
            round_no = game_state.get("last_completed_round")
        scores = team_scores(game_state, round_no)
    else:
        scores = {int(team_id): float(score) for team_id, score in scores.items()}

    qualified, cutoff = qualifying_teams(scores)

    # Keep any password already issued to a team, so a CSV that has already
    # been imported into CTFd stays valid across a restart or a second
    # game-over. Only teams without one get a freshly generated password.
    existing = read_existing_passwords(csv_path)
    passwords = {}
    issued = set(existing.values())
    for team_id in qualified:
        password = existing.get(team_email(team_id))
        if not password:
            password = generate_password(taken=issued)
            issued.add(password)
        passwords[team_id] = password

    rows = [
        {
            "name": team_names.get(team_id) or default_team_name(team_id),
            "email": team_email(team_id),
            "password": passwords[team_id],
        }
        for team_id in qualified
    ]

    # newline="" per the csv module's contract -- without it every row is
    # followed by a blank line on Windows, which some importers reject.
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    # Phase 1 scores for the teams that advanced, so their round 1 points can
    # be carried into CTFd. JSON object keys are strings by definition.
    carried = {str(team_id): scores[team_id] for team_id in qualified}
    scores_path.write_text(json.dumps(carried, indent=2) + "\n", encoding="utf-8")

    return qualified, cutoff, passwords


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game-state", type=Path, default=here / "game_state.json")
    ap.add_argument("--out", type=Path, default=here / "roster_cache_phase2.csv",
                    help="CSV of qualified teams (name,email,password)")
    ap.add_argument("--scores-out", type=Path, default=here / "phase1_scores.json",
                    help="JSON map of team_id -> phase 1 score for those teams")
    ap.add_argument("--round", type=int, default=None,
                    help="Round to score on (default: last_completed_round)")
    args = ap.parse_args()

    if not args.game_state.exists():
        raise SystemExit(f"Missing input file: {args.game_state}")

    game_state = load_json(args.game_state)
    round_no = args.round if args.round is not None else game_state.get("last_completed_round")
    scores = team_scores(game_state, round_no)

    qualified, cutoff, passwords = build_phase2_roster(
        args.out, args.scores_out, game_state_path=args.game_state, round_no=round_no
    )

    print(f"Round {round_no}: {len(scores)} teams scored, "
          f"{len(qualified)} advance (cutoff {cutoff}).")
    print(f"Qualifying teams: {sorted(qualified)}")
    print(f"Wrote {len(passwords)} rows to {args.out}")
    print(f"Wrote phase 1 scores to {args.scores_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
