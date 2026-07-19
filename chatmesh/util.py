"""Shared helpers: logging, locking, ssh, process gates."""

from __future__ import annotations

import fcntl
import logging
import os
import shlex
import subprocess
import sys
from typing import List, Optional

from .config import REMOTE_REPO, default_state_dir

log = logging.getLogger("chatmesh")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def home() -> str:
    """Real home, or CHATMESH_HOME when tests point at a fixture tree."""
    return os.environ.get("CHATMESH_HOME") or os.path.expanduser("~")


def setup_logging(level: str = "INFO", state_dir: Optional[str] = None,
                  file_logging: bool = True) -> None:
    state_dir = state_dir or default_state_dir()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.setLevel(getattr(logging, level, logging.INFO))
    if not log.handlers:
        if file_logging:
            os.makedirs(os.path.join(state_dir, "logs"), exist_ok=True)
            fh = logging.FileHandler(os.path.join(state_dir, "logs", "chatmesh.log"))
            fh.setFormatter(fmt)
            log.addHandler(fh)
        log.addHandler(sh)


class Lock:
    """Exclusive advisory lock so overlapping launchd runs no-op."""

    def __init__(self, name: str = "sync.lock", state_dir: Optional[str] = None):
        state_dir = state_dir or default_state_dir()
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, name)
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self.fd, 0)
            os.write(self.fd, str(os.getpid()).encode())
            return True
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def run(argv: List[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("check", True)
    return subprocess.run(argv, **kw)


def ssh_argv(peer: str, command: str) -> List[str]:
    return ["ssh"] + SSH_OPTS + [peer, command]


def ssh_out(peer: str, command: str, timeout: int = 60) -> str:
    p = subprocess.run(ssh_argv(peer, command), capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("ssh %s failed: %s" % (peer, p.stderr.strip()[:500]))
    return p.stdout


def remote_home(peer: str) -> str:
    return ssh_out(peer, 'printf %s "$HOME"').strip()


def remote_chatmesh_cmd(sub: str) -> str:
    """Shell command string that runs a chatmesh subcommand on a peer."""
    return 'PYTHONPATH="$HOME/%s" python3 -m chatmesh %s' % (REMOTE_REPO, sub)


def remote_chatmesh_args(args: List[str]) -> str:
    """Safely quote an argv list for a remote Chatmesh invocation."""
    return 'PYTHONPATH="$HOME/%s" python3 -m chatmesh %s' % (
        REMOTE_REPO, " ".join(shlex.quote(str(arg)) for arg in args),
    )


# --------------------------------------------------------------------------- #
# App-running gates
# --------------------------------------------------------------------------- #
def _match_app(args_lines: List[str], app: str) -> bool:
    for line in args_lines:
        line = line.strip()
        if not line:
            continue
        if app == "cursor":
            if "Cursor.app/Contents/MacOS/Cursor" in line:
                return True
        elif app == "cursor-cli":
            head = line.split()[0] if line.split() else ""
            if os.path.basename(head) == "cursor-agent" or "/cursor-agent" in head:
                return True
        elif app in ("claude", "codex"):
            head = line.split()[0] if line.split() else ""
            if os.path.basename(head) == app:
                return True
    return False


def app_running_local(app: str) -> bool:
    if os.environ.get("CHATMESH_ASSUME_CLOSED") == "1":  # test fixtures only
        return False
    p = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True)
    return _match_app(p.stdout.splitlines(), app)


def app_running_remote(peer: str, app: str) -> bool:
    out = ssh_out(peer, "ps -axo args=")
    return _match_app(out.splitlines(), app)
