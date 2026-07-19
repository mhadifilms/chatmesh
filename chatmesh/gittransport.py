"""Git smart-protocol transport into isolated Chatmesh refs."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from typing import Optional
from urllib.parse import quote


_PEER_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


def _run_git(repo: str, args, *, check: bool = True,
             input_data: Optional[bytes] = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", "-C", repo] + list(args),
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=env,
    )


def ssh_repo_url(peer: str, absolute_path: str) -> str:
    if not _PEER_RE.match(peer):
        raise ValueError("unsafe SSH peer alias")
    if not os.path.isabs(absolute_path) or "\x00" in absolute_path:
        raise ValueError("remote repository path must be absolute")
    encoded = quote(absolute_path, safe="/-._~")
    return "ssh://%s%s" % (peer, encoded)


def incoming_ref(peer: str, branch: str, oid: str) -> str:
    branch_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")[:80] or "head"
    peer_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", peer).strip("-")[:40] or "peer"
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", oid):
        raise ValueError("invalid object id")
    digest = hashlib.sha256(branch.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return "refs/chatmesh/incoming/%s/%s-%s/%s" % (
        peer_slug, branch_slug, digest, oid.lower(),
    )


def fetch_branch(repo: str, peer: str, remote_path: str, branch: str,
                 expected_oid: str) -> str:
    """Fetch a peer branch into an immutable namespaced ref."""
    ref = incoming_ref(peer, branch, expected_oid)
    url = ssh_repo_url(peer, remote_path)
    spec = "refs/heads/%s:%s" % (branch, ref)
    proc = _run_git(repo, ["fetch", "--no-tags", "--force", url, spec], check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    actual = _run_git(repo, ["rev-parse", "--verify", ref]).stdout.decode().strip()
    if actual != expected_oid:
        raise RuntimeError(
            "branch moved during transfer: expected %s, got %s"
            % (expected_oid, actual)
        )
    return ref


def push_branch(repo: str, peer: str, remote_path: str, branch: str,
                oid: str, source_label: str) -> str:
    """Push an exact commit into an immutable namespaced ref on a peer."""
    ref = incoming_ref(source_label, branch, oid)
    url = ssh_repo_url(peer, remote_path)
    spec = "%s:%s" % (oid, ref)
    proc = _run_git(repo, ["push", url, spec], check=False)
    if proc.returncode != 0:
        # A prior identical push is a successful immutable import.
        verify = subprocess.run(
            ["git", "ls-remote", url, ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
        line = verify.stdout.decode("utf-8", "replace").strip().split()
        if verify.returncode != 0 or not line or line[0] != oid:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    return ref


def fetch_tag(repo: str, peer: str, remote_path: str, tag: str,
              expected_oid: str) -> str:
    ref = incoming_ref(peer, "tag-" + tag, expected_oid)
    url = ssh_repo_url(peer, remote_path)
    spec = "refs/tags/%s:%s" % (tag, ref)
    proc = _run_git(repo, ["fetch", "--no-tags", "--force", url, spec], check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    actual = _run_git(repo, ["rev-parse", "--verify", ref]).stdout.decode().strip()
    if actual != expected_oid:
        raise RuntimeError("tag moved during transfer")
    return ref


def push_all_refs(repo: str, peer: str, remote_path: str) -> None:
    """Populate a newly initialized peer checkout; never use on existing repos."""
    url = ssh_repo_url(peer, remote_path)
    proc = _run_git(
        repo,
        [
            "push", "--atomic", url,
            "refs/heads/*:refs/heads/*",
            "refs/tags/*:refs/tags/*",
        ],
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
