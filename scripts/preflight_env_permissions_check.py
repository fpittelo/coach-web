#!/usr/bin/env python3
"""Lane env-file permission assertion for scripts/e2e-preflight.sh (MADR-0008 C3, #170).

Every lane env file passed on the command line must be mode 600 (owner
read/write only). A group- or world-readable lane env file exposes the lane's
secrets (``OPENROUTER_API_KEY``, ``GITHUB_TOKEN``, ``INTERVALS_API_KEY``,
``AUTH_SESSION_SECRET``, ...) to every local user — the #112 posture claims
600, and MADR-0008 C3 requires the enforcement (including the future
``DATA_ENCRYPTION_KEY`` file).

Paths that do not exist are skipped: the caller (``scripts/e2e-preflight.sh``)
already fails fast on a missing lane env file, and this assertion must not
invent a second, divergent missing-file policy.

Violations are printed to stderr. The exit code is 1 when any violation is
found, 0 otherwise, and 2 on usage error.
"""

import stat
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

REQUIRED_MODE = 0o600


def collect_violations(paths: Iterable[Path]) -> list[str]:
    """Return one violation message per existing env file not at mode 600.

    Non-existent paths are skipped (the caller owns the missing-file policy).
    """
    violations: list[str] = []
    for path in paths:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            continue
        if mode != REQUIRED_MODE:
            violations.append(f"{path} is mode {mode:03o} (must be {REQUIRED_MODE:03o})")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Check every path argument; return 1 on violations, 0 when clean."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if not arguments:
        print(
            "usage: preflight_env_permissions_check.py <env-file> [env-file ...]",
            file=sys.stderr,
        )
        return 2
    violations = collect_violations(Path(argument) for argument in arguments)
    for violation in violations:
        print(violation, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
