"""Pure decision core for the prod-lane release-driven auto-update (#326, MADR-0024).

``scripts/prod-update.sh`` — the ``coach-web-update.timer`` entrypoint — is a thin
shell wrapper around this module. Every decision it makes lives here as a pure
function so it can be unit-tested without docker, the container registry, the
GitHub API or systemd:

* the C1 provenance gate (``digest(:prod) == digest(latest release tag)``),
* the step-3 digest comparison against the digest pinned in ``.env.prod``,
* the C4 ``.env.prod`` rewrite (exactly one image line, mode 600 before and
  after, tempfile + rename in the same directory, previous digest retained),
* the step-7 gate (3/3 healthy **and** ``/healthz`` on the release version),
* the C8 state file (last run, outcome, consecutive-failure streak).

The module resolves nothing itself: the shell performs the docker/HTTP I/O and
hands the results over as files or strings. It holds and prints no secret — only
the image digest, which is public and already recorded in the release notes.

CLI — one verb per decision, called by ``scripts/prod-update.sh``:

    prod_update.py release-tag  --release-json FILE
    prod_update.py compare      --env-file FILE --prod-digest DIGEST
    prod_update.py provenance   --prod-digest DIGEST --tag vX.Y.Z --tag-digest DIGEST
    prod_update.py rewrite      --env-file FILE (--digest DIGEST | --from-prev)
    prod_update.py state-check  --state-file FILE [--limit N]
    prod_update.py state-record --state-file FILE --outcome success|failure [--reason TAG]
    prod_update.py snapshot-check --bytes N [--artifact-bytes M]
    prod_update.py health-gate  --tag vX.Y.Z --ps-json FILE --healthz-json FILE

Exit codes: ``0`` the verb succeeded (and, for the gate verbs, the decision is
positive), ``1`` the decision is negative (provenance refused, update paused,
gate failed), ``2`` the input is malformed or unreadable — always fail closed.
"""

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
"""A resolved image digest — the only accepted pin format (C4/C2)."""

RELEASE_TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+")
"""A release tag: ``vX.Y.Z``. Anything else aborts the provenance gate (C1)."""

IMAGE_REPO = "ghcr.io/fpittelo/coach-web"
"""The prod lane's image repository (docs/admin_guide.md, GHCR table)."""

IMAGE_LINE_KEY = "COACH_WEB_IMAGE"
"""The single digest-pinned image line of ``.env.prod`` (C4)."""

IMAGE_REF_SEPARATOR = "@"
"""Digest reference separator: ``<repo>@sha256:<digest>``."""

PREV_ENV_SUFFIX = ".prev"
"""Suffix of the file retaining the previous digest for the rollback path (C4)."""

OWNER_ONLY_MODE = 0o600
"""Mode every file this module writes must have (C4/C8)."""

STATE_DIR_MODE = 0o700
"""Mode of the state directory (it holds the C8 state file only)."""

FAILURE_LIMIT = 12
"""Consecutive failures after which the updater pauses instead of hammering (C8)."""

SNAPSHOT_TMPFS_BYTES = 64 * 1024 * 1024
"""The coach-web container's ``/tmp`` tmpfs (compose.yaml: ``64m``) — the C6 staging area."""

FERNET_EXPANSION_NUM = 4
FERNET_EXPANSION_DEN = 3
"""Fernet/base64 expansion: the sealed artifact is ~4/3 of the raw database (C6)."""

EXPECTED_SERVICES = frozenset({"coach-web", "coach-mcp", "github-mcp"})
"""The exact prod-lane service set the health gate requires (AC3)."""

OUTCOMES = ("success", "failure")
"""The C8 run outcomes."""


class UpdateError(Exception):
    """Raised when an input is malformed or unusable — the caller must abort."""


# --------------------------------------------------------------------------- #
# Digests, env file and the C1 provenance gate
# --------------------------------------------------------------------------- #


def is_valid_digest(value: str) -> bool:
    """Return True when *value* is exactly ``sha256:<64 lowercase hex>``."""
    return DIGEST_PATTERN.fullmatch(value) is not None


def image_ref(digest: str) -> str:
    """Render the digest-pinned image reference recorded in ``.env.prod`` (C4)."""
    return f"{IMAGE_REPO}{IMAGE_REF_SEPARATOR}{digest}"


