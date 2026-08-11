"""Absolute filesystem paths, resolved from this file's location.

Every path in the app used to be relative to the *current working
directory* -- ``StaticFiles(directory="static")``,
``FileResponse("static/new_client/webpage.html")``,
``os.path.join("static", "images")``, ``Image.open(path)`` and
``"game_state.json"``. That silently assumes the process is launched from
the project root.

It is not, on two of the three supported targets:

* Azure App Service runs ``startup.sh`` with the working directory set to
  ``/home/site`` (not ``/home/site/wwwroot``), so ``StaticFiles`` raises
  ``RuntimeError: Directory 'static' does not exist`` at *import* time --
  the app never starts and the platform just reports a failed container.
* A systemd unit on a local server or Krutrim Cloud VM defaults to ``/``
  unless ``WorkingDirectory=`` is set, with the same result.

Resolving everything from ``__file__`` makes the app independent of how it
is launched. ``DWR_STATE_DIR`` additionally lets the JSON checkpoint be
written to a mounted persistent volume (Azure App Service's ``/home`` is
the only durable path on that platform; the app directory is not).
"""
from __future__ import annotations

import os

# Directory containing this file == the project root.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
REFERENCE_IMAGE_DIR = os.path.join(STATIC_DIR, "images")
GENERATED_IMAGE_DIR = os.path.join(STATIC_DIR, "generated")
CLIENT_HTML = os.path.join(STATIC_DIR, "new_client", "webpage.html")

# Where the JSON game checkpoint lives. Override with DWR_STATE_DIR to point
# at a persistent volume (e.g. /home/data on Azure App Service).
STATE_DIR = os.getenv("DWR_STATE_DIR") or PROJECT_ROOT
STATE_FILE = os.path.join(STATE_DIR, "game_state.json")


def resolve_static_path(url_path: str) -> str:
    """Map a server-relative ``/static/...`` URL onto an absolute file path.

    Rejects anything that escapes ``STATIC_DIR``. The previous implementation
    was ``img_input.lstrip("/")`` with no containment check at all, so a value
    like ``/static/../../etc/passwd`` would have been opened directly. Nothing
    user-controlled reaches this today, but the round image string is passed
    straight through from ``get_image``, so the guard belongs here rather than
    in a comment.
    """
    relative = url_path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if relative.startswith("static/"):
        relative = relative[len("static/"):]

    candidate = os.path.normpath(os.path.join(STATIC_DIR, relative))
    static_root = os.path.normpath(STATIC_DIR)
    if candidate != static_root and not candidate.startswith(static_root + os.sep):
        raise ValueError(f"Refusing to read {url_path!r}: path escapes the static directory.")
    return candidate
