"""The participant CSV becomes a roster, or says exactly why not.

    python tools/test_csv_roster.py

No network, no database, no cost.

Why this is worth 20 checks
---------------------------
This file is the single point where 700 people either can or cannot log in.
Every failure here is silent at 09:00 and obvious at 10:00, and by then the
room is full and nobody knows which six people are affected.

The CSV in this test is deliberately messy, in the ways registration exports
are actually messy: a UTF-8 BOM from Excel, a column called "Mobile Number"
rather than "phone", phone numbers written five different ways, two people
with the same name, a row from a different event, and a row with no phone at
all. If the parser only works on a clean file it does not work.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED, FAILED = [], []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    return bool(condition)


# Deliberately awkward. Note the BOM, the header names, and the phone formats.
MESSY_CSV = (
    "﻿Timestamp,Full Name,Mobile Number,Team Name,Event Name\n"
    "2026-08-01,Aarav Sharma,+91 98765 43210,Nightcrawlers,Dark Web Rises\n"
    "2026-08-01,Priya Nair,9876543211,Nightcrawlers,Dark Web Rises\n"
    "2026-08-01,Rohan Das,(98765) 43212,Nightcrawlers,dark-web-rises\n"
    "2026-08-01,Isha Menon,98765-43213,Nightcrawlers,DARK WEB RISES\n"
    "2026-08-01,Vikram Rao,918765432140,Zero Day,Dark Web Rises\n"
    "2026-08-01,Aarav Sharma,9123456789,Zero Day,Dark Web Rises\n"
    "2026-08-01,Sneha Iyer,  9123456790  ,Zero Day,Dark Web Rises\n"
    "2026-08-01,Karthik R,9123456791,zero day,Dark Web Rises\n"
    "2026-08-01,Someone Else,9999999999,Other Team,Capture The Flag\n"
)

NO_PHONE_CSV = (
    "Full Name,Mobile Number,Team Name,Event Name\n"
    "Valid Person,9000000001,Alpha,Dark Web Rises\n"
    "No Phone Person,,Alpha,Dark Web Rises\n"
    "Dash Person,N/A,Alpha,Dark Web Rises\n"
)


def write(tmp, name, text):
    path = os.path.join(tmp, name)
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="dwr-csv-")
    os.environ.pop("ROSTER_CSV_NAME_COLUMN", None)
    os.environ.pop("ROSTER_CSV_PHONE_COLUMN", None)
    os.environ.pop("ROSTER_CSV_TEAM_COLUMN", None)
    os.environ.pop("ROSTER_CSV_EVENT_COLUMN", None)

    import csv_roster

    print("=" * 70)
    print("CSV ROSTER")
    print("=" * 70)

    print("\n-- 1. a messy but valid export ---------------------------------------")
    path = write(tmp, "messy.csv", MESSY_CSV)
    rows, header = csv_roster.read_csv_rows(path)
    check("the Excel BOM did not corrupt the first column name",
          header and header[0] == "Timestamp", repr(header[0] if header else None))

    result = csv_roster.translate_csv(rows, header, max_members_per_team=4)
    rep = result.report

    check("columns were matched by alias, not by exact name",
          rep.resolved_columns["name"] == "Full Name"
          and rep.resolved_columns["phone"] == "Mobile Number"
          and rep.resolved_columns["team"] == "Team Name",
          str(rep.resolved_columns))

    print("\n-- 2. the event filter -----------------------------------------------")
    check("the row from another event was excluded",
          rep.rows_for_event == 8 and rep.rows_read == 9,
          f"{rep.rows_for_event} of {rep.rows_read} rows")
    check("nobody from 'Capture The Flag' is in the roster",
          all("Someone Else" not in v[0] for v in result.player_data.values()))
    check("the slug spelling matched too",
          any(v[0] == "Rohan Das" for v in result.player_data.values()),
          "'dark-web-rises' is the same event as 'Dark Web Rises'")
    check("matching ignores case",
          any(v[0] == "Isha Menon" for v in result.player_data.values()),
          "'DARK WEB RISES'")

    print("\n-- 3. username is the name, password is the phone digits -------------")
    by_name = {v[0]: v for v in result.player_data.values()}
    check("'+91 98765 43210' becomes '919876543210'",
          by_name["Aarav Sharma"][1] == "919876543210", by_name["Aarav Sharma"][1])
    check("'(98765) 43212' becomes '9876543212'",
          by_name["Rohan Das"][1] == "9876543212", by_name["Rohan Das"][1])
    check("'98765-43213' becomes '9876543213'",
          by_name["Isha Menon"][1] == "9876543213", by_name["Isha Menon"][1])
    check("surrounding whitespace is stripped",
          by_name["Sneha Iyer"][1] == "9123456790", repr(by_name["Sneha Iyer"][1]))

    print("\n-- 4. teams ----------------------------------------------------------")
    check("two teams were found", result.team_count == 2,
          f"{result.team_count}: {list(result.team_names.values())}")
    check("'zero day' and 'Zero Day' are ONE team",
          sum(1 for v in result.player_data.values() if v[2] == 1) == 4,
          "case and spacing in the team column must not split a team")
    check("team ids are numbered by first appearance",
          result.team_names[0] == "Nightcrawlers" and result.team_names[1] == "Zero Day",
          str(result.team_names))
    check("no team was flagged over or under size",
          not rep.oversized_teams and not rep.undersized_teams)

    print("\n-- 5. duplicate names ------------------------------------------------")
    # Two different people are both called Aarav Sharma. GameServer refuses to
    # start on a duplicate username, so one has to be renamed -- and the
    # renamed player types their username to log in, so this cannot be quiet.
    check("the second 'Aarav Sharma' was renamed, not dropped",
          "Aarav Sharma 2" in by_name, sorted(n for n in by_name if "Aarav" in n))
    check("the rename is reported so the player can be told",
          any("Aarav Sharma" in orig for orig, _ in rep.renamed_duplicates),
          str(rep.renamed_duplicates))
    check("the two share a name but NOT a password",
          by_name["Aarav Sharma"][1] != by_name["Aarav Sharma 2"][1],
          "this is why the password is the phone number")

    print("\n-- 6. totals ---------------------------------------------------------")
    check("8 players emitted", rep.players_emitted == 8, str(rep.players_emitted))
    check("player ids are unique and contiguous",
          sorted(result.player_data) == list(range(8)))
    check("nothing fatal in a valid file", not rep.fatal, str(rep.errors))

    print("\n-- 7. a row that cannot produce a login is FATAL ----------------------")
    path2 = write(tmp, "nophone.csv", NO_PHONE_CSV)
    rows2, header2 = csv_roster.read_csv_rows(path2)
    result2 = csv_roster.translate_csv(rows2, header2, max_members_per_team=4)
    check("a blank phone is an error, not a shrug",
          result2.report.fatal, str(result2.report.errors))
    check("'N/A' counts as no phone too",
          len(result2.report.missing_phone) == 2,
          str(result2.report.missing_phone))
    check("the error names the person AND the spreadsheet row",
          any("No Phone Person" in e and "row 3" in e for e in result2.report.errors),
          next((e for e in result2.report.errors), ""))

    print("\n-- 8. a missing column explains itself -------------------------------")
    path3 = write(tmp, "noteam.csv", "Full Name,Mobile Number\nA,900\n")
    rows3, header3 = csv_roster.read_csv_rows(path3)
    try:
        csv_roster.translate_csv(rows3, header3)
        check("missing team column is refused", False, "no exception raised")
    except ValueError as exc:
        check("missing team column is refused", True)
        check("the message shows the actual header and how to override",
              "Full Name" in str(exc) and "ROSTER_CSV_TEAM_COLUMN" in str(exc),
              str(exc).splitlines()[0][:60])

    print("\n-- 9. an explicit column override wins -------------------------------")
    os.environ["ROSTER_CSV_TEAM_COLUMN"] = "Squad Label"
    path4 = write(tmp, "odd.csv",
                  "Full Name,Mobile Number,Squad Label,Event Name\n"
                  "Z Person,9000000009,Ghosts,Dark Web Rises\n")
    rows4, header4 = csv_roster.read_csv_rows(path4)
    result4 = csv_roster.translate_csv(rows4, header4)
    check("a column no alias would find is used when named explicitly",
          result4.team_count == 1 and result4.team_names[0] == "Ghosts")
    os.environ["ROSTER_CSV_TEAM_COLUMN"] = "Does Not Exist"
    try:
        csv_roster.translate_csv(rows4, header4)
        check("an override naming a missing column is an error", False)
    except ValueError:
        check("an override naming a missing column is an error", True,
              "silently falling back would hide a typo someone made on purpose")
    os.environ.pop("ROSTER_CSV_TEAM_COLUMN", None)

    print("\n" + "=" * 70)
    print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"    FAILED: {line}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