def find_env_image_line(content: str) -> tuple[int, str]:
    """Return the index and text of the single ``COACH_WEB_IMAGE=`` line.

    ``.env.prod`` must pin the image exactly once (C4): zero or several
    matching lines raise :class:`UpdateError` rather than guessing.
    """
    matches = [
        (index, line)
        for index, line in enumerate(content.split("\n"))
        if line.startswith(f"{IMAGE_LINE_KEY}=")
    ]
    if len(matches) != 1:
        raise UpdateError(
            f"{IMAGE_LINE_KEY} must appear exactly once in the lane env file, "
            f"found {len(matches)}"
        )
    return matches[0]


def digest_from_image_line(line: str) -> str:
    """Return the validated digest carried by a ``COACH_WEB_IMAGE=`` line."""
    prefix = f"{IMAGE_LINE_KEY}="
    value = line[len(prefix) :] if line.startswith(prefix) else line
    repo_prefix = f"{IMAGE_REPO}{IMAGE_REF_SEPARATOR}"
    if not value.startswith(repo_prefix):
        raise UpdateError(
            f"{IMAGE_LINE_KEY} must be digest-pinned as "
            f"{repo_prefix}sha256:<64 hex>, got {value!r}"
        )
    digest = value[len(repo_prefix) :]
    if not is_valid_digest(digest):
        raise UpdateError(f"{IMAGE_LINE_KEY} digest is malformed: {digest!r}")
    return digest


def env_digest(content: str) -> str:
    """Return the digest pinned in a lane env file's content (fail closed)."""
    _, line = find_env_image_line(content)
    return digest_from_image_line(line)


def compare_digest(content: str, prod_digest: str) -> str:
    """Decide MADR-0024 steps 2-3: ``no-change`` or ``update``.

    A malformed ``:prod`` digest or lane env file raises: the caller aborts
    before any pull, so a broken input can never recreate a container (C2).
    """
    if not is_valid_digest(prod_digest):
        raise UpdateError(
            f"resolved :prod digest is malformed (expected sha256:<64 hex>): {prod_digest!r}"
        )
    return "no-change" if env_digest(content) == prod_digest else "update"


def release_tag_from_api(payload: str) -> str:
    """Return the tag of a ``releases/latest`` payload, or raise (C1).

    The public Releases API returns the latest non-prerelease release; a
    payload without a ``vX.Y.Z`` ``tag_name`` (absent release, prerelease
    promoted by hand, truncated body) aborts the update.
    """
    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"releases/latest payload is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError("releases/latest payload is not a JSON object")
    tag = data.get("tag_name")
    if not isinstance(tag, str) or RELEASE_TAG_PATTERN.fullmatch(tag) is None:
        raise UpdateError(f"latest release tag is absent or not vX.Y.Z: {tag!r}")
    return tag


@dataclass(frozen=True)
class ProvenanceDecision:
    """Outcome of the C1 provenance gate."""

    allowed: bool
    reason: str


def decide_provenance(*, prod_digest: str, tag: str, tag_digest: str) -> ProvenanceDecision:
    """Decide MADR-0024 step 4 (C1): ``:prod`` must be the released tag's digest.

    Decision table — every uncertain input aborts, none of them passes:

    * ``:prod`` digest empty or malformed          -> abort
    * release tag absent or not ``vX.Y.Z``         -> abort
    * released tag digest empty or malformed       -> abort
    * ``digest(:prod) != digest(:vX.Y.Z)``         -> abort
    * equal                                        -> allowed
    """
    if not is_valid_digest(prod_digest):
        return ProvenanceDecision(False, f":prod digest is malformed: {prod_digest!r}")
    if RELEASE_TAG_PATTERN.fullmatch(tag) is None:
        return ProvenanceDecision(False, f"release tag is absent or not vX.Y.Z: {tag!r}")
    if not is_valid_digest(tag_digest):
        return ProvenanceDecision(False, f"digest of {tag} is missing or malformed: {tag_digest!r}")
    if prod_digest != tag_digest:
        return ProvenanceDecision(
            False,
            f"provenance mismatch: :prod is {prod_digest} but {tag} is {tag_digest}",
        )
    return ProvenanceDecision(True, f"digest(:prod) == digest({tag}) == {prod_digest}")


# --------------------------------------------------------------------------- #
# C4: the atomic .env.prod rewrite
# --------------------------------------------------------------------------- #


