#!/usr/bin/env python3
"""Loopback publisher assertion for scripts/e2e-preflight.sh (ADR-007, #138).

Reads the ``docker compose ps -a --format json`` stream on stdin and fails
when host-port publishing violates the loopback-only boundary:

- the web service (argv[1], e.g. ``coach-web``) may publish ONLY on
  127.0.0.1;
- every other service must publish NO host port at all.

Docker Compose v5.1.3 (Docker Desktop ``ports.scheme=v2``) emits phantom
publisher entries with ``PublishedPort == 0`` and an empty ``URL`` for
exposed-but-unpublished ports. A publisher with ``PublishedPort == 0`` is an
exposed-port marker, not a host publication, and is filtered out for ALL
services before any violation check (#138).

Violations are printed to stderr. The exit code is 1 when any violation is
found, 0 otherwise, and 2 on usage error.

Input shape: either JSON lines (one object per container, the compose
default) or a single JSON array — both are accepted.
"""

import json
import sys
from collections.abc import Iterable, Sequence
from typing import Any

LOOPBACK = "127.0.0.1"


def parse_entries(raw: str) -> list[dict[str, Any]]:
    """Parse the compose ps stream into per-container mappings.

    Accepts both input shapes observed across compose versions: JSON lines
    (one object per line) and a single JSON array.
    """
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        entries: list[dict[str, Any]] = [
            json.loads(line) for line in text.splitlines() if line.strip()
        ]
        return entries
    if isinstance(parsed, list):
        entries = [entry for entry in parsed if isinstance(entry, dict)]
        return entries
    return [parsed]


def collect_violations(entries: Iterable[dict[str, Any]], web_service: str) -> list[str]:
    """Return one violation message per host-port publishing breach.

    Publishers with ``PublishedPort == 0`` are phantom entries (exposed-port
    markers emitted by compose ports.scheme=v2) and are ignored for every
    service (#138). Real publications (``PublishedPort > 0``) must respect
    the loopback-only boundary: only ``web_service`` may publish, and only
    on 127.0.0.1.
    """
    violations: list[str] = []
    for entry in entries:
        service = str(entry.get("Service", "<unknown>"))
        publishers = entry.get("Publishers") or []
        real = [publisher for publisher in publishers if (publisher.get("PublishedPort") or 0) != 0]
        if service == web_service:
            for publisher in real:
                host_ip = str(
                    publisher.get("PublishedIP")
                    or publisher.get("URL")
                    or publisher.get("IP")
                    or ""
                )
                published_port = publisher.get("PublishedPort") or 0
                if host_ip != LOOPBACK:
                    violations.append(
                        f"{service} published on {host_ip}:{published_port} (must be {LOOPBACK})"
                    )
        elif real:
            violations.append(f"{service} publishes host ports but must publish none")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Read the ps stream on stdin; return 1 on violations, 0 when clean."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if len(arguments) != 1:
        print("usage: preflight_loopback_check.py <web-service-name>", file=sys.stderr)
        return 2
    violations = collect_violations(parse_entries(sys.stdin.read()), arguments[0])
    for violation in violations:
        print(violation, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
