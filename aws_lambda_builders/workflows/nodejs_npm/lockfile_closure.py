"""
Derive a workspace member's production dependency closure from the lockfile, with no npm subprocess.

Replaces `npm ls --all --parseable --omit=dev` for the #933 artifact-narrowing step. Only
lockfileVersion 2 and 3 carry the `packages` map this needs; version 1 has no per-path entries, so
callers fall back to asking npm.

Returns the same thing npm's --parseable output gives: absolute directories, real paths, one per
resolved package -- including workspace dependencies reported as their own source directory.
"""

import json
import logging
import os
from typing import Dict, List, Optional, Set

LOG = logging.getLogger(__name__)

# lockfileVersion 1 predates the per-path `packages` map this resolver reads
MINIMUM_LOCKFILE_VERSION = 2


def _resolve_key(packages: Dict, from_key: str, name: str) -> Optional[str]:
    """
    Find the packages-map key that `name` resolves to when required from `from_key`.

    Mirrors node's resolution: walk up the directory chain looking for node_modules/<name>, which in
    the lockfile is spelled as a key. `from_key` is "" for the root, "endpoints/orders" for a
    workspace member, or "node_modules/express" for an installed package.
    """
    prefix = from_key
    while True:
        candidate = f"{prefix}/node_modules/{name}" if prefix else f"node_modules/{name}"
        if candidate in packages:
            return candidate
        if not prefix:
            return None
        # strip one path segment and try again, which is what walking up node_modules means
        prefix = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        if prefix.endswith("/node_modules"):
            prefix = prefix[: -len("/node_modules")]


def _entry_dir(project_root: str, key: str, entry: Dict) -> str:
    """The directory a packages-map entry lives in."""
    # a workspace link points at the real source directory rather than the link's own path
    if entry.get("link") and entry.get("resolved"):
        return os.path.join(project_root, *entry["resolved"].split("/"))
    return os.path.join(project_root, *key.split("/")) if key else project_root


def production_closure(project_root: str, install_dir: str, lockfile_path: str) -> Optional[List[str]]:
    """
    The production dependency closure of `install_dir`, as absolute directories.

    Returns None when the lockfile cannot answer -- an unsupported version, an unreadable file, or an
    install directory the lockfile does not describe -- so the caller can fall back to npm.
    """
    try:
        with open(lockfile_path) as handle:
            lock = json.load(handle)
    except (OSError, ValueError):
        return None

    if lock.get("lockfileVersion", 1) < MINIMUM_LOCKFILE_VERSION or "packages" not in lock:
        return None

    packages = lock["packages"]
    project_root = os.path.realpath(project_root)
    rel = os.path.relpath(os.path.realpath(install_dir), project_root).replace(os.sep, "/")
    start = "" if rel == "." else rel
    if start not in packages:
        return None

    # a link entry for a workspace member carries no dependencies; its source entry does
    def deps_of(key: str) -> Dict[str, str]:
        entry = packages.get(key) or {}
        if entry.get("link") and entry.get("resolved"):
            entry = packages.get(entry["resolved"], entry)
        # production only: dependencies plus optional, never devDependencies
        out = dict(entry.get("dependencies") or {})
        out.update(entry.get("optionalDependencies") or {})
        return out

    found: Set[str] = set()
    stack = [start]
    while stack:
        key = stack.pop()
        for name in deps_of(key):
            resolved = _resolve_key(packages, key, name)
            if resolved is None or resolved in found:
                continue
            entry = packages[resolved]
            if entry.get("dev"):
                continue
            found.add(resolved)
            # a link's dependencies must be resolved from its source directory, not from the
            # link's own node_modules path: walking up from the link finds the hoisted copy of
            # a package the source directory nests (node resolves through the real path too)
            if entry.get("link") and entry.get("resolved"):
                stack.append(entry["resolved"])
            else:
                stack.append(resolved)

    # An entry can be in the lockfile without being on disk: an optional dependency skipped for this
    # platform (fsevents is darwin-only), or one whose os/cpu/libc constraints excluded it. npm ls
    # reports the installed tree, so match that and keep only what is actually there. A required
    # dependency missing here means the install itself failed, which the install step surfaces.
    dirs = set()
    for key in found:
        directory = _entry_dir(project_root, key, packages[key])
        if os.path.isdir(directory):
            dirs.add(directory)
        else:
            LOG.debug("NODEJS lockfile lists %s but it is not installed; leaving it out", key)
    # npm --parseable also prints the project itself and the install dir; the caller filters those,
    # so emit them for parity
    dirs.add(project_root)
    dirs.add(os.path.realpath(install_dir))
    return sorted(dirs)
