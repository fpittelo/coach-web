"""Behavioral tests for the prod-lane boot supervision (#328, MADR-0024).

Two daemon-free layers, following the ``test_lane_script.py`` convention:

- ``TestUnitTemplate`` / ``TestUpdateUnitTemplates`` pin the committed unit
  templates (``deploy/systemd/coach-web.service.in`` for #328 and
  ``coach-web-update.{service,timer}.in`` for #326) against security conditions
  C4/C5: the hygiene directives, a bounded docker-readiness retry whose window is
  strictly inside ``TimeoutStartSec``, ``--env-file`` and never
  ``EnvironmentFile=``, the 5-minute timer directives, plus the absence of any
  secret marker or hardcoded checkout path.
- ``TestProdInstall`` drives ``scripts/prod-install.sh`` in a temp ``HOME``
  with PATH shims for ``systemctl``/``loginctl``/``docker``/``curl``: template
  rendering + enable of all three units (#328 + #326), idempotency, the linger
  assertion, the C7 boot-unit audit list and ``--uninstall`` leaving the data
  volume alone.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT_ROOT / "deploy" / "systemd" / "coach-web.service.in"
UPDATE_SERVICE_TEMPLATE = PROJECT_ROOT / "deploy" / "systemd" / "coach-web-update.service.in"
UPDATE_TIMER_TEMPLATE = PROJECT_ROOT / "deploy" / "systemd" / "coach-web-update.timer.in"
INSTALLER = PROJECT_ROOT / "scripts" / "prod-install.sh"

SECRET_MARKERS = (
    "AUTH_SESSION_SECRET",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "DATA_ENCRYPTION_KEY",
    "GOOGLE_OIDC_CLIENT_SECRET",
    "INTERVALS_API_KEY",
)


def _non_comment_lines(text: str) -> str:
    """The template text with its comment lines removed (C4 marker checks)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


# A value no output may ever contain (C4: the lane env content never leaks).
ENV_SENTINEL = "walled-garden-sentinel-42"

SHIM_SOURCE = '''#!/usr/bin/env python3
"""Test shim for systemctl/loginctl/docker/curl: records argv, canned output."""
import json
import os
import sys

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["PROD_INSTALL_SHIM_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps([name, *args]) + "\\n")

if name == "loginctl":
    print(os.environ.get("PROD_INSTALL_LINGER", "yes"))
elif name == "systemctl":
    if "is-active" in args:
        print("active")
    elif "is-enabled" in args:
        print("enabled")
elif name == "docker":
    if "ps" in args:
        print("coach-web healthy")
elif name == "curl":
    print('{"status": "ok", "version": "1.2.3"}')
sys.exit(0)
'''


class TestUnitTemplate:
    """The committed unit template carries the C4/C5 contract."""

    def _text(self) -> str:
        return TEMPLATE.read_text(encoding="utf-8")

    def test_template_exists(self) -> None:
        """The repo-tracked unit template exists."""
        assert TEMPLATE.is_file(), f"{TEMPLATE} must be repo-tracked"

    def test_template_declares_oneshot_remain_after_exit(self) -> None:
        """Type=oneshot + RemainAfterExit + WantedBy=default.target (AC1)."""
        text = self._text()
        assert "Type=oneshot" in text
        assert "RemainAfterExit=yes" in text
        assert "WantedBy=default.target" in text

    def test_template_has_the_c5_hygiene_set(self) -> None:
        """The C5 hygiene directives are all present."""
        text = self._text()
        for directive in (
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "UMask=0077",
            "Environment=PATH=",
            "Restart=on-failure",
            "StartLimitBurst=",
            "StartLimitIntervalSec=",
            "ExecStartPre=",
            "TimeoutStartSec=",
        ):
            assert directive in text, directive

    def test_readiness_retry_window_fits_inside_timeout_start(self) -> None:
        """attempts x delay < TimeoutStartSec (a tighter timeout kills the loop)."""
        text = self._text()
        attempts = re.search(r"seq 1 (\d+)", text)
        delay = re.search(r"sleep (\d+)", text)
        timeout = re.search(r"TimeoutStartSec=(\d+)", text)
        assert attempts is not None, "no bounded retry count in ExecStartPre"
        assert delay is not None, "no retry delay in ExecStartPre"
        assert timeout is not None, "no TimeoutStartSec"
        assert int(attempts.group(1)) * int(delay.group(1)) < int(timeout.group(1))

    def test_template_runs_the_prod_project_from_trunk_files(self) -> None:
        """ExecStart/ExecStop use the prod project, both files and --env-file."""
        text = self._text()
        assert "-p coach-web-prod" in text
        assert "--env-file @REPO_ROOT@/.env.prod" in text
        assert "@REPO_ROOT@/compose.yaml" in text
        assert "@REPO_ROOT@/compose.prod.yml" in text
        assert "up -d --remove-orphans" in text
        assert "down --remove-orphans" in text

    def test_template_never_carries_a_secret(self) -> None:
        """C4: --env-file only, no EnvironmentFile= directive, no secret marker."""
        text = self._text()
        directives = _non_comment_lines(text)
        assert "EnvironmentFile=" not in directives
        assert "docker compose config" not in directives
        for marker in SECRET_MARKERS:
            assert marker not in text, marker

    def test_template_is_placeholder_based(self) -> None:
        """@REPO_ROOT@ is the render point — no hardcoded checkout path."""
        text = self._text()
        assert "@REPO_ROOT@" in text
        assert "/home/" not in text, "the committed template must not hardcode a home path"


