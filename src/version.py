"""App version string -- shown in the web UI sidebar and in diagnostics bundles.

Version = the release tag, CalVer `vYYYY.MM.DD[.N]`, with the leading `v` stripped.
Three sources, tried in order:

1. **git-archive export-subst**: `_ARCHIVE` below is rewritten by `git archive` (wired
   up via `.gitattributes`: `src/version.py export-subst`) into the tag-based describe
   string, e.g. "v2026.10.23" or "v2026.10.23-3-gabc1234". Any ZIP built with `git
   archive` -- including `tools/make_release.py`'s output and GitHub's "Code >
   Download ZIP" -- carries this baked-in version with no git executable required at
   runtime.
2. **git describe** against this repo's own working tree: used when running from a
   plain git checkout (a dev clone, not a `git archive` ZIP) where `_ARCHIVE` is still
   the literal unsubstituted `...` token.
3. **Fallback string** ("geliştirme sürümü" -- "development version") when neither of
   the above applies, e.g. the file was copied out of git entirely with no `.git` in
   reach, or the checkout has no tags at all.

Never raises; never touches the network. All subprocess calls are capped at a 2 second
timeout so a slow/missing git never holds up app startup.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Rewritten by `git archive` when `.gitattributes` marks this path `export-subst`.
# Left untouched (literal "...") in a plain checkout or a non-archive copy.
_ARCHIVE = "v2026.10.24"

ROOT = Path(__file__).resolve().parent.parent

_FALLBACK = "geliştirme sürümü"

_GIT_TIMEOUT_S = 2.0


def get_version(root: Path | None = None) -> str:
    """Best-effort human-readable version string. Never raises.

    `root` defaults to this project's own root; overridable for tests.
    """
    archived = _from_archive(_ARCHIVE)
    if archived is not None:
        return archived

    root = Path(root) if root is not None else ROOT
    dev = _from_git(root)
    if dev is not None:
        return dev

    return _FALLBACK


def _from_archive(raw: str) -> str | None:
    """Strip the leading `v` off the export-subst payload, or None if it's still the
    literal placeholder (i.e. this isn't a `git archive` checkout) or empty."""
    if not raw or raw.startswith("$Format"):
        return None
    return raw[1:] if raw.startswith("v") else raw


def _from_git(root: Path) -> str | None:
    """Tag-based describe string (leading `v` stripped) from a live git checkout, or
    None if git is unavailable, `root` isn't a git repo, or the call fails/times out."""
    described = _run(["git", "-C", str(root), "describe", "--tags", "--always", "--dirty"])
    if described is None:
        return None
    return described[1:] if described.startswith("v") else described


def parse_version(s: str | None) -> tuple[int, ...] | None:
    """Parse the numeric `X.Y.Z[.N]` prefix (before the first `-`) into a tuple of
    ints for comparison, or None if `s` is None or has no such prefix (e.g. the
    "geliştirme sürümü" fallback or an untagged-commit sha)."""
    if not s:
        return None
    head = s.split("-", 1)[0]
    parts = head.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _run(args: list[str]) -> str | None:
    """Run a subprocess, returning stripped stdout or None on any failure/timeout.
    Never raises."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


if __name__ == "__main__":
    print(get_version())
