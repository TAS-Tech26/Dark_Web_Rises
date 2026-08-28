"""Build the Phase 1 roster from a participant CSV.

    { player_id: [username, password, team_id] }

Replaces the Supabase path for production. Same output shape as
`reader.translate_roster`, so `roster.py`, the cache, the size guard and
GameServer are all unchanged.

The rules, as specified
-----------------------
  * filter to rows whose event is "Dark Web Rises" (DWR_EVENT_NAME)
  * username  = the participant's name, exactly as written
  * password  = their phone number, digits only
  * teams     = grouped by the team column, numbered by first appearance

Why so much validation
----------------------
This file is the single point where 700 people either can or cannot log in,
and every failure mode here is silent at 09:00 and obvious at 10:00. A
missing phone is not a warning, it is one person standing at the help desk.
So every row that cannot produce a working login is counted, named, and
reported -- and `roster_build.py check` refuses a roster whose numbers do
not match the event.

Column names are not guessed once and hardcoded. Registration exports name
things differently every time ("Phone", "Mobile Number", "Contact No"), so
each field accepts a list of aliases and can be overridden outright from the
environment when the export does something new.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_EVENT_NAME = "Dark Web Rises"
DEFAULT_EVENT_SLUG = "dark-web-rises"

# Matched case-insensitively, ignoring spaces, underscores and punctuation,
# so "Full Name", "full_name" and "FULLNAME" all hit the same alias.
NAME_ALIASES = (
    "name", "fullname", "full name", "participant", "participantname",
    "student", "studentname", "membername", "member", "attendee",
    "attendeename", "yourname",
)
PHONE_ALIASES = (
    "phone", "phonenumber", "phoneno", "phonenum", "mobile", "mobilenumber",
    "mobileno", "contact", "contactnumber", "contactno", "whatsapp",
    "whatsappnumber", "cell", "cellphone",
)
TEAM_ALIASES = (
    "team", "teamname", "teamid", "teamno", "teamnumber", "teamcode",
    "group", "groupname", "squad", "teamleader", "leadername",
)
EVENT_ALIASES = (
    "event", "eventname", "eventslug", "eventtitle", "competition",
)


def _norm_header(value: str) -> str:
    """'Phone Number ' -> 'phonenumber'. Alias matching happens on this."""
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def _norm_value(value) -> str:
    return str(value or "").strip()


def digits_only(phone) -> str:
    """The password. '+91 98765-43210' -> '919876543210'.

    Everything that is not a digit goes, including a leading +. The player
    types the number they gave at registration with nothing in between,
    which is the only rule that survives being explained once over a PA
    system to 700 people.
    """
    return re.sub(r"\D", "", str(phone or ""))


@dataclass
class CsvRosterReport:
    """What the file contained, and everything that could not be used."""
    rows_read: int = 0
    rows_for_event: int = 0
    players_emitted: int = 0
    teams_seen: int = 0

    header: list = field(default_factory=list)
    resolved_columns: dict = field(default_factory=dict)

    # Fatal: these rows produce no login at all.
    missing_name: list = field(default_factory=list)      # row numbers
    missing_phone: list = field(default_factory=list)     # (row, name)
    missing_team: list = field(default_factory=list)      # (row, name)

    # Non-fatal, but someone needs to know.
    renamed_duplicates: list = field(default_factory=list)   # (original, became)
    shared_phone: list = field(default_factory=list)         # (digits, [names])
    oversized_teams: list = field(default_factory=list)      # (team, n, cap)
    undersized_teams: list = field(default_factory=list)     # (team, n)
    no_event_column: bool = False

    @property
    def errors(self) -> list:
        return (
            [f"row {r}: no name" for r in self.missing_name]
            + [f"row {r}: {n!r} has no phone number, so no password"
               for r, n in self.missing_phone]
            + [f"row {r}: {n!r} has no team" for r, n in self.missing_team]
        )

    @property
    def warnings(self) -> list:
        out = []
        if self.no_event_column:
            out.append(
                "no event column found -- EVERY row was treated as part of this "
                "event. If the file covers more than one event, the roster is wrong."
            )
        out += [
            f"duplicate name {orig!r} logs in as {became!r} -- TELL THAT PLAYER"
            for orig, became in self.renamed_duplicates
        ]
        out += [
            f"phone {d} is shared by {names} -- they share a password"
            for d, names in self.shared_phone
        ]
        out += [f"team {t!r} has {n} members, over the cap of {cap}"
                for t, n, cap in self.oversized_teams]
        out += [f"team {t!r} has only {n} member(s)" for t, n in self.undersized_teams]
        return out

    @property
    def fatal(self) -> bool:
        return bool(self.missing_name or self.missing_phone or self.missing_team)

    def summary(self) -> str:
        lines = [
            f"CSV roster: {self.rows_read} rows read, {self.rows_for_event} for this "
            f"event, {self.players_emitted} players across {self.teams_seen} teams.",
            f"  columns: " + ", ".join(
                f"{k}={v!r}" for k, v in self.resolved_columns.items()
            ),
        ]
        if self.errors:
            lines.append(f"  {len(self.errors)} ERROR(S) -- these people cannot log in:")
            lines += [f"    {e}" for e in self.errors[:25]]
            if len(self.errors) > 25:
                lines.append(f"    ... and {len(self.errors) - 25} more")
        if self.warnings:
            lines.append(f"  {len(self.warnings)} warning(s):")
            lines += [f"    {w}" for w in self.warnings[:25]]
            if len(self.warnings) > 25:
                lines.append(f"    ... and {len(self.warnings) - 25} more")
        return "\n".join(lines)


@dataclass
class TranslatedCsvRoster:
    player_data: dict
    team_count: int
    team_names: dict           # team_id -> the name as written in the CSV
    report: CsvRosterReport


def _resolve_column(header: list, aliases, override_env: str, label: str):
    """Pick the column for one field: explicit override, then aliases.

    Returns the ACTUAL header string, or None. An explicit override that
    does not exist is an error rather than a silent fallback -- someone set
    it on purpose and needs to know it did not take.
    """
    override = os.getenv(override_env, "").strip()
    if override:
        for col in header:
            if _norm_header(col) == _norm_header(override):
                return col
        raise ValueError(
            f"{override_env}={override!r} but no such column. Header is: "
            + ", ".join(repr(c) for c in header)
        )

    normalised = {_norm_header(c): c for c in header}
    for alias in aliases:
        if alias in normalised:
            return normalised[alias]
    return None


def read_csv_rows(path: str) -> tuple:
    """Return (rows, header). Tolerates a UTF-8 BOM, which Excel always writes."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"No participant CSV at {path}")

    # utf-8-sig strips the BOM. Without it the first column is named
    # '﻿Name' and every alias lookup for it misses.
    with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    return rows, header


