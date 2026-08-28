"""Roster loading: Supabase, with a local cache so the event survives an outage.

The running game must not have a hard dependency on Supabase being reachable
at the moment the process starts. If the roster were fetched live with no
fallback, a Supabase outage -- or an expired key, or a network blip on the
venue's uplink -- on the morning of the event would mean the app simply does
not boot, with 600 people already in the room.

So the load order is:

    1. Supabase  -> translate -> validate -> **write cache** -> use
    2. Supabase unreachable or roster invalid -> boot from the last good cache
    3. No cache either -> refuse to start, with a message saying what to do

Configuration (all optional):

    ROSTER_SOURCE      auto (default) | supabase | cache | hardcoded
                       "auto" uses Supabase when SUPABASE_URL and
                       SUPABASE_KEY are both set, otherwise the built-in
                       placeholder roster.
    ROSTER_CACHE_FILE  where to keep the cache (default: alongside the game
                       state, honouring DWR_STATE_DIR)
    DWR_ADMINS         "name:password,name2:password2" -- admin accounts.
                       Falls back to a placeholder with a loud warning.
    MAX_MEMBERS_PER_TEAM  default 4
    SUPABASE_URL / SUPABASE_KEY / DWR_EVENT_NAME / DWR_EVENT_SLUG

Security note: the cache contains attendee passwords in plain text, because
GameServer hashes them at construction and therefore needs the plaintext at
boot. The file is written with owner-only permissions and is gitignored.
Treat it exactly as you would the roster itself.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field

import reader
from paths import STATE_DIR

logger = logging.getLogger("dwr.roster")

CACHE_VERSION = 1
DEFAULT_CACHE_FILE = os.path.join(STATE_DIR, "roster_cache.json")

# Placeholder roster, used only when no Supabase credentials are configured
# (local development and the test suite). Never for a real event.
HARDCODED_PLAYER_DATA = {
    0: ["user1", "password1", 0],
    1: ["user2", "password2", 0],
    2: ["user3", "password3", 2],
    3: ["user4", "password4", 0],
    4: ["user5", "password5", 0],
}
HARDCODED_TEAM_COUNT = 3
HARDCODED_ADMINS = {0: ["admin1", "admin_password1"], 1: ["admin2", "admin_password2"]}


@dataclass
class RosterBundle:
    player_data: dict
    team_count: int
    admin_data: dict
    source: str
    fetched_at: float | None = None
    warnings: list = field(default_factory=list)

    @property
    def player_count(self) -> int:
        return len(self.player_data)


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------
def cache_path() -> str:
    return os.getenv("ROSTER_CACHE_FILE") or DEFAULT_CACHE_FILE


def write_cache(bundle: RosterBundle, path: str | None = None) -> bool:
    """Persist a translated roster. Returns False on failure rather than
    raising -- a cache write problem must never stop a working boot."""
    path = path or cache_path()
    # Stored as a list of records, not a dict. JSON object keys are always
    # strings, so a dict would silently turn integer member ids into strings
    # and break every lookup after a cache round-trip.
    payload = {
        "version": CACHE_VERSION,
        "fetched_at": bundle.fetched_at or time.time(),
        "team_count": bundle.team_count,
        "players": [
            {"id": pid, "username": entry[0], "password": entry[1], "team_id": entry[2]}
            for pid, entry in bundle.player_data.items()
        ],
    }
    temp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(temp, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
        except OSError:
            pass  # best effort; Windows and some mounts don't support it
        logger.info("Roster cache written to %s (%s players).", path, len(payload["players"]))
        return True
    except OSError as exc:
        logger.error("Could not write roster cache to %s: %s", path, exc)
        return False


def read_cache(path: str | None = None) -> RosterBundle | None:
    path = path or cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("cache is not a JSON object")
        if payload.get("version") != CACHE_VERSION:
            raise ValueError(f"unsupported cache version {payload.get('version')!r}")

        player_data = {}
        for record in payload.get("players", []):
            player_data[record["id"]] = [
                record["username"], record["password"], record["team_id"]
            ]

        if not player_data:
            raise ValueError("cache contains no players")

        return RosterBundle(
            player_data=player_data,
            team_count=payload.get("team_count") or (
                max(entry[2] for entry in player_data.values()) + 1
            ),
            admin_data=load_admins(),
            source="cache",
            fetched_at=payload.get("fetched_at"),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        logger.error("Roster cache at %s is unusable (%s); ignoring it.", path, exc)
        return None


# ----------------------------------------------------------------------
# Admins
# ----------------------------------------------------------------------
def load_admins() -> dict:
    """Parse DWR_ADMINS ("name:password,name2:password2")."""
    raw = os.getenv("DWR_ADMINS", "").strip()
    if not raw:
        logger.warning(
            "DWR_ADMINS is not set; falling back to placeholder admin credentials. "
            "Set DWR_ADMINS before the event -- anyone who reads the source knows these."
        )
        return dict(HARDCODED_ADMINS)

    admins = {}
    for index, chunk in enumerate(raw.split(",")):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(
                f"DWR_ADMINS entry {chunk!r} is malformed; expected 'name:password'."
            )
        name, password = chunk.split(":", 1)
        name, password = name.strip(), password.strip()
        if not name or not password:
            raise ValueError(f"DWR_ADMINS entry {chunk!r} has an empty name or password.")
        admins[index] = [name, password]

    if not admins:
        raise ValueError("DWR_ADMINS was set but no valid entries were parsed.")
    logger.info("Loaded %s admin account(s) from DWR_ADMINS.", len(admins))
    return admins


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------
def load_from_supabase(max_members_per_team: int = 4) -> RosterBundle:
    client = reader.connect()
    rows = reader.fetch_roster_rows(
        client,
        event_name=os.getenv("DWR_EVENT_NAME", reader.DEFAULT_EVENT_NAME),
        event_slug=os.getenv("DWR_EVENT_SLUG", reader.DEFAULT_EVENT_SLUG),
    )
    result = reader.translate_roster(rows, max_members_per_team=max_members_per_team)
    logger.info("%s", result.report.summary())

    if result.report.fatal:
        raise ValueError(
            "Roster fetched from Supabase is not usable:\n" + result.report.summary()
        )
    if not result.player_data:
        raise ValueError(
            "Supabase returned no players. If the table is not empty, check that "
            "row-level security grants read access to this key -- RLS returns zero "
            "rows rather than an error."
        )

    return RosterBundle(
        player_data=result.player_data,
        team_count=result.team_count,
        admin_data=load_admins(),
        source="supabase",
        fetched_at=time.time(),
        warnings=list(result.report.warnings),
    )


def load_from_csv(max_members_per_team: int = 4) -> RosterBundle:
    """Production path: build the roster from the participant CSV export.

    Fatal on any row that cannot produce a working login. That is deliberate.
    A roster that boots with 694 of 700 players looks completely healthy --
    "Server ready: 694 players across 175 teams" -- and the only symptom is
    six people who cannot log in and no way to tell who they are once the
    room is full. Better to refuse now, with their names and row numbers.
    """
    import csv_roster

    path = os.getenv("ROSTER_CSV_FILE", "participants.csv")
    result = csv_roster.load_csv_roster(
        path=path,
        max_members_per_team=max_members_per_team,
        event_name=os.getenv("DWR_EVENT_NAME", csv_roster.DEFAULT_EVENT_NAME),
        event_slug=os.getenv("DWR_EVENT_SLUG", csv_roster.DEFAULT_EVENT_SLUG),
    )
    logger.info("%s", result.report.summary())

    strict = os.getenv("ROSTER_CSV_STRICT", "true").strip().lower() not in {"0", "false", "no"}
    if result.report.fatal:
        if strict:
            raise ValueError(
                f"Participant CSV at {path} has rows that cannot produce a login:\n"
                + result.report.summary()
                + "\n\nFix the CSV, or set ROSTER_CSV_STRICT=false to drop those "
                  "rows and play without them."
            )
        logger.error(
            "ROSTER_CSV_STRICT=false: dropping %s unusable row(s) and continuing. "
            "Those people cannot log in.\n%s",
            len(result.report.errors), result.report.summary(),
        )
    if not result.player_data:
        raise ValueError(
            f"No players matched event {os.getenv('DWR_EVENT_NAME', csv_roster.DEFAULT_EVENT_NAME)!r} "
            f"in {path}. Check the event column's spelling -- the filter is exact "
            "apart from case and punctuation."
        )

    return RosterBundle(
        player_data=result.player_data,
        team_count=result.team_count,
        admin_data=load_admins(),
        source="csv",
        fetched_at=time.time(),
        warnings=list(result.report.warnings),
    )


def load_hardcoded() -> RosterBundle:
    logger.warning(
        "Using the built-in placeholder roster (user1/password1/...). This is for "
        "local development only -- configure SUPABASE_URL and SUPABASE_KEY for a real event."
    )
    return RosterBundle(
        player_data=dict(HARDCODED_PLAYER_DATA),
        team_count=HARDCODED_TEAM_COUNT,
        admin_data=dict(HARDCODED_ADMINS),
        source="hardcoded",
    )


def resolve_source() -> str:
    source = os.getenv("ROSTER_SOURCE", "auto").strip().lower()
    if source not in {"auto", "csv", "supabase", "cache", "hardcoded"}:
        logger.warning("Unknown ROSTER_SOURCE %r; treating as 'auto'.", source)
        source = "auto"
    if source == "auto":
        # CSV first: it is the production path, and a participants.csv sitting
        # in the working directory is a much stronger signal of intent than
        # two Supabase variables that may still hold placeholders.
        if os.path.exists(os.getenv("ROSTER_CSV_FILE", "participants.csv")):
            return "csv"
        return "supabase" if (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY")) else "hardcoded"
    return source


def load_roster(max_members_per_team: int | None = None) -> RosterBundle:
    """Load the roster, falling back through Supabase -> cache -> error."""
    if max_members_per_team is None:
        max_members_per_team = int(os.getenv("MAX_MEMBERS_PER_TEAM", "4"))

    source = resolve_source()

    if source == "hardcoded":
        return load_hardcoded()

    if source == "cache":
        cached = read_cache()
        if cached is None:
            raise RuntimeError(
                f"ROSTER_SOURCE=cache but no usable cache exists at {cache_path()}."
            )
        logger.warning(
            "Booting from the roster cache by request (fetched %s).",
            _describe_age(cached.fetched_at),
        )
        return cached

    if source == "csv":
        # No silent fallback to the cache here, unlike Supabase. Supabase can
        # be unreachable through nobody's fault, so booting from a snapshot is
        # the right call. A CSV is a local file: if it is missing or malformed
        # that is a mistake someone just made, and falling back would hide it
        # behind a roster that is quietly out of date.
        bundle = load_from_csv(max_members_per_team)
        for warning in bundle.warnings:
            logger.warning("Roster: %s", warning)
        write_cache(bundle)
        logger.info(
            "Roster loaded from CSV: %s players across %s teams.",
            bundle.player_count, bundle.team_count,
        )
        return bundle

    # source == "supabase"
    try:
        bundle = load_from_supabase(max_members_per_team)
    except Exception as exc:
        logger.error("Could not load the roster from Supabase: %s", exc)
        cached = read_cache()
        if cached is not None:
            logger.warning(
                "FALLING BACK to the cached roster (fetched %s, %s players). "
                "Supabase was unreachable or returned an unusable roster; any "
                "sign-ups since that fetch are NOT included.",
                _describe_age(cached.fetched_at), cached.player_count,
            )
            return cached
        raise RuntimeError(
            "Roster unavailable: Supabase could not be read and there is no local "
            f"cache at {cache_path()}. Run `python tools/supabase_dryrun.py` to "
            "diagnose, or set ROSTER_SOURCE=hardcoded to start with placeholders."
        ) from exc

    for warning in bundle.warnings:
        logger.warning("Roster: %s", warning)

    write_cache(bundle)
    logger.info(
        "Roster loaded from Supabase: %s players across %s teams.",
        bundle.player_count, bundle.team_count,
    )
    return bundle


def _describe_age(fetched_at) -> str:
    if not fetched_at:
        return "at an unknown time"
    age_seconds = max(0.0, time.time() - fetched_at)
    if age_seconds < 3600:
        return f"{age_seconds / 60:.0f} minutes ago"
    if age_seconds < 86400:
        return f"{age_seconds / 3600:.1f} hours ago"
    return f"{age_seconds / 86400:.1f} days ago"
