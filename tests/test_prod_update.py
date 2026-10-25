"""Tests for the prod-lane release-driven auto-update (#326, MADR-0024).

Two layers, mirroring ``tests/test_verify_icu_crud.py`` (pure core loaded from
``scripts/``) and ``tests/integration/test_prod_install.py`` (shell sandbox):

- the pure decision core ``scripts/prod_update.py`` — digest comparison, the C1
  provenance decision table, the C4 ``.env.prod`` rewrite (single line, mode 600,
  same-directory tempfile + rename), the AC3 health gate and the C8 failure
  streak — driven directly and through its CLI contract;
- the shell entrypoint ``scripts/prod-update.sh`` in a temp-HOME sandbox with a
  recording docker shim: the C3 shared-lock skip and the C2 fail-safe aborts
  (no pull, no recreate).

The real lane, the container registry, the GitHub API and systemd are never
touched here; the live proofs (a real release rollout, a reboot) belong to #329.
"""

import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import IO

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CORE_PATH = SCRIPTS_DIR / "prod_update.py"
ENTRYPOINT = SCRIPTS_DIR / "prod-update.sh"
CHECK_GUARD = SCRIPTS_DIR / "repo-clean-check.sh"
PREFLIGHT = SCRIPTS_DIR / "e2e-preflight.sh"
LANE_UNIT_TEMPLATE = PROJECT_ROOT / "deploy" / "systemd" / "coach-web.service.in"

PROD_LANE_LOCK = "coach-web-prod.lock"
RELEASES_API = "https://api.github.com/repos/fpittelo/coach-web/releases/latest"
HEALTHZ_URL = "http://127.0.0.1:8000/healthz"

ALLOWED_COMPOSE_VERBS = frozenset({"ps", "exec", "up", "down"})
"""The `dc` helper's verbs: none of them ever prints the interpolated environment (C4).

`cp` is banned outright (#342): compose cp resolves paths on the container rootfs
and cannot traverse the /tmp tmpfs the C6 artifact is staged on — the artifact is
streamed out with `exec -T cat` instead.
"""

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
IMAGE_LINE = "COACH_WEB_IMAGE=ghcr.io/fpittelo/coach-web@"
LAST_RUN = "2026-10-25T00:00:00Z"


def _load_core() -> ModuleType:
    """Load scripts/prod_update.py as a module."""
    assert CORE_PATH.is_file(), "scripts/prod_update.py must exist"
    spec = importlib.util.spec_from_file_location("prod_update", CORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORE = _load_core()
UpdateError = CORE.UpdateError


def _env_content(digest: str = DIGEST_A, *, extra: str = "OPENROUTER_API_KEY=x\n") -> str:
    """A minimal lane env file body pinning *digest*."""
    return f"# comment\n{IMAGE_LINE}{digest}\n{extra}"


# --------------------------------------------------------------------------- #
# Digests, env parsing and the step-3 comparison
# --------------------------------------------------------------------------- #


class TestDigestAndEnvParsing:
    """Digest validation, single-image-line parsing and the no-op comparison."""

    @pytest.mark.parametrize(
        "value",
        ["sha256:" + "0" * 64, "sha256:" + "abcdef" * 10 + "abcd"],
    )
    def test_valid_digest_accepts_lowercase_hex(self, value: str) -> None:
        assert CORE.is_valid_digest(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "sha256:",
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha512:" + "a" * 64,
            "sha256:" + "g" * 64,
            "latest",
        ],
    )
    def test_valid_digest_rejects_anything_else(self, value: str) -> None:
        assert CORE.is_valid_digest(value) is False

    def test_env_digest_returns_the_pinned_digest(self) -> None:
        assert CORE.env_digest(_env_content(DIGEST_A)) == DIGEST_A

    @pytest.mark.parametrize(
        "content",
        [
            "OPENROUTER_API_KEY=x\n",
            _env_content(DIGEST_A) + f"{IMAGE_LINE}{DIGEST_B}\n",
            "# COACH_WEB_IMAGE=ghcr.io/fpittelo/coach-web@sha256-x\n",
        ],
    )
    def test_env_digest_fails_closed_without_exactly_one_image_line(self, content: str) -> None:
        with pytest.raises(UpdateError):
            CORE.env_digest(content)

    @pytest.mark.parametrize(
        "value",
        [
            "ghcr.io/fpittelo/coach-web:prod",
            "ghcr.io/fpittelo/coach-web@sha256:" + "a" * 63,
            "ghcr.io/other/thing@sha256:" + "a" * 64,
            "",
        ],
    )
    def test_env_digest_fails_closed_on_a_non_digest_pin(self, value: str) -> None:
        with pytest.raises(UpdateError):
            CORE.env_digest(f"COACH_WEB_IMAGE={value}\n")

    def test_compare_digest_is_a_no_change_when_the_pin_matches(self) -> None:
        assert CORE.compare_digest(_env_content(DIGEST_A), DIGEST_A) == "no-change"

    def test_compare_digest_reports_an_update_when_the_pin_differs(self) -> None:
        assert CORE.compare_digest(_env_content(DIGEST_A), DIGEST_B) == "update"

    @pytest.mark.parametrize("prod_digest", ["", "sha256:zz", "prod"])
    def test_compare_digest_fails_closed_on_a_malformed_prod_digest(self, prod_digest: str) -> None:
        with pytest.raises(UpdateError):
            CORE.compare_digest(_env_content(DIGEST_A), prod_digest)


# --------------------------------------------------------------------------- #
# C1 provenance gate
# --------------------------------------------------------------------------- #


class TestProvenanceDecisionTable:
    """The C1 decision table: only an exact match passes."""

    def test_equal_digests_are_allowed(self) -> None:
        decision = CORE.decide_provenance(prod_digest=DIGEST_A, tag="v1.2.3", tag_digest=DIGEST_A)
        assert decision.allowed is True
        assert "v1.2.3" in decision.reason

    @pytest.mark.parametrize(
        ("prod_digest", "tag", "tag_digest"),
        [
            (DIGEST_A, "v1.2.3", DIGEST_B),
            (DIGEST_B, "v1.2.3", DIGEST_A),
            (DIGEST_A, "", DIGEST_A),
            (DIGEST_A, "1.2.3", DIGEST_A),
            (DIGEST_A, "v1.2", DIGEST_A),
            (DIGEST_A, "v1.2.3-rc1", DIGEST_A),
            (DIGEST_A, "release-1.2.3", DIGEST_A),
            ("", "v1.2.3", DIGEST_A),
            ("sha256:nothex", "v1.2.3", DIGEST_A),
            (DIGEST_A, "v1.2.3", ""),
            (DIGEST_A, "v1.2.3", "sha256:nothex"),
        ],
    )
    def test_every_uncertain_input_aborts(
        self, prod_digest: str, tag: str, tag_digest: str
    ) -> None:
        decision = CORE.decide_provenance(prod_digest=prod_digest, tag=tag, tag_digest=tag_digest)
        assert decision.allowed is False
        assert decision.reason

    def test_a_mismatch_names_both_digests(self) -> None:
        decision = CORE.decide_provenance(prod_digest=DIGEST_A, tag="v1.2.3", tag_digest=DIGEST_B)
        assert DIGEST_A in decision.reason
        assert DIGEST_B in decision.reason


class TestReleaseTagFromApi:
    """The releases/latest payload is parsed fail-closed (C1)."""

    def test_latest_release_tag_is_returned(self) -> None:
        payload = json.dumps({"tag_name": "v0.14.0", "name": "v0.14.0"})
        assert CORE.release_tag_from_api(payload) == "v0.14.0"

    @pytest.mark.parametrize(
        "payload",
        [
            "not json",
            "[]",
            "null",
            "{}",
            '{"tag_name": null}',
            '{"tag_name": "0.14.0"}',
            '{"tag_name": "v0.14.0-rc1"}',
            '{"tag_name": 14}',
        ],
    )
    def test_absent_or_non_release_tags_abort(self, payload: str) -> None:
        with pytest.raises(UpdateError):
            CORE.release_tag_from_api(payload)


# --------------------------------------------------------------------------- #
# C4 .env.prod rewrite
# --------------------------------------------------------------------------- #