def translate_csv(
    rows,
    header,
    max_members_per_team: int = 4,
    event_name: str | None = None,
    event_slug: str | None = None,
) -> TranslatedCsvRoster:
    """Rows in, {id: [username, password, team_id]} out, plus a report."""
    report = CsvRosterReport()
    report.header = list(header)

    name_col = _resolve_column(header, NAME_ALIASES, "ROSTER_CSV_NAME_COLUMN", "name")
    phone_col = _resolve_column(header, PHONE_ALIASES, "ROSTER_CSV_PHONE_COLUMN", "phone")
    team_col = _resolve_column(header, TEAM_ALIASES, "ROSTER_CSV_TEAM_COLUMN", "team")
    event_col = _resolve_column(header, EVENT_ALIASES, "ROSTER_CSV_EVENT_COLUMN", "event")

    missing = [
        label for label, col in (("name", name_col), ("phone", phone_col), ("team", team_col))
        if col is None
    ]
    if missing:
        raise ValueError(
            "Participant CSV is missing the "
            + " and ".join(missing)
            + " column.\nHeader is: "
            + ", ".join(repr(c) for c in header)
            + "\nSet ROSTER_CSV_"
            + "/ROSTER_CSV_".join(m.upper() + "_COLUMN" for m in missing)
            + " to name it explicitly."
        )

    report.resolved_columns = {
        "name": name_col, "phone": phone_col, "team": team_col,
        "event": event_col or "<none>",
    }
    report.no_event_column = event_col is None

    wanted = {
        _norm_header(event_name or DEFAULT_EVENT_NAME),
        _norm_header(event_slug or DEFAULT_EVENT_SLUG),
    }

    player_data: dict = {}
    team_ids: dict = {}        # normalised team key -> team_id
    team_names: dict = {}      # team_id -> name as written
    team_members: dict = {}    # team_id -> [names]
    username_counts: dict = {}
    phone_owners: dict = {}    # digits -> [names]
    next_id = 0

    for offset, row in enumerate(rows):
        # +2: one for the header line, one because humans count from 1. This
        # number is what someone types into the spreadsheet's "go to row" box.
        row_number = offset + 2
        report.rows_read += 1

        if event_col is not None:
            if _norm_header(_norm_value(row.get(event_col))) not in wanted:
                continue
        report.rows_for_event += 1

        full_name = _norm_value(row.get(name_col))
        phone_raw = _norm_value(row.get(phone_col))
        team_raw = _norm_value(row.get(team_col))

        if not full_name:
            report.missing_name.append(row_number)
            continue
        if not team_raw:
            report.missing_team.append((row_number, full_name))
            continue

        password = digits_only(phone_raw)
        if not password:
            # No digits at all -- blank, or something like "N/A". Either way
            # there is no password, so there is no login.
            report.missing_phone.append((row_number, full_name))
            continue

        phone_owners.setdefault(password, []).append(full_name)

        team_key = _norm_header(team_raw)
        if team_key not in team_ids:
            team_ids[team_key] = len(team_ids)
            team_names[team_ids[team_key]] = team_raw
            team_members[team_ids[team_key]] = []
        team_id = team_ids[team_key]

        # Real names collide -- at 700 attendees a repeat is close to
        # certain, and GameServer refuses to start on a duplicate username.
        # Suffix deterministically rather than failing, and shout about it:
        # the player types their own name to log in, so anyone renamed has
        # to be told what they were renamed to.
        username = full_name
        key = username.casefold()
        if key in username_counts:
            username_counts[key] += 1
            username = f"{full_name} {username_counts[key]}"
            report.renamed_duplicates.append((full_name, username))
        else:
            username_counts[key] = 1

        player_data[next_id] = [username, password, team_id]
        team_members[team_id].append(username)
        report.players_emitted += 1
        next_id += 1

    for team_id, members in team_members.items():
        if len(members) > max_members_per_team:
            report.oversized_teams.append(
                (team_names[team_id], len(members), max_members_per_team)
            )
        elif len(members) < max_members_per_team:
            report.undersized_teams.append((team_names[team_id], len(members)))

    for digits, names in phone_owners.items():
        if len(names) > 1:
            report.shared_phone.append((digits, names))

    report.teams_seen = len(team_ids)
    return TranslatedCsvRoster(
        player_data=player_data,
        team_count=len(team_ids),
        team_names=team_names,
        report=report,
    )


def load_csv_roster(
    path: str | None = None,
    max_members_per_team: int = 4,
    event_name: str | None = None,
    event_slug: str | None = None,
) -> TranslatedCsvRoster:
    path = path or os.getenv("ROSTER_CSV_FILE", "participants.csv")
    rows, header = read_csv_rows(path)
    return translate_csv(
        rows, header,
        max_members_per_team=max_members_per_team,
        event_name=event_name or os.getenv("DWR_EVENT_NAME", DEFAULT_EVENT_NAME),
        event_slug=event_slug or os.getenv("DWR_EVENT_SLUG", DEFAULT_EVENT_SLUG),
    )
