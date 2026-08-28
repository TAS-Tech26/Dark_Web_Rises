"""Build roster_cache.json from the registration export.

    python roster_csv.py                                  # defaults, see below
    python roster_csv.py --xlsx export.xlsx --out roster_cache.json
    python roster_csv.py --event "Dark Web Rises" --max-members 4

Credentials, unchanged from the original:

    username = the member's name exactly as registered
    password = firstname@last5digits      e.g.  faiza@63220

What this adds over the first version
-------------------------------------
Three things stop GameServer booting AT ALL, and none of them were checked.
Each one is a `raise ValueError` in models/server.py, which means the app
does not start -- not "starts degraded", not "logs a warning". Verified
against the real server, not read off the source:

    duplicate username (case-insensitive)  -> "duplicate username 'FAIZA HANA'"
    a team over MAX_MEMBERS_PER_TEAM       -> "team 0 would exceed its capacity"
    a member whose name matches an admin   -> "duplicate username 'admin'"

At 108 people the first is unlikely. At 700 it is close to certain, and the
symptom is the server refusing to start on the morning of the event with a
message about one name in a spreadsheet of hundreds.

So this script resolves what it safely can, refuses what it cannot, and
prints exactly what the server will do with the file before you ship it.

Member columns are discovered from the header rather than assumed. The real
export has Member 1..10; the original read Member 1..8, so a nine- or
ten-person registration would have lost two people silently.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

try:
    import openpyxl
except ImportError:
    sys.exit(
        "openpyxl is not installed.\n"
        "  pip install openpyxl\n"
        "If you are running this INSIDE the dwr container, it is not in "
        "requirements.txt -- run it on the host instead."
    )

DEFAULT_EVENT = "Dark Web Rises"


def normalise_username(raw):
    """Exactly what models/server.py does when it checks for duplicates.

    Kept identical on purpose. If these two ever disagree, this script
    approves a roster the server then refuses, which is the worst possible
    place for the disagreement to surface.
    """
    if not isinstance(raw, str):
        return None
    return raw.strip().casefold()


def extract_first_name(full_name: str) -> str:
    """First token, up to a space or full stop, lowercased."""
    parts = re.split(r"[\s\.]", full_name.strip())
    first = next((p for p in parts if p), "")
    return first.lower()


def extract_last_5_digits(phone_str) -> str:
    digits = re.sub(r"\D", "", str(phone_str or ""))
    if len(digits) >= 5:
        return digits[-5:]
    return digits.zfill(5)


def phone_of(value) -> str:
    """Excel stores phone numbers as floats often enough to matter."""
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value))
    return str(value).strip()


def member_column_pairs(header_map):
    """Every (name_col, phone_col) pair the sheet actually has.

    Discovered, not assumed. The live export carries Member 1..10; reading a
    hardcoded 1..8 drops two people per oversized team and says nothing.
    """
    pairs = []
    for key in header_map:
        m = re.fullmatch(r"Member\s+(\d+)\s+Name", str(key).strip(), re.I)
        if m:
            n = int(m.group(1))
            phone_key = next(
                (k for k in header_map
                 if re.fullmatch(rf"Member\s+{n}\s+Phone", str(k).strip(), re.I)),
                None,
            )
            pairs.append((n, key, phone_key))
    pairs.sort()
    return pairs


def admin_usernames():
    """Names taken by DWR_ADMINS, which players may not collide with."""
    raw = os.getenv("DWR_ADMINS", "")
    out = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        out.add(normalise_username(chunk.split(":", 1)[0]))
    return {u for u in out if u}


def build(xlsx_path, event, max_members, allow_oversized=False):
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    sheet = wb.active
    rows = sheet.iter_rows(values_only=True)

    headers = [str(c).strip() if c is not None else None for c in next(rows)]
    header_map = {h: i for i, h in enumerate(headers) if h}

    if "Event" not in header_map:
        raise SystemExit(
            "No 'Event' column in the sheet. Columns are:\n  "
            + "\n  ".join(repr(h) for h in headers if h)
        )

    pairs = member_column_pairs(header_map)
    if not pairs:
        raise SystemExit("No 'Member N Name' columns found.")

    event_col = header_map["Event"]
    taken = dict()          # normalised username -> the name that claimed it
    admins = admin_usernames()

    players, teams = [], []
    renamed, no_phone, oversized, other_events = [], [], [], dict()
    admin_clashes = []
    player_id = 1
    team_id = 0

    for row_no, row in enumerate(rows, start=2):
        raw_event = str(row[event_col]).strip() if row[event_col] else ""
        if raw_event.strip().casefold() != event.strip().casefold():
            other_events[raw_event] = other_events.get(raw_event, 0) + 1
            continue

        members = []
        for n, name_col, phone_col in pairs:
            raw_name = row[header_map[name_col]]
            if not raw_name or not str(raw_name).strip():
                continue
            name = str(raw_name).strip()
            phone = phone_of(row[header_map[phone_col]]) if phone_col else ""
            members.append((row_no, n, name, phone))

        if not members:
            continue

        if len(members) > max_members:
            oversized.append((row_no, len(members), [m[2] for m in members]))
            if not allow_oversized:
                # Do not truncate silently. Truncating drops real people who
                # registered, and the only sign would be a short team.
                continue

        for row_number, slot, name, phone in members[:max_members]:
            key = normalise_username(name)

            if key in admins:
                admin_clashes.append((row_number, name))
                continue

            if not phone or not re.sub(r"\D", "", phone):
                no_phone.append((row_number, name))

            username = name
            if key in taken:
                # The server refuses to start on a duplicate, so one of them
                # has to change. Suffix deterministically and shout: the
                # renamed player types the SUFFIXED name to log in.
                suffix = 2
                while normalise_username(f"{name} {suffix}") in taken:
                    suffix += 1
                username = f"{name} {suffix}"
                renamed.append((row_number, name, username))
                key = normalise_username(username)

            taken[key] = username
            players.append({
                "id": player_id,
                "username": username,
                "password": f"{extract_first_name(name)}@{extract_last_5_digits(phone)}",
                "team_id": team_id,
            })
            player_id += 1

        teams.append(len([p for p in players if p["team_id"] == team_id]))
        team_id += 1

    return {
        "payload": {
            "version": 1,
            "fetched_at": time.time(),
            "team_count": team_id,
            "players": players,
        },
        "team_sizes": teams,
        "renamed": renamed,
        "no_phone": no_phone,
        "oversized": oversized,
        "admin_clashes": admin_clashes,
        "other_events": other_events,
        "member_slots": [p[0] for p in pairs],
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", default="active_registrations_2026-08-27.xlsx")
    ap.add_argument("--out", default="roster_cache.json")
    ap.add_argument("--event", default=os.getenv("DWR_EVENT_NAME", DEFAULT_EVENT))
    ap.add_argument("--max-members", type=int,
                    default=int(os.getenv("MAX_MEMBERS_PER_TEAM", "4")))
    ap.add_argument("--allow-oversized", action="store_true",
                    help="keep the first N members of an oversized team instead "
                         "of skipping the team. The extras are dropped either "
                         "way -- this just chooses which failure you get.")
    args = ap.parse_args()

    if not os.path.exists(args.xlsx):
        raise SystemExit(f"No such file: {args.xlsx}")

    r = build(args.xlsx, args.event, args.max_members, args.allow_oversized)
    payload = r["payload"]

    print("=" * 68)
    print(f"  {args.event}")
    print("=" * 68)
    print(f"  member columns in sheet : Member {min(r['member_slots'])}"
          f"..{max(r['member_slots'])}")
    print(f"  teams                   : {payload['team_count']}")
    print(f"  players                 : {len(payload['players'])}")
    sizes = {}
    for s in r["team_sizes"]:
        sizes[s] = sizes.get(s, 0) + 1
    print(f"  team sizes              : {sizes}  (size: count)")
    print(f"  other events skipped    : {sum(r['other_events'].values())} rows "
          f"across {len(r['other_events'])} events")

    problems = False

    if r["oversized"]:
        problems = True
        print(f"\n  !! {len(r['oversized'])} team(s) over the cap of {args.max_members} "
              f"-- SKIPPED ENTIRELY (--allow-oversized keeps the first {args.max_members}):")
        for row_no, n, names in r["oversized"][:10]:
            print(f"       row {row_no}: {n} members -- {', '.join(names)}")
        print("     GameServer refuses to start on an oversized team, so these")
        print("     people are not in the roster. Fix the sheet or split the team.")

    if r["admin_clashes"]:
        problems = True
        print(f"\n  !! {len(r['admin_clashes'])} member name(s) collide with a DWR_ADMINS "
              "login and were EXCLUDED:")
        for row_no, name in r["admin_clashes"]:
            print(f"       row {row_no}: {name!r}")
        print("     GameServer refuses to start when a player and an admin share")
        print("     a name. Rename the admin in .env, or the player in the sheet.")

    if r["renamed"]:
        problems = True
        print(f"\n  !! {len(r['renamed'])} duplicate name(s) renamed -- TELL THESE PLAYERS:")
        for row_no, orig, became in r["renamed"]:
            print(f"       row {row_no}: {orig!r} logs in as {became!r}")
        print("     They type the SUFFIXED name. Their passwords already differ,")
        print("     because the password comes from their own phone number.")

    if r["no_phone"]:
        problems = True
        print(f"\n  !! {len(r['no_phone'])} member(s) have no phone -- password ends @00000:")
        for row_no, name in r["no_phone"][:10]:
            print(f"       row {row_no}: {name!r}")
        print("     Guessable, and identical for everyone in this list. Fill the")
        print("     sheet in, or hand these people a password personally.")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print()
    if not problems:
        print(f"  No problems. GameServer will accept this file.")
    else:
        print(f"  Written anyway -- read the warnings above before shipping it.")
    print(f"  -> {args.out}")
    print()
    print("  Verify against the expectations for this event:")
    print(f"    python tools/roster_build.py check --expect-teams {payload['team_count']} "
          f"--expect-players {len(payload['players'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
