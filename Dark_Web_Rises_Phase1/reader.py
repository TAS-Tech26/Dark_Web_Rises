"""Supabase roster loader / translator.

Reads the event sign-up rows out of Supabase and translates them into the
`player_data` shape `GameServer` expects:

    { player_id: [username, password, team_id] }

Two structural changes from the original:

1. **No client is created at import time.** The module used to call
   `create_client(os.environ.get("SUPABASE_URL"), ...)` at the top level,
   which raises if either variable is unset -- so the module could not even
   be imported, which is why `game.py` never imported it. The client is now
   built on demand by `connect()`.

2. **The translation is a pure function of the rows** (`translate_roster`),
   separate from the query (`fetch_roster_rows`). That means the part with
   all the logic in it can be tested exhaustively without a database, a
   network, or the `supabase` package installed at all.

The translator now also *validates*. The original silently dropped players:
`final_dict[member.get("id")] = ...` means every member with a missing or
duplicated `id` overwrote the previous one. A four-person team where two
members had no `id` produced two players, with no error raised and nothing
logged -- those attendees would simply have been unable to log in on the
day. Problems are now collected into a `RosterReport` and, by default, raise
before the roster is ever handed to `GameServer`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("dwr.reader")

DEFAULT_EVENT_NAME = "Dark Web Rises"
DEFAULT_EVENT_SLUG = "dark-web-rises"
DEFAULT_TABLE = "logged_in_users"

# How many phone digits go into the derived password.
PASSWORD_PHONE_DIGITS = 5


@dataclass
class RosterReport:
    """Everything that was wrong with the source data."""
    teams_seen: int = 0
    members_seen: int = 0
    players_emitted: int = 0

    missing_id: list = field(default_factory=list)
    duplicate_id: list = field(default_factory=list)
    missing_name: list = field(default_factory=list)
    missing_phone: list = field(default_factory=list)
    duplicate_username: list = field(default_factory=list)
    oversized_teams: list = field(default_factory=list)
    empty_teams: list = field(default_factory=list)
    undersized_teams: list = field(default_factory=list)
    unparseable_rows: list = field(default_factory=list)

    @property
    def fatal(self) -> list:
        """Problems that mean the roster is wrong, not merely imperfect."""
        return (
            [f"member with no id: {m}" for m in self.missing_id]
            + [f"duplicate member id {m}" for m in self.duplicate_id]
            + [f"member with no name: id={m}" for m in self.missing_name]
            + [f"team {t} has {n} members, over the cap of {cap}"
               for t, n, cap in self.oversized_teams]
            + [f"row {i} could not be parsed: {err}" for i, err in self.unparseable_rows]
        )

    @property
    def warnings(self) -> list:
        return (
            [f"member id={m} has no phone; password falls back to name only"
             for m in self.missing_phone]
            + [f"username {u!r} collided and was suffixed" for u in self.duplicate_username]
            + [f"team {t} is empty" for t in self.empty_teams]
            + [f"team {t} has only {n} members" for t, n in self.undersized_teams]
        )

    def summary(self) -> str:
        lines = [
            f"Roster: {self.teams_seen} teams, {self.members_seen} members read, "
            f"{self.players_emitted} players emitted."
        ]
        if self.members_seen != self.players_emitted:
            lines.append(
                f"  !! {self.members_seen - self.players_emitted} member(s) did not "
                f"survive translation."
            )
        for problem in self.fatal:
            lines.append(f"  FATAL   {problem}")
        for problem in self.warnings:
            lines.append(f"  warning {problem}")
        return "\n".join(lines)


@dataclass
class TranslatedRoster:
    player_data: dict
    team_count: int
    report: RosterReport


def connect(url: str | None = None, key: str | None = None):
    """Build a Supabase client on demand.

    Deliberately not called at import time -- see the module docstring.
    """
    # Credentials are checked before the import so that a missing env var
    # reports the actual problem rather than an unrelated ImportError on
    # machines where the optional `supabase` package isn't installed.
    url = url or os.environ.get("SUPABASE_URL")
    key = key or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must both be set (or passed explicitly) "
            "before connecting to Supabase."
        )

    try:
        from supabase import create_client  # optional dependency, imported lazily
    except ImportError as exc:
        raise RuntimeError(
            "The 'supabase' package is not installed. It is only needed for the "
            "database migration path; the running game does not require it. "
            "Install it with: pip install supabase"
        ) from exc

    return create_client(url, key)


def fetch_roster_rows(
    supabase,
    event_name: str = DEFAULT_EVENT_NAME,
    event_slug: str = DEFAULT_EVENT_SLUG,
    table: str = DEFAULT_TABLE,
):
    """Pull the team-leader rows for one event. Pure I/O, no logic."""
    response = (
        supabase.table(table)
        .select("team_members")
        .eq("team_leader", True)
        .eq("event_name", event_name)
        .eq("event_slug", event_slug)
        .execute()
    )
    return response.data or []


def derive_password(full_name: str, phone, digits: int = PASSWORD_PHONE_DIGITS) -> str:
    """Build the login password from the attendee's phone and first name.

    Two fixes over the original `str(phone)[:5]`:

    * **Digits only.** A number stored as "+919876543210" gave a prefix of
      "+9198" -- three of the five characters are the country code, identical
      for every Indian attendee. Whether a password had any entropy at all
      depended on whether that row happened to include "+91".
    * **Last N digits, not first.** Indian mobile numbers start 6-9 and share
      long operator/circle prefixes, so leading digits are largely common
      across attendees. Trailing digits are effectively random.
    """
    digits_only = re.sub(r"\D", "", str(phone or ""))
    tail = digits_only[-digits:] if digits_only else ""
    first_name = full_name.split()[0] if full_name and full_name.split() else ""
    return f"{tail}{first_name}"


def translate_roster(
    rows,
    max_members_per_team: int = 4,
    password_digits: int = PASSWORD_PHONE_DIGITS,
    skip_empty_teams: bool = True,
) -> TranslatedRoster:
    """Translate Supabase rows into GameServer's `player_data`.

    Pure function -- no network, no `supabase` import. This is where all the
    logic lives, so this is what the tests exercise.
    """
    report = RosterReport()
    player_data: dict = {}
    seen_ids: set = set()
    username_counts: dict = {}
    team_index = 0

    # A sentinel distinguishes "the column is absent" (a malformed row) from
    # "the column is present but empty" (a genuinely empty team). Defaulting
    # to [] conflated the two.
    missing = object()

    for row_number, row in enumerate(rows):
        raw_members = row.get("team_members", missing) if isinstance(row, dict) else missing

        if raw_members is missing:
            report.unparseable_rows.append((row_number, "no team_members column"))
            continue

        if isinstance(raw_members, str):
            try:
                raw_members = json.loads(raw_members)
            except (json.JSONDecodeError, ValueError) as exc:
                report.unparseable_rows.append((row_number, str(exc)))
                continue

        if raw_members is None:
            report.unparseable_rows.append((row_number, "team_members is null"))
            continue
        if not isinstance(raw_members, list):
            report.unparseable_rows.append(
                (row_number, f"team_members is {type(raw_members).__name__}, expected list")
            )
            continue

        if not raw_members:
            # An empty team still consumed a team number in the original,
            # silently shifting every subsequent team's id.
            report.empty_teams.append(team_index)
            if skip_empty_teams:
                continue
            team_index += 1
            continue

        if len(raw_members) > max_members_per_team:
            report.oversized_teams.append((team_index, len(raw_members), max_members_per_team))
        elif len(raw_members) < max_members_per_team:
            report.undersized_teams.append((team_index, len(raw_members)))

        for member in raw_members:
            report.members_seen += 1
            if not isinstance(member, dict):
                report.unparseable_rows.append(
                    (row_number, f"member is {type(member).__name__}, expected object")
                )
                continue

            member_id = member.get("id")
            full_name = (member.get("name") or "").strip()
            phone = member.get("phone", "")

            # These two were the silent data-loss paths.
            if member_id is None:
                report.missing_id.append(full_name or "<unnamed>")
                continue
            if member_id in seen_ids:
                report.duplicate_id.append(member_id)
                continue
            seen_ids.add(member_id)

            if not full_name:
                report.missing_name.append(member_id)
                continue
            if not phone:
                report.missing_phone.append(member_id)

            username = full_name
            # Real names collide -- at 600 attendees a repeat is close to
            # certain, and GameServer (correctly) refuses to start on a
            # duplicate. Disambiguate deterministically instead of failing.
            key = username.casefold()
            if key in username_counts:
                username_counts[key] += 1
                report.duplicate_username.append(username)
                username = f"{full_name} {username_counts[key]}"
            else:
                username_counts[key] = 1

            player_data[member_id] = [
                username,
                derive_password(full_name, phone, password_digits),
                team_index,
            ]
            report.players_emitted += 1

        team_index += 1

    report.teams_seen = team_index
    return TranslatedRoster(player_data=player_data, team_count=team_index, report=report)


def generate_team_dictionary(
    supabase,
    event_name: str = DEFAULT_EVENT_NAME,
    event_slug: str = DEFAULT_EVENT_SLUG,
    max_members_per_team: int = 4,
    strict: bool = True,
):
    """Fetch and translate in one call.

    Returns `(player_data, team_count)`, matching the original signature.

    With `strict=True` (the default) a roster with fatal problems raises
    rather than being handed to GameServer half-formed. Pass `strict=False`
    to get whatever could be salvaged -- the report is logged either way.
    """
    rows = fetch_roster_rows(supabase, event_name, event_slug)
    result = translate_roster(rows, max_members_per_team=max_members_per_team)

    logger.info("%s", result.report.summary())

    if result.report.fatal:
        message = "Roster from Supabase is not usable:\n" + result.report.summary()
        if strict:
            raise ValueError(message)
        logger.error("%s", message)

    return result.player_data, result.team_count