class TestUpdateUnitTemplates:
    """The #326 auto-update units carry the same C4/C5 contract."""

    def test_templates_exist(self) -> None:
        """Both update unit templates are repo-tracked (#326)."""
        assert UPDATE_SERVICE_TEMPLATE.is_file()
        assert UPDATE_TIMER_TEMPLATE.is_file()

    def test_service_template_runs_the_entrypoint_as_a_oneshot(self) -> None:
        """The timer-activated oneshot runs scripts/prod-update.sh (thin wrapper)."""
        text = UPDATE_SERVICE_TEMPLATE.read_text(encoding="utf-8")
        assert "Type=oneshot" in text
        assert "ExecStart=/bin/bash @REPO_ROOT@/scripts/prod-update.sh" in text
        assert "WorkingDirectory=@REPO_ROOT@" in text
        assert "WantedBy=default.target" in text

    def test_service_template_has_the_c5_hygiene_set(self) -> None:
        """The C5 hygiene directives, identical to the boot unit, are all present."""
        text = UPDATE_SERVICE_TEMPLATE.read_text(encoding="utf-8")
        for directive in (
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "UMask=0077",
            "Environment=PATH=",
            "Restart=on-failure",
            "StartLimitBurst=",
            "StartLimitIntervalSec=",
            "TimeoutStartSec=",
        ):
            assert directive in text, directive

    def test_service_template_timeout_covers_the_update_window(self) -> None:
        """TimeoutStartSec must exceed the 120 s health gate, not the 90 s default."""
        text = UPDATE_SERVICE_TEMPLATE.read_text(encoding="utf-8")
        match = re.search(r"TimeoutStartSec=(\d+)", text)
        assert match is not None, "no TimeoutStartSec"
        assert int(match.group(1)) > 120

    def test_service_template_never_carries_a_secret(self) -> None:
        """C4: --env-file only, no EnvironmentFile= directive, no secret marker."""
        text = UPDATE_SERVICE_TEMPLATE.read_text(encoding="utf-8")
        assert "EnvironmentFile=" not in _non_comment_lines(text)
        assert "docker compose config" not in _non_comment_lines(text)
        for marker in SECRET_MARKERS:
            assert marker not in text, marker

    def test_timer_template_ticks_every_five_minutes(self) -> None:
        """5-minute schedule, boot catch-up, jitter, timer install target (#326)."""
        text = UPDATE_TIMER_TEMPLATE.read_text(encoding="utf-8")
        assert "OnBootSec=5min" in text
        assert "OnUnitActiveSec=5min" in text
        assert "Persistent=true" in text
        assert "RandomizedDelaySec=30" in text
        assert "WantedBy=timers.target" in text

    def test_templates_are_placeholder_based(self) -> None:
        """@REPO_ROOT@ is the render point — no hardcoded checkout path."""
        for template in (UPDATE_SERVICE_TEMPLATE, UPDATE_TIMER_TEMPLATE):
            text = template.read_text(encoding="utf-8")
            assert "@REPO_ROOT@" in text, template
            assert "/home/" not in text, template