class TestEnvRewrite:
    """The C4 rewrite contract: one line, mode 600, same-directory atomic rename."""

    def test_rewrite_replaces_only_the_image_line(self) -> None:
        content = _env_content(DIGEST_A, extra="INTERVALS_API_KEY=y\n")
        rewritten = CORE.rewrite_env_content(content, DIGEST_B)
        assert rewritten == _env_content(DIGEST_B, extra="INTERVALS_API_KEY=y\n")
        assert rewritten.count("COACH_WEB_IMAGE=") == 1

    def test_rewrite_preserves_a_missing_trailing_newline(self) -> None:
        assert CORE.rewrite_env_content(f"{IMAGE_LINE}{DIGEST_A}", DIGEST_B) == (
            f"{IMAGE_LINE}{DIGEST_B}"
        )

    @pytest.mark.parametrize("digest", ["", "sha256:zz", "sha256:" + "A" * 64])
    def test_rewrite_refuses_a_malformed_digest(self, digest: str) -> None:
        with pytest.raises(UpdateError):
            CORE.rewrite_env_content(_env_content(DIGEST_A), digest)

    def test_rewrite_refuses_zero_or_several_image_lines(self) -> None:
        with pytest.raises(UpdateError):
            CORE.rewrite_env_content("OPENROUTER_API_KEY=x\n", DIGEST_B)
        with pytest.raises(UpdateError):
            CORE.rewrite_env_content(_env_content(DIGEST_A) + _env_content(DIGEST_B), DIGEST_B)

    def _env_file(self, tmp_path: Path, digest: str = DIGEST_A) -> Path:
        env_file = tmp_path / ".env.prod"
        env_file.write_text(_env_content(digest), encoding="utf-8")
        env_file.chmod(0o600)
        return env_file

    def test_write_pins_the_new_digest_and_keeps_mode_600(self, tmp_path: Path) -> None:
        env_file = self._env_file(tmp_path)
        previous = CORE.write_env_digest(env_file, DIGEST_B)
        assert previous == DIGEST_A
        assert CORE.env_digest(env_file.read_text(encoding="utf-8")) == DIGEST_B
        assert env_file.stat().st_mode & 0o777 == 0o600

    def test_write_retains_the_previous_line_in_prev_mode_600(self, tmp_path: Path) -> None:
        env_file = self._env_file(tmp_path)
        CORE.write_env_digest(env_file, DIGEST_B)
        prev = CORE.previous_env_path(env_file)
        assert prev.name == ".env.prod.prev"
        assert prev.read_text(encoding="utf-8") == f"{IMAGE_LINE}{DIGEST_A}\n"
        assert prev.stat().st_mode & 0o777 == 0o600
        assert prev.stat().st_uid == os.geteuid()

    def test_write_replaces_via_a_same_directory_tempfile_after_retaining_prev(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C4: tempfile + rename in the same directory, previous digest written first."""
        env_file = self._env_file(tmp_path)
        renames: list[tuple[str, str, str, str]] = []
        real_replace = os.replace

        def spy(src: object, dst: object) -> None:
            renames.append(
                (
                    os.path.dirname(str(src)),
                    os.path.dirname(str(dst)),
                    os.path.basename(str(src)),
                    os.path.basename(str(dst)),
                )
            )
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", spy)
        CORE.write_env_digest(env_file, DIGEST_B)

        assert [(src_dir, dst_dir) for src_dir, dst_dir, _, _ in renames] == [
            (str(tmp_path), str(tmp_path)),
            (str(tmp_path), str(tmp_path)),
        ]
        assert [dst for _, _, _, dst in renames] == [".env.prod.prev", ".env.prod"]
        assert all(src != dst for _, _, src, dst in renames)
        assert not list(tmp_path.glob(".env.prod.*.tmp"))

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o700])
    def test_write_and_restore_refuse_a_lane_env_file_that_is_not_owner_only(
        self, tmp_path: Path, mode: int
    ) -> None:
        """C4: the mode/owner check runs before the swap *and* before the rollback."""
        env_file = self._env_file(tmp_path)
        env_file.chmod(mode)
        with pytest.raises(UpdateError):
            CORE.write_env_digest(env_file, DIGEST_B)
        with pytest.raises(UpdateError):
            CORE.restore_env_digest(env_file)
        assert CORE.env_digest(env_file.read_text(encoding="utf-8")) == DIGEST_A

    def test_write_refuses_a_malformed_new_digest_without_touching_the_file(
        self, tmp_path: Path
    ) -> None:
        env_file = self._env_file(tmp_path)
        with pytest.raises(UpdateError):
            CORE.write_env_digest(env_file, "sha256:nope")
        assert CORE.env_digest(env_file.read_text(encoding="utf-8")) == DIGEST_A

    def test_restore_re_applies_the_retained_digest_idempotently(self, tmp_path: Path) -> None:
        env_file = self._env_file(tmp_path)
        CORE.write_env_digest(env_file, DIGEST_B)
        assert CORE.restore_env_digest(env_file) == DIGEST_A
        assert CORE.restore_env_digest(env_file) == DIGEST_A
        assert CORE.env_digest(env_file.read_text(encoding="utf-8")) == DIGEST_A
        assert CORE.previous_env_path(env_file).read_text(encoding="utf-8") == (
            f"{IMAGE_LINE}{DIGEST_A}\n"
        )

    def test_restore_without_a_retained_digest_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(UpdateError):
            CORE.restore_env_digest(self._env_file(tmp_path))


# --------------------------------------------------------------------------- #
# C6 snapshot guards
# --------------------------------------------------------------------------- #

MEGABYTE = 1024 * 1024


class TestSnapshotGuards:
    """F3/F4: the sealed artifact must fit the tmpfs and must be usable."""

    def test_the_tmpfs_ceiling_is_the_64mb_budget_over_the_4_3_expansion(self) -> None:
        """~48 MB of database fits a 64 MB /tmp; a byte more does not."""
        capacity = CORE.SNAPSHOT_TMPFS_BYTES
        assert capacity == 64 * MEGABYTE
        fits = capacity * CORE.FERNET_EXPANSION_DEN // CORE.FERNET_EXPANSION_NUM
        ok, detail = CORE.snapshot_fits(fits)
        assert ok is True
        assert "snapshot fits" in detail
        too_big, detail = CORE.snapshot_fits(fits + 1)
        assert too_big is False
        assert "database too large for an in-container snapshot" in detail
        assert "off-host backup" in detail

    def test_the_estimate_rounds_up_the_encryption_expansion(self) -> None:
        assert CORE.snapshot_artifact_bytes(0) == 0
        assert CORE.snapshot_artifact_bytes(3) == 4
        assert CORE.snapshot_artifact_bytes(4096) == 5462

    @pytest.mark.parametrize("raw_bytes", [0, -1])
    def test_an_unknown_database_size_is_refused(self, raw_bytes: int) -> None:
        ok, detail = CORE.snapshot_fits(raw_bytes)
        assert ok is False
        assert "refusing to snapshot blindly" in detail

    def test_an_artifact_larger_than_the_database_passes_integrity(self) -> None:
        ok, detail = CORE.snapshot_integrity_ok(raw_bytes=4096, artifact_bytes=5462)
        assert ok is True
        assert "snapshot integrity ok" in detail

    @pytest.mark.parametrize("artifact_bytes", [0, 1, 4095, 4096])
    def test_an_empty_or_truncated_artifact_fails_integrity(self, artifact_bytes: int) -> None:
        """A `cp` that returned 0 is not evidence: the envelope must exceed the source."""
        ok, detail = CORE.snapshot_integrity_ok(raw_bytes=4096, artifact_bytes=artifact_bytes)
        assert ok is False
        assert "snapshot" in detail


# --------------------------------------------------------------------------- #
# AC3 health gate
# --------------------------------------------------------------------------- #


def _ps(
    *, health: str = "healthy", services: tuple[str, ...] = ("coach-web", "coach-mcp", "github-mcp")
) -> str:
    """A `docker compose ps -a --format json` payload for *services*."""
    return json.dumps(
        [{"Service": name, "Health": health, "State": "running"} for name in services]
    )


HEALTHZ_OK = json.dumps({"status": "healthy", "service": "coach-web", "version": "0.14.0"})


class TestHealthGate:
    """Step 7: 3/3 healthy **and** /healthz on the release version (AC3)."""

    def test_three_healthy_services_on_the_release_version_pass(self) -> None:
        passed, detail = CORE.health_gate(ps_json=_ps(), healthz_json=HEALTHZ_OK, tag="v0.14.0")
        assert passed is True
        assert "3/3" in detail
        assert "0.14.0" in detail

    def test_json_lines_form_is_accepted(self) -> None:
        payload = "\n".join(json.dumps(entry) for entry in json.loads(_ps()))
        passed, _ = CORE.health_gate(ps_json=payload, healthz_json=HEALTHZ_OK, tag="v0.14.0")
        assert passed is True

    def test_a_tag_without_the_v_prefix_is_compared_as_is(self) -> None:
        passed, _ = CORE.health_gate(ps_json=_ps(), healthz_json=HEALTHZ_OK, tag="0.14.0")
        assert passed is True

    @pytest.mark.parametrize(
        ("ps_json", "healthz_json", "tag"),
        [
            (_ps(services=("coach-web", "coach-mcp")), HEALTHZ_OK, "v0.14.0"),
            (
                _ps(services=("coach-web", "coach-mcp", "github-mcp", "extra")),
                HEALTHZ_OK,
                "v0.14.0",
            ),
            (_ps(health="starting"), HEALTHZ_OK, "v0.14.0"),
            (_ps(), json.dumps({"version": "0.13.0"}), "v0.14.0"),
            (_ps(), json.dumps({"version": "v0.14.0"}), "v0.14.0"),
            (_ps(), "not json", "v0.14.0"),
            (_ps(), HEALTHZ_OK, "v0.15.0"),
            ("", HEALTHZ_OK, "v0.14.0"),
            ("not json", HEALTHZ_OK, "v0.14.0"),
        ],
    )
    def test_any_missing_service_or_version_difference_fails(
        self, ps_json: str, healthz_json: str, tag: str
    ) -> None:
        passed, detail = CORE.health_gate(ps_json=ps_json, healthz_json=healthz_json, tag=tag)
        assert passed is False
        assert detail


# --------------------------------------------------------------------------- #
# #345: per-tick lane health signal
# --------------------------------------------------------------------------- #


class TestLaneHealthSignal:
    """#345: the no-change tick measures the exact 3/3 set instead of assuming it."""

    def test_a_complete_healthy_set_passes_with_the_3_3_verdict(self) -> None:
        ok, detail = CORE.services_healthy(_ps())
        assert ok is True
        assert "3/3" in detail

    @pytest.mark.parametrize(
        "payload",
        [
            _ps(services=("coach-web", "coach-mcp")),  # a sidecar disappeared at runtime
            _ps(services=("coach-web", "coach-mcp", "extra")),
            _ps(health="starting"),
            _ps(health="unhealthy"),
            "",
        ],
    )
    def test_an_incomplete_or_unhealthy_set_is_detected(self, payload: str) -> None:
        ok, detail = CORE.services_healthy(payload)
        assert ok is False
        assert detail

    def test_lane_health_verb_prints_pass_and_fail(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ps_json = tmp_path / "ps.json"
        ps_json.write_text(_ps(), encoding="utf-8")
        assert CORE.main(["lane-health", "--ps-json", str(ps_json)]) == 0
        assert capsys.readouterr().out.startswith("PASS: ")
        ps_json.write_text(_ps(services=("coach-web",)), encoding="utf-8")
        assert CORE.main(["lane-health", "--ps-json", str(ps_json)]) == 1
        assert capsys.readouterr().out.startswith("FAIL: ")


# --------------------------------------------------------------------------- #
# C8 state and failure streak
# --------------------------------------------------------------------------- #


class TestFailureStreak:
    """The C8 streak: failures accumulate, a success resets, the limit pauses."""

    def test_first_failure_starts_the_streak(self) -> None:
        state = CORE.next_state(None, outcome="failure", now=LAST_RUN)
        assert state.consecutive_failures == 1
        assert state.outcome == "failure"

    def test_consecutive_failures_accumulate_to_the_limit(self) -> None:
        state = None
        for _ in range(CORE.FAILURE_LIMIT):
            assert CORE.should_halt(state) is False
            state = CORE.next_state(state, outcome="failure", now=LAST_RUN)
        assert state is not None
        assert state.consecutive_failures == CORE.FAILURE_LIMIT
        assert CORE.should_halt(state) is True

    def test_a_success_resets_the_streak(self) -> None:
        state = CORE.next_state(None, outcome="failure", now=LAST_RUN)
        state = CORE.next_state(state, outcome="success", now="2026-10-25T00:01:00Z")
        assert state.consecutive_failures == 0
        assert state.outcome == "success"

    def test_no_history_never_halts(self) -> None:
        assert CORE.should_halt(None) is False

    def test_an_unknown_outcome_is_refused(self) -> None:
        with pytest.raises(UpdateError):
            CORE.next_state(None, outcome="maybe", now=LAST_RUN)

    def test_a_failure_reason_round_trips_and_a_success_clears_it(self, tmp_path: Path) -> None:
        """#344: the state file names the failed condition (e.g. checkout-guard)."""
        state_file = tmp_path / "update-state.json"
        failed = CORE.next_state(None, outcome="failure", now=LAST_RUN, reason="checkout-guard")
        assert failed.reason == "checkout-guard"
        CORE.write_state(state_file, failed)
        assert CORE.read_state(state_file) == failed
        assert "checkout-guard" in state_file.read_text(encoding="utf-8")
        succeeded = CORE.next_state(
            failed, outcome="success", now=LAST_RUN, reason="checkout-guard"
        )
        assert succeeded.reason is None

    def test_a_non_string_reason_is_no_history(self, tmp_path: Path) -> None:
        state_file = tmp_path / "update-state.json"
        state_file.write_text(
            json.dumps(
                {
                    "last_run": LAST_RUN,
                    "outcome": "failure",
                    "consecutive_failures": 1,
                    "reason": 7,
                }
            ),
            encoding="utf-8",
        )
        assert CORE.read_state(state_file) is None

    def test_state_round_trips_owner_only(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state" / "update-state.json"
        state = CORE.next_state(None, outcome="failure", now=LAST_RUN)
        CORE.write_state(state_file, state)
        assert CORE.read_state(state_file) == state
        assert state_file.stat().st_mode & 0o777 == 0o600
        assert state_file.parent.stat().st_mode & 0o777 == 0o700
        assert "failure" in state_file.read_text(encoding="utf-8")

    def test_a_missing_state_file_is_no_history(self, tmp_path: Path) -> None:
        assert CORE.read_state(tmp_path / "absent.json") is None

    @pytest.mark.parametrize(
        "payload",
        [
            "not json",
            "[]",
            json.dumps({"last_run": LAST_RUN}),
            json.dumps({"last_run": LAST_RUN, "outcome": "maybe", "consecutive_failures": 1}),
            json.dumps({"last_run": 1, "outcome": "failure", "consecutive_failures": 1}),
            json.dumps({"last_run": LAST_RUN, "outcome": "failure", "consecutive_failures": -1}),
            json.dumps({"last_run": LAST_RUN, "outcome": "failure", "consecutive_failures": True}),
        ],
    )
    def test_a_corrupt_state_file_is_no_history(self, tmp_path: Path, payload: str) -> None:
        state_file = tmp_path / "update-state.json"
        state_file.write_text(payload, encoding="utf-8")
        assert CORE.read_state(state_file) is None


class TestLaneState:
    """#345: the lane signal is additive — old state files keep reading (C8)."""

    def test_a_success_records_the_lane_and_round_trips(self, tmp_path: Path) -> None:
        state_file = tmp_path / "update-state.json"
        state = CORE.next_state(None, outcome="success", now=LAST_RUN, lane="healthy")
        assert state.lane == "healthy"
        CORE.write_state(state_file, state)
        assert CORE.read_state(state_file) == state
        assert json.loads(state_file.read_text(encoding="utf-8"))["lane"] == "healthy"

    def test_a_degraded_outcome_neither_resets_nor_increments_the_streak(self) -> None:
        state = CORE.next_state(None, outcome="failure", now=LAST_RUN)
        state = CORE.next_state(state, outcome="failure", now=LAST_RUN)
        assert state.consecutive_failures == 2
        degraded = CORE.next_state(state, outcome="degraded", now=LAST_RUN, lane="degraded")
        assert degraded.outcome == "degraded"
        assert degraded.lane == "degraded"
        assert degraded.consecutive_failures == 2, "a degraded lane is not an update failure"

    def test_an_old_state_file_without_a_lane_field_still_reads(self, tmp_path: Path) -> None:
        """Backward compatibility: the pre-#345 file has no `lane` key."""
        state_file = tmp_path / "update-state.json"
        state_file.write_text(
            json.dumps({"last_run": LAST_RUN, "outcome": "success", "consecutive_failures": 0}),
            encoding="utf-8",
        )
        state = CORE.read_state(state_file)
        assert state is not None
        assert state.outcome == "success"
        assert state.lane is None

    def test_a_non_string_lane_is_no_history(self, tmp_path: Path) -> None:
        state_file = tmp_path / "update-state.json"
        state_file.write_text(
            json.dumps(
                {
                    "last_run": LAST_RUN,
                    "outcome": "success",
                    "consecutive_failures": 0,
                    "lane": 7,
                }
            ),
            encoding="utf-8",
        )
        assert CORE.read_state(state_file) is None


# --------------------------------------------------------------------------- #
# CLI contract the shell entrypoint depends on
# --------------------------------------------------------------------------- #


class TestCliContract:
    """One verb per decision: the exit code and the printed word are the contract."""

    def _env_file(self, tmp_path: Path) -> Path:
        env_file = tmp_path / ".env.prod"
        env_file.write_text(_env_content(DIGEST_A), encoding="utf-8")
        env_file.chmod(0o600)
        return env_file

    def test_release_tag_prints_the_tag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = tmp_path / "release.json"
        payload.write_text('{"tag_name": "v0.14.0"}', encoding="utf-8")
        assert CORE.main(["release-tag", "--release-json", str(payload)]) == 0
        assert capsys.readouterr().out.strip() == "v0.14.0"

    def test_release_tag_fails_closed_on_a_missing_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert CORE.main(["release-tag", "--release-json", str(tmp_path / "absent.json")]) == 2
        assert capsys.readouterr().err

    def test_compare_prints_no_change(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file = self._env_file(tmp_path)
        assert CORE.main(["compare", "--env-file", str(env_file), "--prod-digest", DIGEST_A]) == 0
        assert capsys.readouterr().out.strip() == "no-change"

    def test_compare_prints_update(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file = self._env_file(tmp_path)
        assert CORE.main(["compare", "--env-file", str(env_file), "--prod-digest", DIGEST_B]) == 0
        assert capsys.readouterr().out.strip() == "update"

    def test_provenance_exit_code_signals_the_verdict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["provenance", "--prod-digest", DIGEST_A, "--tag", "v0.14.0"]
        assert CORE.main([*argv, "--tag-digest", DIGEST_A]) == 0
        assert capsys.readouterr().out.startswith("match: ")
        assert CORE.main([*argv, "--tag-digest", DIGEST_B]) == 1
        assert capsys.readouterr().out.startswith("abort: ")

    def test_rewrite_pins_then_restores(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file = self._env_file(tmp_path)
        assert CORE.main(["rewrite", "--env-file", str(env_file), "--digest", DIGEST_B]) == 0
        assert DIGEST_B in capsys.readouterr().out
        assert CORE.main(["rewrite", "--env-file", str(env_file), "--from-prev"]) == 0
        assert DIGEST_A in capsys.readouterr().out
        assert CORE.env_digest(env_file.read_text(encoding="utf-8")) == DIGEST_A

    def test_state_check_reports_ok_then_halted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state_file = tmp_path / "update-state.json"
        argv = ["state-check", "--state-file", str(state_file), "--limit", "2"]
        assert CORE.main(argv) == 0
        assert capsys.readouterr().out.startswith("ok")
        for _ in range(2):
            CORE.main(["state-record", "--state-file", str(state_file), "--outcome", "failure"])
        capsys.readouterr()
        assert CORE.main(argv) == 1
        assert capsys.readouterr().out.startswith("halted")

    def test_state_record_reports_the_streak(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state_file = tmp_path / "update-state.json"
        for attempt in (1, 2):
            assert (
                CORE.main(["state-record", "--state-file", str(state_file), "--outcome", "failure"])
                == 0
            )
            assert f"consecutive_failures={attempt}" in capsys.readouterr().out
        assert (
            CORE.main(["state-record", "--state-file", str(state_file), "--outcome", "success"])
            == 0
        )
        assert "consecutive_failures=0" in capsys.readouterr().out

    def test_state_record_tags_a_failure_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """#344: `state-record --reason TAG` records the failed condition."""
        state_file = tmp_path / "update-state.json"
        argv = [
            "state-record",
            "--state-file",
            str(state_file),
            "--outcome",
            "failure",
            "--reason",
            "checkout-guard",
        ]
        assert CORE.main(argv) == 0
        assert "reason=checkout-guard" in capsys.readouterr().out
        assert json.loads(state_file.read_text(encoding="utf-8"))["reason"] == "checkout-guard"

    def test_state_record_records_the_lane_signal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """#345: `state-record --lane` persists and prints the lane health."""
        state_file = tmp_path / "update-state.json"
        argv = [
            "state-record",
            "--state-file",
            str(state_file),
            "--outcome",
            "success",
            "--lane",
            "recovered",
        ]
        assert CORE.main(argv) == 0
        assert "lane=recovered" in capsys.readouterr().out
        assert json.loads(state_file.read_text(encoding="utf-8"))["lane"] == "recovered"

    def test_snapshot_check_covers_both_phases(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert CORE.main(["snapshot-check", "--bytes", "4096"]) == 0
        assert capsys.readouterr().out.startswith("snapshot fits")
        assert CORE.main(["snapshot-check", "--bytes", str(50 * MEGABYTE)]) == 1
        assert "too large" in capsys.readouterr().out
        argv = ["snapshot-check", "--bytes", "4096", "--artifact-bytes", "10"]
        assert CORE.main(argv) == 1
        assert "not larger than the source database" in capsys.readouterr().out
        argv = ["snapshot-check", "--bytes", "4096", "--artifact-bytes", "5462"]
        assert CORE.main(argv) == 0
        assert capsys.readouterr().out.startswith("snapshot integrity ok")

    def test_health_gate_prints_pass_and_fail(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ps_json = tmp_path / "ps.json"
        ps_json.write_text(_ps(), encoding="utf-8")
        healthz = tmp_path / "healthz.json"
        healthz.write_text(HEALTHZ_OK, encoding="utf-8")
        argv = [
            "health-gate",
            "--tag",
            "v0.14.0",
            "--ps-json",
            str(ps_json),
            "--healthz-json",
            str(healthz),
        ]
        assert CORE.main(argv) == 0
        assert capsys.readouterr().out.startswith("PASS: ")
        healthz.write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
        assert CORE.main(argv) == 1
        assert capsys.readouterr().out.startswith("FAIL: ")


# --------------------------------------------------------------------------- #
# Script guards (C3, C4, AC8)
# --------------------------------------------------------------------------- #


class TestScriptGuards:
    """The committed entrypoint carries the C3/C4/AC8 contract."""

    def _directives(self) -> str:
        """The entrypoint with its comment lines removed."""
        text = ENTRYPOINT.read_text(encoding="utf-8")
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

    def test_entrypoint_exists_and_is_executable(self) -> None:
        assert ENTRYPOINT.is_file()
        assert ENTRYPOINT.stat().st_mode & 0o111

    def test_checkout_guard_exists_and_is_executable(self) -> None:
        """#344: the shared guard is a committed, executable script."""
        assert CHECK_GUARD.is_file()
        assert CHECK_GUARD.stat().st_mode & 0o111

    def test_checkout_guard_passes_bash_syntax_check(self) -> None:
        result = subprocess.run(
            ["/bin/bash", "-n", str(CHECK_GUARD)], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_entrypoint_invokes_the_checkout_guard(self) -> None:
        """#344: the updater runs the guard and records the failed condition."""
        directives = self._directives()
        assert "repo-clean-check.sh" in directives
        assert "record failure checkout-guard" in directives

    def test_boot_unit_applies_the_same_checkout_guard(self) -> None:
        """#344: a dirty tree cannot change boot behaviour either."""
        unit = LANE_UNIT_TEMPLATE.read_text(encoding="utf-8")
        assert "ExecStartPre=/bin/bash @REPO_ROOT@/scripts/repo-clean-check.sh" in unit

    def test_entrypoint_passes_a_bash_syntax_check(self) -> None:
        result = subprocess.run(
            ["/bin/bash", "-n", str(ENTRYPOINT)], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_core_passes_a_python_syntax_check(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(CORE_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_entrypoint_never_traces_or_dumps_the_environment(self) -> None:
        """AC8: no `set -x`, no `printenv`, and the lane env file is only an --env-file."""
        directives = self._directives()
        assert "set -x" not in directives
        assert "printenv" not in directives
        assert "--env-file" in directives

    def test_compose_helper_only_invokes_non_dumping_verbs(self) -> None:
        """C4/AC8 (F7): assert what the `dc` helper actually runs, not a literal string.

        A `docker compose config` that never prints its output would still hand
        the resolved `.env.prod` to anything watching the process tree, so the
        invariant is the helper's verb set: `ps`, `exec`, `up`, `down` —
        never `config` (or any future verb that dumps the interpolation).
        """
        verbs = set(re.findall(r"\bdc ([a-z][a-z-]*)", self._directives()))
        assert verbs, "the compose helper is expected to be invoked directly"
        assert verbs <= ALLOWED_COMPOSE_VERBS, sorted(verbs - ALLOWED_COMPOSE_VERBS)
        assert "config" not in verbs

    def test_entrypoint_streams_the_artifact_and_never_uses_compose_cp(self) -> None:
        """C6/#342: the artifact leaves the container via `exec -T cat`, never cp.

        The container's /tmp is a tmpfs mount: docker cp / compose cp resolve
        paths on the container rootfs and do not traverse it, so every tick
        failed at extraction ("Could not find the file"). The mechanism is
        pinned statically: no `dc cp` anywhere, and the extraction is the
        exec-stream redirect into a host temp file on the snapshots filesystem.
        """
        directives = self._directives()
        assert not re.search(r"\bdc cp\b", directives)
        assert "dc exec -T coach-web cat" in directives

    def test_entrypoint_never_names_a_secret(self) -> None:
        """C4/AC8: the script carries no secret name except the defensive unset."""
        directives = self._directives()
        for marker in (
            "AUTH_SESSION_SECRET",
            "OPENROUTER_API_KEY",
            "DATA_ENCRYPTION_KEY",
            "GOOGLE_OIDC_CLIENT_SECRET",
            "INTERVALS_API_KEY",
        ):
            assert marker not in directives, marker
        for line in directives.splitlines():
            if "GITHUB_TOKEN" in line:
                assert "unset" in line, line

    def test_entrypoint_preflight_and_lane_unit_share_the_prod_lane_lock(self) -> None:
        """C3 (F1): one lock file guards the tick, the preflight and the lane unit."""
        lock_expression = "XDG_RUNTIME_DIR:-/run/user/$(id -u)"
        for script in (ENTRYPOINT, PREFLIGHT):
            text = script.read_text(encoding="utf-8")
            assert PROD_LANE_LOCK in text, script
            assert lock_expression in text, script
        # The lane unit is a second writer of the same project: its start and its
        # stop must take the same lock, addressed as /run/user/%U (the unit's uid
        # specifier), and wait rather than race the tick.
        unit = LANE_UNIT_TEMPLATE.read_text(encoding="utf-8")
        flock_command = f"/usr/bin/flock -w 900 /run/user/%U/{PROD_LANE_LOCK}"
        assert unit.count(flock_command) == 2, "ExecStart and ExecStop both take the lock"
        assert "ExecStart=/usr/bin/flock" in unit
        assert "ExecStop=/usr/bin/flock" in unit
        assert "flock -n 9" in ENTRYPOINT.read_text(encoding="utf-8")

    def test_preflight_takes_the_lock_for_the_prod_lane_only(self) -> None:
        """The dev/qa lanes keep their behaviour: the lock block is prod-scoped."""
        text = PREFLIGHT.read_text(encoding="utf-8")
        assert 'if [[ "${LANE}" == "prod" ]]; then' in text
        assert "flock -n 9" in text


# --------------------------------------------------------------------------- #
# Shell entrypoint behaviour in a sandbox (C2, C3, C6, AC2/AC3/AC4)
# --------------------------------------------------------------------------- #

DOCKER_SHIM = '''#!/usr/bin/env python3
"""Recording docker shim: logs argv and answers every call prod-update.sh makes.

The shim is the whole (fake) docker surface: registry digest resolution, the
digest pull, the compose verb set (ps/exec/up), the in-container database
size probe, the maintenance backup, and the extraction stream (`exec -T cat`)
that carries the staged artifact out of the container. Every call is
appended to PROD_UPDATE_SHIM_LOG so the tests can assert the ORDER as well as
the arguments; env vars steer each answer.
"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["PROD_UPDATE_SHIM_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(["docker", *args]) + "\\n")

DIGEST = os.environ.get("PROD_UPDATE_DIGEST", "")
TAG_DIGEST = os.environ.get("PROD_UPDATE_TAG_DIGEST", DIGEST)
DB_BYTES = int(os.environ.get("PROD_UPDATE_DB_BYTES", "4096"))
ARTIFACT_BYTES = int(os.environ.get("PROD_UPDATE_ARTIFACT_BYTES", str(DB_BYTES * 4 // 3 + 1)))
HEAL_MARKER = os.environ.get("PROD_UPDATE_HEAL_MARKER", "")


def enabled(name: str) -> bool:
    """True unless the test switched this behaviour off."""
    return os.environ.get(name, "1") == "1"


def healed() -> bool:
    """True once a self-heal `up` recreated the lane (the marker file exists).

    Models the #345 incident: before the heal the lane is degraded (a service is
    missing or unhealthy, steered by PROD_UPDATE_PS_SERVICES / PROD_UPDATE_HEALTH);
    after the idempotent `up` recreated it, the same `ps` reports 3/3 healthy.
    """
    return bool(HEAL_MARKER) and os.path.exists(HEAL_MARKER)


if args[:3] == ["buildx", "imagetools", "inspect"]:
    digest = DIGEST if args[3].endswith(":prod") else TAG_DIGEST
    if not digest:
        sys.exit(1)
    print("Name:   " + args[3])
    print("Digest: " + digest)
    sys.exit(0)

if args[:1] == ["pull"]:
    print("Digest: " + DIGEST)
    sys.exit(0 if enabled("PROD_UPDATE_PULL_OK") else 1)

if args[:1] == ["compose"]:
    index = 1
    while index < len(args) and args[index].startswith("-"):
        index += 2  # -p / -f / --env-file all take exactly one value
    verb = args[index] if index < len(args) else ""
    rest = args[index + 1 :]
    if verb == "ps":
        if healed():
            names = ("coach-web", "coach-mcp", "github-mcp")
            health = "healthy"
        else:
            names = tuple(
                os.environ.get(
                    "PROD_UPDATE_PS_SERVICES", "coach-web,coach-mcp,github-mcp"
                ).split(",")
            )
            health = os.environ.get("PROD_UPDATE_HEALTH", "healthy")
        print(
            json.dumps(
                [{"Service": name, "Health": health, "State": "running"} for name in names]
            )
        )
        sys.exit(0)
    if verb == "exec":
        if "-c" in rest:
            print(DB_BYTES)
            sys.exit(0)
        if any("coach_web.maintenance" in token for token in rest):
            sys.exit(0 if enabled("PROD_UPDATE_BACKUP_OK") else 1)
        if any(token == "cat" for token in rest):
            # The extraction stream (#342): `exec -T cat` writes the staged
            # artifact to stdout; the entrypoint redirects it into a host temp
            # file. PROD_UPDATE_STREAM_OK=0 fails the stream (mid-stream death),
            # ARTIFACT_BYTES=0 empties it.
            if not enabled("PROD_UPDATE_STREAM_OK"):
                sys.exit(1)
            sys.stdout.buffer.write(b"x" * ARTIFACT_BYTES)
            sys.stdout.buffer.flush()
            sys.exit(0)
        sys.exit(0)  # the rm of the staged artifact
    if verb == "cp":
        # #342: compose cp resolves paths on the container rootfs and cannot
        # traverse the /tmp tmpfs the C6 artifact is staged on — reproduce the
        # real daemon failure so a regression back to cp fails like production.
        print("Error response from daemon: Could not find the file", file=sys.stderr)
        sys.exit(1)
    if verb == "up":
        if enabled("PROD_UPDATE_UP_OK"):
            if HEAL_MARKER:
                open(HEAL_MARKER, "a", encoding="utf-8").close()
            sys.exit(0)
        sys.exit(1)
    if verb == "down":
        sys.exit(0)
    sys.exit(1)

sys.exit(1)
'''

CURL_SHIM = '''#!/usr/bin/env python3
"""Recording curl shim: answers the Releases API and /healthz the script fetches."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["PROD_UPDATE_SHIM_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(["curl", *args]) + "\\n")

url = args[-1]
if "api.github.com" in url:
    if os.environ.get("PROD_UPDATE_API_OK", "1") != "1":
        sys.exit(22)  # what curl -f returns on an HTTP error
    print(json.dumps({"tag_name": os.environ.get("PROD_UPDATE_RELEASE_TAG", "v0.14.0")}))
    sys.exit(0)
if url.endswith("/healthz"):
    print(
        json.dumps(
            {
                "status": "healthy",
                "service": "coach-web",
                "version": os.environ.get("PROD_UPDATE_HEALTHZ_VERSION", "0.14.0"),
            }
        )
    )
    sys.exit(0)
sys.exit(1)
'''


class ProdUpdateSandbox:
    """Temp HOME + temp repo copy with recording docker/curl shims."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin_dir = tmp_path / "bin"
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.run_dir = tmp_path / "run"
        self.log = tmp_path / "shim.log"
        self._lock_handles: list[IO[str]] = []
        (self.repo / "scripts").mkdir(parents=True)
        self.bin_dir.mkdir()
        self.home.mkdir()
        self.run_dir.mkdir()
        for name in ("prod-update.sh", "prod_update.py", "repo-clean-check.sh"):
            shutil.copy2(SCRIPTS_DIR / name, self.repo / "scripts" / name)
        for name in ("prod-update.sh", "repo-clean-check.sh"):
            (self.repo / "scripts" / name).chmod(0o755)
        for name in ("compose.yaml", "compose.prod.yml"):
            (self.repo / name).touch()
        self.log.touch()
        for name, source in (("docker", DOCKER_SHIM), ("curl", CURL_SHIM)):
            shim = self.bin_dir / name
            shim.write_text(source, encoding="utf-8")
            shim.chmod(0o755)
        self._init_git()

    def _git(self, *args: str) -> None:
        """Run git in the sandbox repo with a hermetic, host-config-free env."""
        env = dict(os.environ)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "sandbox"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "sandbox@example.invalid"
        git = shutil.which("git")
        assert git is not None, "git must be available for the #344 checkout-guard tests"
        subprocess.run(
            [git, *args], cwd=self.repo, env=env, check=True, capture_output=True, text=True
        )

    def _init_git(self) -> None:
        """Make the sandbox repo a clean checkout of `main` at origin/main (#344).

        Every sandbox test now crosses the checkout guard, so the fixture models
        the legitimate case: branch `main`, no tracked changes, HEAD == the
        last-known `refs/remotes/origin/main`.
        """
        self._git("init", "-q")
        self._git("symbolic-ref", "HEAD", "refs/heads/main")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "sandbox", "--no-gpg-sign")
        self._git("update-ref", "refs/remotes/origin/main", "HEAD")

    def dirty_tracked_file(self) -> None:
        """Introduce an unstaged modification to a tracked file (#344 AC3)."""
        (self.repo / "compose.yaml").write_text("dirty\n", encoding="utf-8")

    def switch_branch(self, name: str) -> None:
        """Check out a new branch off the sandbox trunk (#344 AC3)."""
        self._git("checkout", "-q", "-b", name)

    def advance_head(self) -> None:
        """Commit on main so HEAD no longer equals origin/main (#344 AC3)."""
        (self.repo / "compose.yaml").write_text("ahead\n", encoding="utf-8")
        self._git("add", "compose.yaml")
        self._git("commit", "-q", "-m", "ahead", "--no-gpg-sign")

    @property
    def script(self) -> Path:
        return self.repo / "scripts" / "prod-update.sh"

    @property
    def lock_path(self) -> Path:
        return self.run_dir / "coach-web-prod.lock"

    @property
    def env_file(self) -> Path:
        return self.repo / ".env.prod"

    @property
    def prev_file(self) -> Path:
        return self.repo / ".env.prod.prev"

    @property
    def snapshot_dir(self) -> Path:
        return self.home / ".local" / "state" / "coach-web" / "snapshots"

    @property
    def heal_marker(self) -> Path:
        """Marker the docker shim writes when a self-heal `up` recreates the lane."""
        return self.tmp / "healed"

    def lane(self) -> str | None:
        """The `lane` field recorded in the C8 state file (#345)."""
        state: dict[str, str | None] = json.loads(self.state_file.read_text(encoding="utf-8"))
        return state.get("lane")

    @property
    def state_file(self) -> Path:
        return self.home / ".local" / "state" / "coach-web" / "update-state.json"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["HOME"] = str(self.home)
        env["XDG_RUNTIME_DIR"] = str(self.run_dir)
        # Drop any ambient PROD_UPDATE_* steering from the developer's shell, then
        # set the sandbox's own. Keep the gate loop short: the shim answers
        # instantly, and a failing gate must not sit in the 120 s production
        # window for the whole suite.
        for key in list(env):
            if key.startswith("PROD_UPDATE_"):
                del env[key]
        env["PROD_UPDATE_SHIM_LOG"] = str(self.log)
        env["COACH_WEB_GATE_TIMEOUT_SECONDS"] = "4"
        if extra:
            env.update(extra)
        return env

    def run(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *args],
            cwd=self.repo,
            env=self.env(extra_env),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def run_guard(self) -> subprocess.CompletedProcess[str]:
        """Run the #344 checkout guard directly against the sandbox repo."""
        return subprocess.run(
            ["/bin/bash", str(self.repo / "scripts" / "repo-clean-check.sh"), str(self.repo)],
            cwd=self.repo,
            env=self.env(),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def start(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        """Start the entrypoint without waiting (lock-contention tests)."""
        return subprocess.Popen(
            [str(self.script), *args],
            cwd=self.repo,
            env=self.env(extra_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def write_env_file(self, digest: str) -> Path:
        self.env_file.write_text(f"{IMAGE_LINE}{digest}\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        return self.env_file

    def invocations(self) -> list[list[str]]:
        """Every recorded shim call, in order, each prefixed by its tool name."""
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def docker_invocations(self) -> list[list[str]]:
        return [call for call in self.invocations() if call[0] == "docker"]

    def index_of(self, call: list[str]) -> int:
        """Position of the first recorded call whose argv equals *call*."""
        return self.invocations().index(call)

    def outcome(self) -> str:
        state: dict[str, str] = json.loads(self.state_file.read_text(encoding="utf-8"))
        return state["outcome"]

    def state_reason(self) -> str | None:
        state: dict[str, str | None] = json.loads(self.state_file.read_text(encoding="utf-8"))
        return state.get("reason")

    def snapshots(self) -> list[Path]:
        return sorted(self.snapshot_dir.glob("coach-*.backup"))

    def hold_lock(self) -> None:
        """Take the shared prod-lane lock in-process (flock(2) — same as the script)."""
        handle = open(self.lock_path, "w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._lock_handles.append(handle)

    def close(self) -> None:
        """Release the locks taken by :meth:`hold_lock`."""
        for handle in self._lock_handles:
            handle.close()
        self._lock_handles.clear()


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[ProdUpdateSandbox]:
    """A temp-HOME sandbox with a recording docker shim."""
    box = ProdUpdateSandbox(tmp_path)
    yield box
    box.close()


class TestCheckoutGuard:
    """#344: the updater must never execute unreviewed code from a dirty tree.

    The v0.14.0 incident ran a review-pending branch checked out in the shared
    working tree. Each violation must abort before any registry or docker call,
    name the condition and the fix, record ``reason=checkout-guard`` and exit
    non-zero — ``--force`` and ``--rollback`` included.
    """

    def test_a_clean_checkout_on_main_passes(self, sandbox: ProdUpdateSandbox) -> None:
        result = sandbox.run_guard()
        assert result.returncode == 0, result.stderr
        assert "ok: clean checkout of main" in result.stdout

    def test_untracked_files_are_allowed(self, sandbox: ProdUpdateSandbox) -> None:
        """Untracked files cannot change a tracked script, so they do not abort."""
        sandbox.write_env_file(DIGEST_A)  # an untracked .env.prod
        assert sandbox.run_guard().returncode == 0

    def test_the_message_names_the_three_conditions_and_the_fix(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        sandbox.switch_branch("fix/some-review-pending-branch")
        result = sandbox.run_guard()
        assert result.returncode != 0
        assert "clean checkout of 'main' at origin/main" in result.stderr
        assert "checkout main" in result.stderr and "pull --ff-only" in result.stderr

    def test_a_dirty_tracked_file_aborts_before_any_mutation(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        sandbox.write_env_file(DIGEST_A)
        sandbox.dirty_tracked_file()
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode != 0
        assert "tracked changes" in result.stderr
        assert sandbox.invocations() == [], "no registry or docker call may run"
        assert sandbox.state_reason() == "checkout-guard"

    def test_a_feature_branch_aborts_before_any_mutation(self, sandbox: ProdUpdateSandbox) -> None:
        sandbox.write_env_file(DIGEST_A)
        sandbox.switch_branch("fix/342-snapshot-extraction")
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode != 0
        assert "not 'main'" in result.stderr
        assert sandbox.invocations() == []
        assert sandbox.state_reason() == "checkout-guard"

    def test_a_stale_head_aborts_before_any_mutation(self, sandbox: ProdUpdateSandbox) -> None:
        sandbox.write_env_file(DIGEST_A)
        sandbox.advance_head()  # clean tree, on main, but ahead of origin/main
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode != 0
        assert "origin/main" in result.stderr
        assert sandbox.invocations() == []
        assert sandbox.state_reason() == "checkout-guard"

    def test_force_never_bypasses_the_guard(self, sandbox: ProdUpdateSandbox) -> None:
        """--force skips the no-op/C8 pause, never the checkout guard."""
        sandbox.switch_branch("fix/342-snapshot-extraction")
        result = sandbox.run("--force", extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode != 0
        assert "not 'main'" in result.stderr
        assert sandbox.invocations() == []

    def test_rollback_requires_a_clean_tree(self, sandbox: ProdUpdateSandbox) -> None:
        sandbox.dirty_tracked_file()
        result = sandbox.run("--rollback")
        assert result.returncode != 0
        assert "tracked changes" in result.stderr
        assert sandbox.invocations() == []

    def test_a_dirty_tree_refuses_before_waiting_on_the_lock(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """#344/review: the guard precedes the C3 lock.

        With the lock held, `--rollback` on a dirty tree must refuse immediately
        (it would otherwise wait up to LOCK_WAIT_SECONDS=900 s first). The guard
        error is emitted and no lock acquisition is logged.
        """
        sandbox.dirty_tracked_file()
        sandbox.hold_lock()
        result = sandbox.run("--rollback")
        assert result.returncode != 0
        assert "tracked changes" in result.stderr
        assert "acquired the shared prod-lane lock" not in result.stdout
        assert sandbox.invocations() == []


class TestEntrypointBehaviour:
    """C2/C3/C6 behaviour of scripts/prod-update.sh (shimmed, daemon-free).

    The docker and curl shims record every call, so the tests pin the arguments
    (which digest, which artifact) *and* the order of the operations (pull before
    the env swap, snapshot before the swap, rollback after a failed gate).
    """

    PROD_REF = "ghcr.io/fpittelo/coach-web:prod"

    @staticmethod
    def _first(sandbox: ProdUpdateSandbox, tool: str, needle: str) -> int:
        """Position of the first *tool* call carrying *needle* as an exact argv token."""
        for position, call in enumerate(sandbox.invocations()):
            if call[0] == tool and needle in call:
                return position
        raise AssertionError(f"no {tool} call with argument {needle!r}: {sandbox.invocations()}")

    @staticmethod
    def _last(sandbox: ProdUpdateSandbox, tool: str, needle: str) -> int:
        """Position of the last *tool* call carrying *needle* as an exact argv token."""
        positions = [
            position
            for position, call in enumerate(sandbox.invocations())
            if call[0] == tool and needle in call
        ]
        assert positions, f"no {tool} call with argument {needle!r}: {sandbox.invocations()}"
        return positions[-1]

    def test_a_held_lock_makes_the_tick_a_no_op(self, sandbox: ProdUpdateSandbox) -> None:
        """AC6/C3: a tick during another prod-lane operation exits 0, untouched."""
        sandbox.write_env_file(DIGEST_A)
        sandbox.hold_lock()
        result = sandbox.run()
        assert result.returncode == 0, result.stderr
        assert "skipping this tick" in result.stdout
        assert sandbox.invocations() == []
        assert not sandbox.state_file.exists()

    def test_a_held_lock_is_waited_for_by_the_operator_paths(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """C3: --rollback waits for the in-flight operation instead of skipping."""
        sandbox.write_env_file(DIGEST_A)
        sandbox.hold_lock()
        process = sandbox.start("--rollback")
        time.sleep(1.0)
        sandbox.close()  # the in-flight operation finishes; the wait must end
        stdout, stderr = process.communicate(timeout=60)
        assert "acquired the shared prod-lane lock" in stdout, (stdout, stderr)
        # It then ran the rollback and refused it: no previous digest is retained.
        assert process.returncode != 0
        assert "rollback failed" in stderr

    def test_an_unreachable_registry_aborts_without_pull_or_recreate(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """C2/AC5: no digest resolvable => non-zero, no pull, no recreate."""
        sandbox.write_env_file(DIGEST_A)
        result = sandbox.run()
        assert result.returncode != 0
        assert "registry unreachable" in result.stderr
        assert sandbox.docker_invocations() == [
            ["docker", "buildx", "imagetools", "inspect", self.PROD_REF]
        ]

    def test_a_missing_lane_env_file_aborts_before_any_pull(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """C2/AC5: an unreadable .env.prod => non-zero, no pull, no recreate."""
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode != 0
        assert "no pull, no recreate" in result.stderr
        assert sandbox.docker_invocations() == [
            ["docker", "buildx", "imagetools", "inspect", self.PROD_REF]
        ]

    def test_a_matching_digest_is_a_no_op_without_pull_or_recreate(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """Steps 2-3 + #345: the pinned digest is unchanged; the tick reads the lane."""
        env_file = sandbox.write_env_file(DIGEST_A)
        before = env_file.read_text(encoding="utf-8")
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode == 0, result.stderr
        assert "no change" in result.stdout
        assert sandbox.docker_invocations()[0] == [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            self.PROD_REF,
        ]
        # #345: the no-change tick now measures the lane (one `ps`) but never
        # pulls and never recreates a healthy lane.
        assert sum(1 for call in sandbox.docker_invocations() if "ps" in call) == 1
        assert not any("pull" in call for call in sandbox.docker_invocations())
        assert not any("up" in call for call in sandbox.docker_invocations())
        assert env_file.read_text(encoding="utf-8") == before
        assert sandbox.outcome() == "success"
        assert sandbox.lane() == "healthy"

    def test_the_happy_path_pulls_snapshots_swaps_and_gates(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """AC3 (shell half): pull the verified digest, snapshot, swap atomically, gate."""
        env_file = sandbox.write_env_file(DIGEST_B)  # the running release
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode == 0, result.stderr

        # C4: exactly one image line, pinned to the verified digest, mode 600,
        # with the previous line retained for the rollback.
        assert env_file.read_text(encoding="utf-8") == f"{IMAGE_LINE}{DIGEST_A}\n"
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert sandbox.prev_file.read_text(encoding="utf-8") == f"{IMAGE_LINE}{DIGEST_B}\n"
        assert sandbox.prev_file.stat().st_mode & 0o777 == 0o600

        # C2/F1: the *verified* digest is what gets pulled (not a tag).
        assert ["docker", "pull", CORE.image_ref(DIGEST_A)] in sandbox.invocations()

        # C6: one owner-only artifact, larger than the database it seals, streamed
        # out of the container and renamed into place (no staging file remains).
        snapshots = sandbox.snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].stat().st_mode & 0o777 == 0o600
        raw_bytes = 4096
        assert snapshots[0].stat().st_size == raw_bytes * 4 // 3 + 1
        assert snapshots[0].stat().st_size > raw_bytes
        assert list(sandbox.snapshot_dir.glob(".staging-*")) == []

        # #342: the artifact travelled via the exec-stream, never via compose cp.
        assert not any("cp" in call for call in sandbox.docker_invocations())

        # Ordering: provenance -> pull -> measure/snapshot/stream -> recreate -> gate.
        assert (
            self._first(sandbox, "curl", RELEASES_API)
            < self._first(sandbox, "docker", "pull")
            < self._first(sandbox, "docker", "-c")  # the in-container size probe
            < self._first(sandbox, "docker", "coach_web.maintenance")
            < self._first(sandbox, "docker", "cat")  # the exec-stream extraction
            < self._first(sandbox, "docker", "up")
            < self._first(sandbox, "docker", "ps")
            < self._first(sandbox, "curl", HEALTHZ_URL)
        )
        assert sandbox.outcome() == "success"
        assert "update complete" in result.stdout

    def test_a_failed_pull_leaves_the_env_file_byte_identical(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """F1.1/C2: the swap happens only after the image is local."""
        env_file = sandbox.write_env_file(DIGEST_B)
        before = env_file.read_bytes()
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A, "PROD_UPDATE_PULL_OK": "0"})
        assert result.returncode != 0
        assert env_file.read_bytes() == before
        assert not sandbox.prev_file.exists()
        assert sandbox.snapshots() == [], "the snapshot must follow a successful pull"
        assert not any("up" in call for call in sandbox.invocations())
        assert sandbox.outcome() == "failure"
        assert "unchanged, lane untouched" in result.stderr

    def test_a_provenance_mismatch_aborts_before_any_pull_or_rewrite(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """AC2 (shell half)/C1: :prod one digest ahead of the release tags aborts."""
        env_file = sandbox.write_env_file(DIGEST_B)
        before = env_file.read_bytes()
        result = sandbox.run(
            extra_env={"PROD_UPDATE_DIGEST": DIGEST_A, "PROD_UPDATE_TAG_DIGEST": DIGEST_B}
        )
        assert result.returncode != 0
        assert "provenance gate refused" in result.stderr
        assert not any("pull" in call for call in sandbox.invocations())
        assert not any("up" in call for call in sandbox.invocations())
        assert env_file.read_bytes() == before
        assert not sandbox.prev_file.exists()
        assert sandbox.snapshots() == []
        assert sandbox.outcome() == "failure"

    def test_a_gate_failure_rolls_back_to_the_previous_release(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """AC4 (shell half): after the swap, a failed gate restores the previous digest."""
        env_file = sandbox.write_env_file(DIGEST_B)
        result = sandbox.run(
            extra_env={
                "PROD_UPDATE_DIGEST": DIGEST_A,
                "PROD_UPDATE_HEALTHZ_VERSION": "0.13.0",  # never the release version
            }
        )
        assert result.returncode != 0
        assert "rolling back to the previous release" in result.stderr
        assert "post-swap health gate failed" in result.stderr

        # The previous release is pinned again, and its image is brought back
        # after the failed gate (a second `up` follows the gate's `ps` probes).
        assert env_file.read_text(encoding="utf-8") == f"{IMAGE_LINE}{DIGEST_B}\n"
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert sandbox.prev_file.read_text(encoding="utf-8") == f"{IMAGE_LINE}{DIGEST_B}\n"
        assert len(sandbox.snapshots()) == 1, "the pre-swap snapshot stays on disk"
        assert self._last(sandbox, "docker", "up") > self._last(sandbox, "docker", "ps")
        assert sandbox.outcome() == "failure"

    def test_an_oversized_database_aborts_before_the_backup(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """F3/C6: the sealed artifact must fit the container's 64 MB tmpfs (~48 MB)."""
        env_file = sandbox.write_env_file(DIGEST_B)
        before = env_file.read_bytes()
        result = sandbox.run(
            extra_env={"PROD_UPDATE_DIGEST": DIGEST_A, "PROD_UPDATE_DB_BYTES": "52428800"}
        )
        assert result.returncode != 0
        assert "database too large for an in-container snapshot" in result.stderr
        assert not any("coach_web.maintenance" in call for call in sandbox.invocations())
        assert not any("cp" in call for call in sandbox.invocations())
        assert env_file.read_bytes() == before
        assert sandbox.snapshots() == []
        assert sandbox.outcome() == "failure"

    @pytest.mark.parametrize("artifact_bytes", ["0", "10"])
    def test_a_truncated_or_empty_artifact_fails_the_integrity_check(
        self, sandbox: ProdUpdateSandbox, artifact_bytes: str
    ) -> None:
        """F4/C6: a successful stream is not evidence — the artifact must be usable.

        An empty (0-byte) or truncated stream aborts before the swap, and the
        unusable host temp is deleted: `.env.prod` stays byte-identical and no
        artifact (nor staging leftover) reaches the snapshots directory.
        """
        env_file = sandbox.write_env_file(DIGEST_B)
        before = env_file.read_bytes()
        result = sandbox.run(
            extra_env={"PROD_UPDATE_DIGEST": DIGEST_A, "PROD_UPDATE_ARTIFACT_BYTES": artifact_bytes}
        )
        assert result.returncode != 0
        assert "snapshot" in result.stderr
        assert any("coach_web.maintenance" in call for call in sandbox.invocations())
        assert env_file.read_bytes() == before
        assert not sandbox.prev_file.exists()
        assert sandbox.snapshots() == [], "the unusable artifact is removed"
        assert list(sandbox.snapshot_dir.glob(".staging-*")) == []
        assert not any("up" in call for call in sandbox.invocations())
        assert sandbox.outcome() == "failure"

    def test_a_failed_stream_aborts_before_the_swap(self, sandbox: ProdUpdateSandbox) -> None:
        """C6/#342: a failed extraction stream (mid-stream death) aborts the update.

        The `exec -T cat` stream returning non-zero must abort before `.env.prod`
        is touched, with the host temp deleted and no snapshot swapped in.
        """
        env_file = sandbox.write_env_file(DIGEST_B)
        before = env_file.read_bytes()
        result = sandbox.run(
            extra_env={"PROD_UPDATE_DIGEST": DIGEST_A, "PROD_UPDATE_STREAM_OK": "0"}
        )
        assert result.returncode != 0
        assert "cannot stream the snapshot out of the container" in result.stderr
        assert any("coach_web.maintenance" in call for call in sandbox.invocations())
        assert env_file.read_bytes() == before
        assert not sandbox.prev_file.exists()
        assert sandbox.snapshots() == []
        assert list(sandbox.snapshot_dir.glob(".staging-*")) == []
        assert not any("up" in call for call in sandbox.invocations())
        assert sandbox.outcome() == "failure"

    def test_help_lists_the_three_modes(self, sandbox: ProdUpdateSandbox) -> None:
        result = sandbox.run("--help")
        assert result.returncode == 0
        assert "--force" in result.stdout
        assert "--rollback" in result.stdout
        assert sandbox.invocations() == []

    def test_an_unknown_argument_is_refused(self, sandbox: ProdUpdateSandbox) -> None:
        result = sandbox.run("--nope")
        assert result.returncode == 2
        assert "unknown argument" in result.stderr
        assert sandbox.invocations() == []


# --------------------------------------------------------------------------- #
# #345: the no-change tick's lane health signal + bounded self-heal
# --------------------------------------------------------------------------- #


class TestLaneTicks:
    """#345: a swapped-but-degraded lane is detected and healed on the no-op tick.

    Root cause of the v0.14.0 incident: the no-change branch returned before any
    ``dc ps``, so a sidecar that disappeared after the swap ran the lane 2/3 for
    ~20 minutes unnoticed. These tests pin the new behaviour: every no-change tick
    measures the exact 3/3 set, a degraded set triggers exactly ONE bounded
    self-heal with the idempotent project-scoped ``up -d`` (no image pull), and a
    lane that does not recover is recorded as ``degraded`` — never ``success``.
    """

    def test_a_healthy_lane_no_change_tick_is_healthy_without_recreate(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        env_file = sandbox.write_env_file(DIGEST_A)
        before = env_file.read_bytes()
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert result.returncode == 0, result.stderr
        assert sandbox.outcome() == "success"
        assert sandbox.lane() == "healthy"
        assert not any("up" in call for call in sandbox.docker_invocations())
        assert not any("pull" in call for call in sandbox.docker_invocations())
        assert env_file.read_bytes() == before

    def test_a_missing_service_is_healed_once_without_a_pull(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """The v0.14.0 shape: github-mcp gone, the lane reports 2/3."""
        sandbox.write_env_file(DIGEST_A)
        result = sandbox.run(
            extra_env={
                "PROD_UPDATE_DIGEST": DIGEST_A,
                "PROD_UPDATE_PS_SERVICES": "coach-web,coach-mcp",
                "PROD_UPDATE_HEAL_MARKER": str(sandbox.heal_marker),
            }
        )
        assert result.returncode == 0, result.stderr
        ups = [call for call in sandbox.docker_invocations() if "up" in call]
        assert len(ups) == 1, "exactly one bounded self-heal attempt"
        assert "--remove-orphans" in ups[0]
        assert not any("pull" in call for call in sandbox.docker_invocations())
        assert sandbox.outcome() == "success"
        assert sandbox.lane() == "recovered"
        assert "RECOVERED" in result.stdout

    def test_an_unhealthy_service_is_healed_once(self, sandbox: ProdUpdateSandbox) -> None:
        sandbox.write_env_file(DIGEST_A)
        result = sandbox.run(
            extra_env={
                "PROD_UPDATE_DIGEST": DIGEST_A,
                "PROD_UPDATE_HEALTH": "starting",
                "PROD_UPDATE_HEAL_MARKER": str(sandbox.heal_marker),
            }
        )
        assert result.returncode == 0, result.stderr
        assert len([call for call in sandbox.docker_invocations() if "up" in call]) == 1
        assert sandbox.outcome() == "success"
        assert sandbox.lane() == "recovered"

    def test_a_heal_that_does_not_recover_is_recorded_degraded(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """AC1: an unrecovered lane is `degraded`, never `success`, bounded by the gate window."""
        sandbox.write_env_file(DIGEST_A)
        started = time.monotonic()
        result = sandbox.run(
            extra_env={
                "PROD_UPDATE_DIGEST": DIGEST_A,
                "PROD_UPDATE_PS_SERVICES": "coach-web",  # never repaired (no heal marker)
                "COACH_WEB_GATE_TIMEOUT_SECONDS": "2",
            }
        )
        elapsed = time.monotonic() - started
        assert result.returncode == 0, result.stderr
        assert sandbox.outcome() == "degraded"
        assert sandbox.lane() == "degraded"
        assert "DEGRADED" in result.stderr
        assert len([call for call in sandbox.docker_invocations() if "up" in call]) == 1
        assert 2.0 <= elapsed < 30.0, "the re-verify wait is bounded by the gate window"

    def test_a_failed_self_heal_is_recorded_degraded(self, sandbox: ProdUpdateSandbox) -> None:
        sandbox.write_env_file(DIGEST_A)
        result = sandbox.run(
            extra_env={
                "PROD_UPDATE_DIGEST": DIGEST_A,
                "PROD_UPDATE_PS_SERVICES": "coach-web,coach-mcp",
                "PROD_UPDATE_UP_OK": "0",
            }
        )
        assert result.returncode == 0, result.stderr
        assert sandbox.outcome() == "degraded"
        assert sandbox.lane() == "degraded"
        assert "DEGRADED" in result.stderr

    def test_the_degraded_state_does_not_accumulate_the_failure_streak(
        self, sandbox: ProdUpdateSandbox
    ) -> None:
        """A sustained degradation keeps the tick running (exit 0) so it keeps healing.

        The degraded outcome must not accumulate the C8 failure streak, otherwise
        the pause would stop the tick before its lane check — reintroducing the
        blind spot. Several degraded ticks must leave the streak at zero.
        """
        sandbox.write_env_file(DIGEST_A)
        extra = {
            "PROD_UPDATE_DIGEST": DIGEST_A,
            "PROD_UPDATE_PS_SERVICES": "coach-web",
            "COACH_WEB_GATE_TIMEOUT_SECONDS": "1",
        }
        for _ in range(3):
            assert sandbox.run(extra_env=extra).returncode == 0
        state = json.loads(sandbox.state_file.read_text(encoding="utf-8"))
        assert state["outcome"] == "degraded"
        assert state["consecutive_failures"] == 0, "a degraded lane is not an update failure"
        result = sandbox.run(extra_env={"PROD_UPDATE_DIGEST": DIGEST_A})
        assert "paused" not in result.stdout, "a degraded lane must not pause the updater"