def rewrite_env_content(content: str, digest: str) -> str:
    """Return *content* with its single image line pinned to *digest* (C4).

    Line structure is preserved byte for byte apart from that one line, so the
    atomic rewrite cannot drop a secret, a comment or a trailing newline.
    """
    if not is_valid_digest(digest):
        raise UpdateError(f"refusing to pin a malformed digest: {digest!r}")
    index, _ = find_env_image_line(content)
    lines = content.split("\n")
    lines[index] = f"{IMAGE_LINE_KEY}={image_ref(digest)}"
    return "\n".join(lines)


def _assert_owner_only(path: Path) -> None:
    """Assert *path* exists, is owned by this effective uid, and is mode 600."""
    info = path.stat()
    if info.st_uid != os.geteuid():
        raise UpdateError(
            f"{path} is owned by uid {info.st_uid}, not this user (euid {os.geteuid()}) — C4"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode != OWNER_ONLY_MODE:
        raise UpdateError(f"{path} is mode {mode:04o}, not 0600 — C4")


def _write_owner_only(path: Path, content: str) -> None:
    """Write *content* to *path* atomically with mode 600, then re-assert it (C4).

    Temp file in the same directory + ``os.replace``: a reader (docker compose
    resolving ``--env-file``) sees either the complete old file or the complete
    new one, never a half-written image line.
    """
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(handle_fd, OWNER_ONLY_MODE)
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    _assert_owner_only(path)


def _read_text(path: Path) -> str:
    """Read *path* as UTF-8 text, converting OS errors into :class:`UpdateError`."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError(f"cannot read {path}: {exc}") from exc


def previous_env_path(env_path: Path) -> Path:
    """Return the path retaining the previous image line (``<env>.prev``, C4)."""
    return env_path.with_name(env_path.name + PREV_ENV_SUFFIX)


def write_env_digest(env_path: Path, digest: str) -> str:
    """Pin *digest* in *env_path*, retaining the previous line, and return it (C4).

    Order matters: the previous ``COACH_WEB_IMAGE`` line is written to
    ``<env>.prev`` (mode 600) **before** the env file is replaced, so a crash
    between the two steps still leaves the rollback anchor on disk. The env file
    must already be owner-only and ours — a foreign or group-readable lane env
    file is refused rather than rewritten.
    """
    _assert_owner_only(env_path)
    content = _read_text(env_path)
    previous = env_digest(content)
    _, previous_line = find_env_image_line(content)
    prev_path = previous_env_path(env_path)
    _write_owner_only(prev_path, previous_line + "\n")
    _write_owner_only(env_path, rewrite_env_content(content, digest))
    return previous


def restore_env_digest(env_path: Path) -> str:
    """Restore the digest retained in ``<env>.prev`` and return it (rollback path).

    Idempotent: the ``.prev`` file is left untouched, so re-running the rollback
    re-applies the same previous release rather than flipping between digests.
    The env file is asserted owner-only before *and* after, exactly as the swap
    path does (C4).
    """
    _assert_owner_only(env_path)
    prev_path = previous_env_path(env_path)
    previous_digest = env_digest(_read_text(prev_path))
    _write_owner_only(env_path, rewrite_env_content(_read_text(env_path), previous_digest))
    return previous_digest


# --------------------------------------------------------------------------- #
# C6: the pre-swap snapshot guards
# --------------------------------------------------------------------------- #


def snapshot_artifact_bytes(raw_bytes: int) -> int:
    """Estimate the sealed artifact size for a *raw_bytes* database.

    The artifact is a Fernet envelope: base64 expands the payload by 4/3, plus a
    few bytes of version/timestamp/HMAC. Rounded up, so the estimate is an upper
    bound for the tmpfs pre-flight (C6).
    """
    return -(-raw_bytes * FERNET_EXPANSION_NUM // FERNET_EXPANSION_DEN)


def snapshot_fits(
    raw_bytes: int, *, capacity_bytes: int = SNAPSHOT_TMPFS_BYTES
) -> tuple[bool, str]:
    """C6 pre-flight: the sealed snapshot must fit the container's ``/tmp`` tmpfs.

    The artifact is staged on the container's tmpfs before being streamed out, so
    the effective ceiling is the tmpfs size divided by the 4/3 encryption
    expansion (~48 MB of database for the 64 MB tmpfs). A larger database aborts
    with an actionable message instead of failing later inside the backup with a
    generic encryption/WAL error.
    """
    if raw_bytes <= 0:
        return False, "the database size is unknown (0 bytes) — refusing to snapshot blindly"
    artifact = snapshot_artifact_bytes(raw_bytes)
    if artifact > capacity_bytes:
        return False, (
            f"database too large for an in-container snapshot: {raw_bytes} B of database seal "
            f"to ~{artifact} B, beyond the {capacity_bytes} B /tmp tmpfs — restore from the "
            "off-host backup instead (docs/admin_guide.md, Database Maintenance)"
        )
    return True, (
        f"snapshot fits: {raw_bytes} B raw -> ~{artifact} B sealed (tmpfs {capacity_bytes} B)"
    )


def snapshot_integrity_ok(*, raw_bytes: int, artifact_bytes: int) -> tuple[bool, str]:
    """C6 post-copy: the extracted artifact must be present and larger than the source.

    A successful extraction stream is not evidence by itself: an empty or
    truncated extraction must fail the update while ``.env.prod`` is still
    untouched. Fernet always expands the payload, so an artifact no larger than
    the raw database cannot be the sealed envelope.
    """
    if artifact_bytes <= 0:
        return False, "the extracted snapshot is empty — refusing to continue (C6)"
    if artifact_bytes <= raw_bytes:
        return False, (
            f"the extracted snapshot ({artifact_bytes} B) is not larger than the source database "
            f"({raw_bytes} B): the sealed envelope is missing or the copy is truncated (C6)"
        )
    return True, f"snapshot integrity ok: {artifact_bytes} B artifact for a {raw_bytes} B database"


# --------------------------------------------------------------------------- #
# AC3: the post-swap health gate
# --------------------------------------------------------------------------- #


def _ps_entries(payload: str) -> list[dict[str, Any]]:
    """Parse ``docker compose ps --format json`` (a JSON array or JSON lines)."""
    raw = payload.strip()
    if not raw:
        return []
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        entries: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                item: Any = json.loads(line)
            except json.JSONDecodeError:
                return []
            if isinstance(item, dict):
                entries.append(item)
        return entries
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def services_healthy(payload: str) -> tuple[bool, str]:
    """Check the exact service set is present and every service is healthy (AC3)."""
    entries = _ps_entries(payload)
    present = {str(entry.get("Service")) for entry in entries}
    if present != EXPECTED_SERVICES:
        missing = sorted(EXPECTED_SERVICES - present)
        unexpected = sorted(present - EXPECTED_SERVICES)
        return False, f"service set mismatch (missing: {missing}; unexpected: {unexpected})"
    unhealthy = sorted(
        str(entry.get("Service")) for entry in entries if entry.get("Health") != "healthy"
    )
    if unhealthy:
        return False, f"services not healthy: {unhealthy}"
    return True, "3/3 services healthy (coach-web, coach-mcp, github-mcp)"


def version_matches(payload: str, tag: str) -> tuple[bool, str]:
    """Check ``/healthz`` reports the release tag without its leading ``v`` (AC3)."""
    expected = tag[1:] if tag.startswith("v") else tag
    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        return False, f"/healthz payload is not JSON: {exc}"
    version = data.get("version") if isinstance(data, dict) else None
    if version != expected:
        return False, f"/healthz version {version!r} != release tag {tag!r}"
    return True, f"/healthz version {version} == {tag}"


def health_gate(*, ps_json: str, healthz_json: str, tag: str) -> tuple[bool, str]:
    """Decide MADR-0024 step 7: 3/3 healthy **and** ``/healthz`` on the release."""
    healthy, health_detail = services_healthy(ps_json)
    if not healthy:
        return False, health_detail
    matched, version_detail = version_matches(healthz_json, tag)
    if not matched:
        return False, version_detail
    return True, f"{health_detail}; {version_detail}"


# --------------------------------------------------------------------------- #
# C8: state file and failure streak
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UpdateState:
    """The C8 bookkeeping: last run, its outcome, the failure streak and reason.

    ``reason`` is a short machine-readable tag for a *failed* run (e.g.
    ``checkout-guard``, #344); it is ``None`` when a run did not carry one (a
    success, or a failure the caller did not tag).
    """

    last_run: str
    outcome: str
    consecutive_failures: int
    reason: str | None = None


def read_state(path: Path) -> UpdateState | None:
    """Read the state file; a missing or unusable file means "no history" (C8).

    The state file is a status signal, not a gate: a corrupt one is reported by
    the caller and treated as a first run rather than blocking the lane forever.
    """
    if not path.is_file():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        last_run: Any = data["last_run"]
        outcome: Any = data["outcome"]
        streak: Any = data["consecutive_failures"]
        reason: Any = data.get("reason")
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(last_run, str) or outcome not in OUTCOMES:
        return None
    if isinstance(streak, bool) or not isinstance(streak, int) or streak < 0:
        return None
    if reason is not None and not isinstance(reason, str):
        return None
    return UpdateState(
        last_run=last_run, outcome=outcome, consecutive_failures=streak, reason=reason
    )


def next_state(
    previous: UpdateState | None, *, outcome: str, now: str, reason: str | None = None
) -> UpdateState:
    """Return the state after a run with *outcome*: a success resets the streak.

    *reason* tags a failed run (e.g. ``checkout-guard``); a success never carries
    one. The streak accumulates on failure and resets on success.
    """
    if outcome not in OUTCOMES:
        raise UpdateError(f"unknown outcome {outcome!r} (expected one of {OUTCOMES})")
    streak = 0
    if outcome == "failure":
        streak = (previous.consecutive_failures if previous is not None else 0) + 1
    return UpdateState(
        last_run=now,
        outcome=outcome,
        consecutive_failures=streak,
        reason=reason if outcome == "failure" else None,
    )


def should_halt(state: UpdateState | None, *, limit: int = FAILURE_LIMIT) -> bool:
    """Return True when the failure streak reached *limit* (C8: stop hammering)."""
    return state is not None and state.consecutive_failures >= limit


def write_state(path: Path, state: UpdateState) -> None:
    """Persist the state file atomically, owner-only (C8)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=STATE_DIR_MODE)
    payload = (
        json.dumps(
            {
                "last_run": state.last_run,
                "outcome": state.outcome,
                "consecutive_failures": state.consecutive_failures,
                "reason": state.reason,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    _write_owner_only(path, payload)


def now_utc() -> str:
    """Current UTC timestamp (seconds precision) for the C8 state file."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _release_tag(args: argparse.Namespace) -> int:
    """`release-tag`: print the latest release tag or fail closed."""
    print(release_tag_from_api(_read_text(Path(args.release_json))))
    return 0


def _compare(args: argparse.Namespace) -> int:
    """`compare`: print `no-change` or `update` for the pinned vs resolved digest."""
    print(compare_digest(_read_text(Path(args.env_file)), args.prod_digest))
    return 0


def _provenance(args: argparse.Namespace) -> int:
    """`provenance`: print the C1 verdict; exit non-zero when refused."""
    decision = decide_provenance(
        prod_digest=args.prod_digest, tag=args.tag, tag_digest=args.tag_digest
    )
    print(f"{'match' if decision.allowed else 'abort'}: {decision.reason}")
    return 0 if decision.allowed else 1


def _rewrite(args: argparse.Namespace) -> int:
    """`rewrite`: pin the new digest, or restore the retained previous one (C4)."""
    env_path = Path(args.env_file)
    if args.from_prev:
        digest = restore_env_digest(env_path)
        print(f"restored {IMAGE_LINE_KEY} from {previous_env_path(env_path).name} -> {digest}")
        return 0
    previous = write_env_digest(env_path, args.digest)
    print(
        f"pinned {IMAGE_LINE_KEY} -> {args.digest} (previous {previous} retained in "
        f"{previous_env_path(env_path).name}, mode 600)"
    )
    return 0


def _state_check(args: argparse.Namespace) -> int:
    """`state-check`: exit non-zero (without touching the lane) when paused (C8)."""
    state = read_state(Path(args.state_file))
    if state is None:
        print("ok: no previous run recorded")
        return 0
    if should_halt(state, limit=args.limit):
        print(
            f"halted: {state.consecutive_failures} consecutive failures "
            f"(limit {args.limit}); last run {state.last_run} ({state.outcome})"
        )
        return 1
    print(f"ok: streak {state.consecutive_failures}/{args.limit}, last run {state.last_run}")
    return 0


def _state_record(args: argparse.Namespace) -> int:
    """`state-record`: append this run's outcome to the C8 state file."""
    state = next_state(
        read_state(Path(args.state_file)), outcome=args.outcome, now=now_utc(), reason=args.reason
    )
    write_state(Path(args.state_file), state)
    suffix = " — auto-update will pause until a --force run" if should_halt(state) else ""
    reason = f" reason={state.reason}" if state.reason else ""
    print(
        f"outcome={state.outcome}{reason} consecutive_failures={state.consecutive_failures} "
        f"last_run={state.last_run}{suffix}"
    )
    return 0


def _snapshot_check(args: argparse.Namespace) -> int:
    """`snapshot-check`: C6 pre-flight ceiling, or post-copy integrity."""
    if args.artifact_bytes is None:
        ok, detail = snapshot_fits(args.bytes)
    else:
        ok, detail = snapshot_integrity_ok(raw_bytes=args.bytes, artifact_bytes=args.artifact_bytes)
    print(detail)
    return 0 if ok else 1


def _health_gate(args: argparse.Namespace) -> int:
    """`health-gate`: print PASS/FAIL for 3/3 healthy + release version (AC3)."""
    passed, detail = health_gate(
        ps_json=_read_text(Path(args.ps_json)),
        healthz_json=_read_text(Path(args.healthz_json)),
        tag=args.tag,
    )
    print(f"{'PASS' if passed else 'FAIL'}: {detail}")
    return 0 if passed else 1


VERBS = {
    "release-tag": _release_tag,
    "compare": _compare,
    "provenance": _provenance,
    "rewrite": _rewrite,
    "state-check": _state_check,
    "state-record": _state_record,
    "snapshot-check": _snapshot_check,
    "health-gate": _health_gate,
}
"""The CLI verbs, one per decision (see the module docstring)."""


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser: one subcommand per decision."""
    parser = argparse.ArgumentParser(
        prog="prod_update.py",
        description="Pure decision core of the prod-lane auto-update (#326, MADR-0024).",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    release_tag = sub.add_parser("release-tag", help="Print the latest release tag.")
    release_tag.add_argument("--release-json", required=True, help="releases/latest payload.")

    compare = sub.add_parser("compare", help="no-change | update against .env.prod.")
    compare.add_argument("--env-file", required=True, help="Lane env file (read only).")
    compare.add_argument("--prod-digest", required=True, help="Resolved :prod digest.")

    provenance = sub.add_parser("provenance", help="C1: :prod == latest release digest.")
    provenance.add_argument("--prod-digest", required=True, help="Resolved :prod digest.")
    provenance.add_argument("--tag", required=True, help="Latest release tag (vX.Y.Z).")
    provenance.add_argument("--tag-digest", required=True, help="Digest resolved for that tag.")

    rewrite = sub.add_parser("rewrite", help="C4: pin (or restore) the image line.")
    rewrite.add_argument("--env-file", required=True, help="Lane env file to rewrite.")
    target = rewrite.add_mutually_exclusive_group(required=True)
    target.add_argument("--digest", help="New digest to pin.")
    target.add_argument(
        "--from-prev",
        action="store_true",
        help="Restore the digest retained in <env>.prev (rollback).",
    )

    state_check = sub.add_parser("state-check", help="C8: exit non-zero when paused.")
    state_check.add_argument("--state-file", required=True, help="C8 state file.")
    state_check.add_argument("--limit", type=int, default=FAILURE_LIMIT, help="Failure limit.")

    state_record = sub.add_parser("state-record", help="C8: record this run's outcome.")
    state_record.add_argument("--state-file", required=True, help="C8 state file.")
    state_record.add_argument("--outcome", required=True, choices=OUTCOMES, help="Run outcome.")
    state_record.add_argument(
        "--reason",
        default=None,
        help="Short machine-readable tag for a failed run (e.g. checkout-guard, #344).",
    )

    snapshot = sub.add_parser("snapshot-check", help="C6: snapshot ceiling / integrity.")
    snapshot.add_argument("--bytes", type=int, required=True, help="Raw database size in bytes.")
    snapshot.add_argument(
        "--artifact-bytes",
        type=int,
        default=None,
        help="Extracted artifact size; switches the verb to the post-copy integrity check.",
    )

    gate = sub.add_parser("health-gate", help="AC3: 3/3 healthy + release version.")
    gate.add_argument("--tag", required=True, help="Release tag the lane must report.")
    gate.add_argument("--ps-json", required=True, help="`docker compose ps -a --format json`.")
    gate.add_argument("--healthz-json", required=True, help="/healthz payload.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one verb; exit 2 on any malformed or unreadable input (fail closed)."""
    args = _build_parser().parse_args(argv)
    handler = VERBS[args.verb]
    try:
        return handler(args)
    except UpdateError as exc:
        print(f"prod_update: {args.verb}: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"prod_update: {args.verb}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