class ProdInstallSandbox:
    """Temp HOME + repo copy with recording shims for the system tools."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin_dir = tmp_path / "bin"
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.log = tmp_path / "shim.log"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "deploy" / "systemd").mkdir(parents=True)
        self.bin_dir.mkdir()
        self.home.mkdir()
        shutil.copy2(INSTALLER, self.repo / "scripts" / "prod-install.sh")
        (self.repo / "scripts" / "prod-install.sh").chmod(0o755)
        for template in (TEMPLATE, UPDATE_SERVICE_TEMPLATE, UPDATE_TIMER_TEMPLATE):
            shutil.copy2(template, self.repo / "deploy" / "systemd" / template.name)
        for name in ("compose.yaml", "compose.prod.yml"):
            (self.repo / name).touch()
        env_file = self.repo / ".env.prod"
        env_file.write_text(f"OPENROUTER_API_KEY={ENV_SENTINEL}\n", encoding="utf-8")
        env_file.chmod(0o600)
        self.log.touch()
        for name in ("docker", "systemctl", "loginctl", "curl"):
            shim = self.bin_dir / name
            shim.write_text(SHIM_SOURCE, encoding="utf-8")
            shim.chmod(0o755)

    @property
    def unit_path(self) -> Path:
        """The installed unit path under the sandbox HOME."""
        return self.home / ".config" / "systemd" / "user" / "coach-web.service"

    @property
    def update_service_path(self) -> Path:
        """The installed #326 update service path under the sandbox HOME."""
        return self.home / ".config" / "systemd" / "user" / "coach-web-update.service"

    @property
    def timer_path(self) -> Path:
        """The installed #326 update timer path under the sandbox HOME."""
        return self.home / ".config" / "systemd" / "user" / "coach-web-update.timer"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Process environment with the shims first on PATH and a temp HOME."""
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["HOME"] = str(self.home)
        env.pop("XDG_CONFIG_HOME", None)
        env["PROD_INSTALL_SHIM_LOG"] = str(self.log)
        if extra:
            env.update(extra)
        return env

    def run(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the sandboxed installer with the shims first on PATH."""
        return subprocess.run(
            [str(self.repo / "scripts" / "prod-install.sh"), *args],
            cwd=self.repo,
            env=self.env(extra_env),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def invocations(self) -> list[list[str]]:
        """Return the recorded argv of every shim invocation, in order."""
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def sandbox(tmp_path: Path) -> ProdInstallSandbox:
    """A temp-HOME sandbox with recording system-tool shims."""
    return ProdInstallSandbox(tmp_path)


class TestProdInstall:
    """Install / refresh / uninstall behavior (shimmed, daemon-free)."""

    def test_install_renders_and_enables_the_unit(self, sandbox: ProdInstallSandbox) -> None:
        """Install renders @REPO_ROOT@, daemon-reloads and enables the unit (AC2)."""
        result = sandbox.run()
        assert result.returncode == 0, result.stderr
        assert sandbox.unit_path.is_file()
        text = sandbox.unit_path.read_text(encoding="utf-8")
        assert "@REPO_ROOT@" not in text
        assert str(sandbox.repo) in text
        assert "Type=oneshot" in text
        commands = sandbox.invocations()
        assert ["systemctl", "--user", "daemon-reload"] in commands
        assert ["systemctl", "--user", "enable", "--now", "coach-web.service"] in commands

    def test_second_install_is_an_idempotent_no_op(self, sandbox: ProdInstallSandbox) -> None:
        """A second install leaves the unit byte-identical and reports a no-op (AC2)."""
        assert sandbox.run().returncode == 0
        before = sandbox.unit_path.read_text(encoding="utf-8")
        second = sandbox.run()
        assert second.returncode == 0, second.stderr
        assert sandbox.unit_path.read_text(encoding="utf-8") == before
        assert "already up to date" in second.stdout

    def test_install_fails_without_linger_and_prints_the_remediation(
        self, sandbox: ProdInstallSandbox
    ) -> None:
        """No linger => non-zero exit, exact remediation, no unit installed (AC3)."""
        result = sandbox.run(extra_env={"PROD_INSTALL_LINGER": "no"})
        assert result.returncode != 0
        assert "loginctl enable-linger" in result.stderr
        assert not sandbox.unit_path.exists(), "no unit may be installed without linger"

    def test_install_reports_boot_persistent_user_units(self, sandbox: ProdInstallSandbox) -> None:
        """The C7 audit list names the enabled user units (AC3)."""
        wants = sandbox.home / ".config" / "systemd" / "user" / "default.target.wants"
        wants.mkdir(parents=True)
        (wants / "other-agent.service").symlink_to(sandbox.home / "other-agent.service")
        result = sandbox.run()
        assert result.returncode == 0, result.stderr
        assert "boot-persistent user units" in result.stdout
        assert "other-agent.service" in result.stdout

    def test_uninstall_removes_the_unit_and_keeps_the_data_volume(
        self, sandbox: ProdInstallSandbox
    ) -> None:
        """--uninstall disables + removes the unit without touching the volume (AC2)."""
        assert sandbox.run().returncode == 0
        sandbox.log.write_text("", encoding="utf-8")
        result = sandbox.run("--uninstall")
        assert result.returncode == 0, result.stderr
        assert not sandbox.unit_path.exists()
        commands = sandbox.invocations()
        assert ["systemctl", "--user", "disable", "--now", "coach-web.service"] in commands
        assert all(
            invocation[0] != "docker" for invocation in commands
        ), "uninstall must not run any docker command (no volume removal)"
        assert "coach-web-prod_coach-data" in result.stdout
        assert "-v" not in json.dumps(commands)

    def test_install_never_prints_the_env_secret(self, sandbox: ProdInstallSandbox) -> None:
        """C4: the lane env file value never reaches stdout/stderr."""
        result = sandbox.run()
        assert result.returncode == 0, result.stderr
        assert ENV_SENTINEL not in result.stdout
        assert ENV_SENTINEL not in result.stderr

    def test_install_renders_and_enables_the_auto_update_units(
        self, sandbox: ProdInstallSandbox
    ) -> None:
        """#326 AC2 carry-over: the update service + its timer are installed and enabled."""
        result = sandbox.run()
        assert result.returncode == 0, result.stderr
        assert sandbox.update_service_path.is_file()
        assert sandbox.timer_path.is_file()
        rendered = sandbox.timer_path.read_text(encoding="utf-8")
        assert "@REPO_ROOT@" not in rendered
        assert str(sandbox.repo) in rendered
        assert "OnUnitActiveSec=5min" in rendered
        assert ["systemctl", "--user", "enable", "--now", "coach-web-update.timer"] in (
            sandbox.invocations()
        )

    def test_install_of_the_three_units_is_idempotent(self, sandbox: ProdInstallSandbox) -> None:
        """A second install rewrites nothing and reports one no-op per unit."""
        assert sandbox.run().returncode == 0
        paths = (sandbox.unit_path, sandbox.update_service_path, sandbox.timer_path)
        before = [path.read_text(encoding="utf-8") for path in paths]
        second = sandbox.run()
        assert second.returncode == 0, second.stderr
        assert second.stdout.count("already up to date") == 3
        assert [path.read_text(encoding="utf-8") for path in paths] == before

    def test_uninstall_stops_the_timer_before_the_lane(self, sandbox: ProdInstallSandbox) -> None:
        """--uninstall leaves no update unit behind and tears the lane down last."""
        assert sandbox.run().returncode == 0
        sandbox.log.write_text("", encoding="utf-8")
        result = sandbox.run("--uninstall")
        assert result.returncode == 0, result.stderr
        assert not sandbox.timer_path.exists()
        assert not sandbox.update_service_path.exists()
        assert not sandbox.unit_path.exists()
        commands = sandbox.invocations()
        assert ["systemctl", "--user", "disable", "--now", "coach-web-update.timer"] in commands
        assert commands.index(
            ["systemctl", "--user", "disable", "--now", "coach-web-update.timer"]
        ) < commands.index(["systemctl", "--user", "disable", "--now", "coach-web.service"])


class TestInstallerScript:
    """The installer keeps strict mode and owner-only umask."""

    def test_script_exists_and_is_executable(self) -> None:
        """scripts/prod-install.sh exists and is executable."""
        assert INSTALLER.is_file()
        assert INSTALLER.stat().st_mode & 0o111, "script must be executable"

    def test_script_passes_bash_syntax_check(self) -> None:
        """bash -n validates the script syntax."""
        result = subprocess.run(
            ["/bin/bash", "-n", str(INSTALLER)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_script_uses_strict_mode_and_owner_only_umask(self) -> None:
        """set -euo pipefail + umask 077 before anything else runs."""
        script = INSTALLER.read_text(encoding="utf-8")
        assert "set -euo pipefail" in script
        assert "umask 077" in script
