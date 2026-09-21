"""Copy the translation resources to the site, so there is one source of truth.

WHY THIS EXISTS
---------------
The same strings are needed in two places that are deployed separately. The
Lambda bundle is `backend/`, so it can only read files under `backend/`. The
site bundle is `frontend/site/`, so the browser can only fetch files under
that. Neither can reach into the other.

The alternative to copying is maintaining two sets of translations, and two
sets of translations become two different translations - usually at the worst
moment, when somebody fixes a sentence in one of them. So `backend/i18n/` is
the source, `frontend/site/i18n/` is a copy, and a test fails the build if
they ever drift apart.

    python scripts/sync_i18n.py           # copy, and report what changed
    python scripts/sync_i18n.py --check   # fail if they differ, change nothing

The `--check` form is what the test suite uses.
"""

from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backend" / "i18n"
TARGET = ROOT / "frontend" / "site" / "i18n"


def files() -> list:
    return sorted(SOURCE.glob("*.json"))


def differences() -> list:
    """Every resource that is missing from, or differs in, the site copy."""
    out = []
    for path in files():
        mirror = TARGET / path.name
        if not mirror.exists():
            out.append((path.name, "missing"))
        elif not filecmp.cmp(path, mirror, shallow=False):
            out.append((path.name, "differs"))
    # A file that exists only on the site side is drift too: it would be
    # served to a browser while no Lambda has ever seen it.
    for mirror in sorted(TARGET.glob("*.json")):
        if not (SOURCE / mirror.name).exists():
            out.append((mirror.name, "orphaned"))
    return out


def sync() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    changed = differences()
    for name, state in changed:
        if state == "orphaned":
            (TARGET / name).unlink()
            print(f"  removed {name} (no longer in backend/i18n)")
            continue
        shutil.copy2(SOURCE / name, TARGET / name)
        print(f"  {state:8s} -> copied {name}")
    if not changed:
        print("  already in sync")
    print(f"\n{len(files())} language files in {TARGET}")
    return 0


def check() -> int:
    changed = differences()
    if not changed:
        print(f"OK: {len(files())} language files are in sync")
        return 0
    print("DRIFT between backend/i18n and frontend/site/i18n:")
    for name, state in changed:
        print(f"  {name}: {state}")
    print("\nRun: python scripts/sync_i18n.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(check() if "--check" in sys.argv else sync())
