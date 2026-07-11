"""LaunchAgent install so sync runs at login and every CHATMESH_INTERVAL
seconds, surviving restarts."""

from __future__ import annotations

import os
import plistlib
import subprocess

from .config import STATE_DIR
from .util import log

LABEL = "com.mhadifilms.chatmesh"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install(interval: int) -> None:
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(os.path.join(STATE_DIR, "logs"), exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [os.path.join(_repo_root(), "bin", "chatmesh"), "sync"],
        "RunAtLoad": True,
        "StartInterval": interval,
        "StandardOutPath": os.path.join(STATE_DIR, "logs", "launchd.out"),
        "StandardErrorPath": os.path.join(STATE_DIR, "logs", "launchd.err"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        },
    }
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, LABEL)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, PLIST],
                       capture_output=True, text=True)
    if r.returncode != 0:
        r2 = subprocess.run(["launchctl", "load", "-w", PLIST],
                            capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError("launchctl failed: %s / %s"
                               % (r.stderr.strip(), r2.stderr.strip()))
    log.info("LaunchAgent installed: %s (every %ds, plus at login)", PLIST, interval)


def uninstall() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, LABEL)],
                   capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    log.info("LaunchAgent removed")
