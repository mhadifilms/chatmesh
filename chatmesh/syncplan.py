"""Pure repository matching and convergence planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class RepoPair:
    identity: str
    local: dict
    remote: dict


@dataclass(frozen=True)
class RepoAmbiguity:
    identity: str
    local: Tuple[dict, ...]
    remote: Tuple[dict, ...]
    reason: str


def _identity(record: dict) -> str:
    return str(
        record.get("identity")
        or record.get("repository_id")
        or record.get("origin_identity")
        or ""
    )


def _relative(record: dict) -> str:
    return str(record.get("relative_path") or record.get("relpath") or "")


def _branch(record: dict) -> str:
    return str(record.get("branch") or "")


def _superproject(record: dict) -> str:
    return str(
        record.get("superproject_identity")
        or record.get("parent_identity")
        or record.get("superproject")
        or ""
    )


def match_repositories(local: Sequence[dict], remote: Sequence[dict]):
    """Match moves safely; ambiguous duplicate clones are never guessed."""
    lgroups: Dict[str, List[dict]] = {}
    rgroups: Dict[str, List[dict]] = {}
    for record in local:
        identity = _identity(record)
        if identity:
            lgroups.setdefault(identity, []).append(record)
    for record in remote:
        identity = _identity(record)
        if identity:
            rgroups.setdefault(identity, []).append(record)

    pairs: List[RepoPair] = []
    ambiguities: List[RepoAmbiguity] = []
    local_only: List[dict] = []
    remote_only: List[dict] = []

    for identity in sorted(set(lgroups) | set(rgroups)):
        left = list(lgroups.get(identity, []))
        right = list(rgroups.get(identity, []))
        if not left:
            remote_only.extend(right)
            continue
        if not right:
            local_only.extend(left)
            continue

        unmatched_left = list(left)
        unmatched_right = list(right)

        def consume(key):
            left_map: Dict[Tuple[str, ...], List[dict]] = {}
            right_map: Dict[Tuple[str, ...], List[dict]] = {}
            for item in unmatched_left:
                left_map.setdefault(key(item), []).append(item)
            for item in unmatched_right:
                right_map.setdefault(key(item), []).append(item)
            selected = []
            for value in set(left_map) & set(right_map):
                if not any(value):
                    continue
                if len(left_map[value]) == len(right_map[value]) == 1:
                    selected.append((left_map[value][0], right_map[value][0]))
            for litem, ritem in selected:
                unmatched_left.remove(litem)
                unmatched_right.remove(ritem)
                pairs.append(RepoPair(identity, litem, ritem))

        # Stable logical path is strongest, followed by worktree branch and
        # nested-superproject ancestry. A unique remainder is safe after moves.
        consume(lambda item: (_relative(item),))
        consume(lambda item: (_branch(item), _superproject(item)))
        consume(lambda item: (_branch(item),))
        if len(unmatched_left) == len(unmatched_right) == 1:
            pairs.append(RepoPair(identity, unmatched_left.pop(), unmatched_right.pop()))

        if unmatched_left and unmatched_right:
            ambiguities.append(RepoAmbiguity(
                identity, tuple(unmatched_left), tuple(unmatched_right),
                "duplicate repository identity could not be matched safely",
            ))
        else:
            local_only.extend(unmatched_left)
            remote_only.extend(unmatched_right)

    # One peer may be offline from gh and therefore expose only the normalized
    # origin fallback while the other has GitHub's stable repository ID. Apply
    # the same path/branch/parent disambiguation used above instead of requiring
    # the whole repository group to contain exactly one checkout.
    left_aliases: Dict[str, List[dict]] = {}
    right_aliases: Dict[str, List[dict]] = {}
    for record in local_only:
        alias = str(record.get("origin_identity") or "")
        if alias:
            left_aliases.setdefault(alias, []).append(record)
    for record in remote_only:
        alias = str(record.get("origin_identity") or "")
        if alias:
            right_aliases.setdefault(alias, []).append(record)
    for alias in sorted(set(left_aliases) & set(right_aliases)):
        unmatched_left = list(left_aliases[alias])
        unmatched_right = list(right_aliases[alias])

        def compatible(left: dict, right: dict) -> bool:
            left_id = left.get("repository_id")
            right_id = right.get("repository_id")
            return not left_id or not right_id or left_id == right_id

        def pair(left: dict, right: dict) -> None:
            unmatched_left.remove(left)
            unmatched_right.remove(right)
            local_only.remove(left)
            remote_only.remove(right)
            stable_id = left.get("repository_id") or right.get("repository_id")
            pairs.append(RepoPair(
                "github-id:%s" % stable_id if stable_id else alias,
                left,
                right,
            ))

        def consume_alias(key) -> None:
            left_map: Dict[Tuple[str, ...], List[dict]] = {}
            right_map: Dict[Tuple[str, ...], List[dict]] = {}
            for item in unmatched_left:
                left_map.setdefault(key(item), []).append(item)
            for item in unmatched_right:
                right_map.setdefault(key(item), []).append(item)
            selected = []
            for value in set(left_map) & set(right_map):
                if (
                    any(value)
                    and len(left_map[value]) == len(right_map[value]) == 1
                    and compatible(left_map[value][0], right_map[value][0])
                ):
                    selected.append((left_map[value][0], right_map[value][0]))
            for left, right in selected:
                pair(left, right)

        consume_alias(lambda item: (_relative(item),))
        consume_alias(lambda item: (_branch(item), _superproject(item)))
        consume_alias(lambda item: (_branch(item),))
        if (
            len(unmatched_left) == len(unmatched_right) == 1
            and compatible(unmatched_left[0], unmatched_right[0])
        ):
            pair(unmatched_left[0], unmatched_right[0])

        if unmatched_left and unmatched_right:
            for item in unmatched_left:
                local_only.remove(item)
            for item in unmatched_right:
                remote_only.remove(item)
            ambiguities.append(RepoAmbiguity(
                alias,
                tuple(unmatched_left),
                tuple(unmatched_right),
                "origin fallback could not match duplicate checkouts safely",
            ))

    return pairs, local_only, remote_only, ambiguities


def branch_import_plan(local_heads: Dict[str, str], remote_heads: Dict[str, str],
                       directions: Iterable[str]) -> List[dict]:
    """Plan immutable imports; ancestry is classified after objects arrive."""
    directions = set(directions)
    actions = []
    for branch in sorted(set(local_heads) | set(remote_heads)):
        local_oid = local_heads.get(branch)
        remote_oid = remote_heads.get(branch)
        if local_oid == remote_oid:
            continue
        if remote_oid and "pull" in directions:
            actions.append({
                "action": "pull-ref",
                "branch": branch,
                "source_oid": remote_oid,
                "destination_oid": local_oid,
            })
        if local_oid and "push" in directions:
            actions.append({
                "action": "push-ref",
                "branch": branch,
                "source_oid": local_oid,
                "destination_oid": remote_oid,
            })
    return actions


def wip_transfer_plan(local: dict, remote: dict,
                      directions: Iterable[str]) -> List[dict]:
    """Choose safe WIP transfer/apply versus inbox-only quarantine."""
    directions = set(directions)
    local_id = local.get("wip_id")
    remote_id = remote.get("wip_id")
    local_dirty = bool(local.get("dirty"))
    remote_dirty = bool(remote.get("dirty"))
    if local_id and local_id == remote_id:
        return []
    actions = []
    conflict = local_dirty and remote_dirty
    pull_base_matches = (
        bool(remote.get("head"))
        and remote.get("head") == local.get("head")
        and remote.get("branch") == local.get("branch")
    )
    push_base_matches = (
        bool(local.get("head"))
        and local.get("head") == remote.get("head")
        and local.get("branch") == remote.get("branch")
    )
    if remote_dirty and "pull" in directions:
        apply = not local_dirty and pull_base_matches
        actions.append({
            "action": "pull-wip",
            "snapshot_id": remote_id,
            "apply": apply,
            "reason": (
                "concurrent-wip"
                if conflict
                else "destination-clean"
                if apply
                else "base-mismatch"
            ),
        })
    if local_dirty and "push" in directions:
        apply = not remote_dirty and push_base_matches
        actions.append({
            "action": "push-wip",
            "snapshot_id": local_id,
            "apply": apply,
            "reason": (
                "concurrent-wip"
                if conflict
                else "destination-clean"
                if apply
                else "base-mismatch"
            ),
        })
    return actions
