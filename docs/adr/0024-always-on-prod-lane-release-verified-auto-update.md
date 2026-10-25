# MADR-0024: Always-On Prod Lane with Release-Verified Auto-Update

- **Status:** accepted
- **Date:** 2026-10-25
- **Deciders:** @architect, @fpittelo
- **Reviewed by:** @cyber-security (MADR-0008 security-impact routing — unattended image replacement on the daily-use lane; APPROVE-WITH-CONDITIONS C1–C8, review recorded on fpittelo/coach-web#327)
- **Tracked in:** fpittelo/coach-web#327, Sprint 29 marker #331; to be implemented by #328 (boot supervision) and #326 (auto-update), proven live by #329 (all three `status::ready` at time of writing); flow order amended by #336 after the #326 implementation review

> **Acceptance basis:** PO directive 2026-10-25 — "automated continuous deployment in production for each release… the docker image must be up and running all the time when I start my PC, and updated for each release" — plus the PO's instruction to start the Sprint 29 autonomous loop. Presented for PO review at sprint closeout.
>
> **Numbering note:** takes the next free **portfolio** number (0021 is the model-roster ADR in `opencode-home-config/docs/adr`). MADR-0001–0007 remain sections of `docs/architecture.md`, MADR-0009 is the appendix of `docs/e2e-checklist.md`; 0012–0023 are portfolio-level and indexed centrally.

## Context

The **prod lane** (`compose.yaml` + `compose.prod.yml`, project `coach-web-prod`) is the PO's daily-use deployment: three containers on the VIDAR workstation, published on `127.0.0.1:8000` only, OIDC whitelist enabled, `coach-web` running a **digest-pinned** release image (`COACH_WEB_IMAGE`, enforced by compose interpolation), sidecars digest-pinned as well. This is the whole production topology (ADR-007 §10 — there is no cloud).

Two operational gaps remain against the PO's requirement:

1. **Boot:** the lane is started by hand (`docker compose … up -d`). After a reboot or a crash nothing brings it back.
2. **Release rollout:** step 2 of the Release Runbook is manual — resolve the digest from the release notes, edit `.env.prod`, `pull`, run `e2e-preflight.sh prod`, `up -d`, `curl /health`. Five human steps per release, each of which can be skipped or botched.

Forces that constrain any automation:

- **The PO release approval is the deploy gate** (home-governance Rule 5, MADR-0017). Automation must not create a second, weaker path to production.
- **The digest pin is deliberate.** It exists so a registry-side tag move cannot silently change what runs (#113, #275, ADR-007). "Follow the `:prod` tag" would delete that control.
- **`scripts/e2e-preflight.sh` tears the lane down on exit** (validate-then-clean semantics). Any background supervisor can resurrect the lane mid-validation and corrupt the evidence.
- **Docker is a system unit; systemd user units cannot order against it.** A user unit started at boot will race the daemon; ordering must be handled by the job itself.
- **n=1, Swiss nLPD:** single user, health data at rest encrypted, no team to share operational burden with; every added daemon is a permanent cost and a permanent attack surface.

## Decision Drivers

1. **Zero manual steps after a release** — the PO's explicit requirement; a tag must be sufficient.
2. **Preserve provenance** — the running artifact must provably be the release the PO approved, not merely "the newest thing in the registry".
3. **No new attack surface** — no inbound listener, no new privileged container, no new credential.
4. **Boot reliability without a login** — the lane must serve while nobody is logged in, including when docker is slower than the session.
5. **Failure must be safe** — a bad pull, a bad image, or a failed migration leaves the previous release serving; data rollback is a first-class, rehearsed path.
6. **KIS/YAGNI** — the smallest mechanism that meets 1–5 on one workstation.

## Considered Options

### Option 1: Keep the manual runbook (status quo)

- Pros: zero new code; every rollout is a conscious human act; no new failure mode.
- Cons: fails the requirement outright (manual boot, manual rollout); invites skipped preflight steps and undocumented digest drift between releases.

### Option 2: Watchtower-style pull-and-restart agent

- Pros: smallest configuration; industry-familiar pattern; no scripts to own.
- Cons: requires a new container with the docker socket (root-equivalent) and Docker-API trust; follows a mutable tag with **no provenance** — it cannot tell an approved release from a registry-side push; no health-gated rollback, no pre-swap data snapshot; one more daemon to patch. Rejected on drivers 2, 3 and 5.

### Option 3: Push-based deployment from CI (self-hosted runner or inbound webhook)

- Pros: immediate on tag; deployment intent stays in the pipeline; timer-free.
- Cons: a self-hosted runner is a persistent privileged agent on the workstation; a webhook needs an inbound listener plus a shared secret on a machine that sleeps. Both add permanent surface for a latency gain of minutes. Rejected on driver 3 (and YAGNI at n=1).

### Option 4: systemd user units + provenance-gated pull timer (chosen)

- **Supervision:** `coach-web.service` (user unit, `Type=oneshot`, `RemainAfterExit=yes`, `WantedBy=default.target`) runs the compose `up -d` with a bounded docker-readiness retry; `loginctl enable-linger` makes it boot-persistent without a login.
- **Update:** `coach-web-update.timer` (5 min, `Persistent=true`) runs a script that:
  1. takes a shared `flock` (the preflight, the lane unit, and any manual rollout take the same lock);
  2. resolves `ghcr.io/fpittelo/coach-web:prod` and validates the digest;
  3. no-ops when it equals the digest in `.env.prod`;
  4. **provenance gate:** requires `digest(:prod) == digest(:vX.Y.Z)` for the latest non-prerelease release tag, resolved through the public Releases API (never the application's `GITHUB_TOKEN`);
  5. **pulls the verified digest** (by digest, not by tag) — a pull failure leaves `.env.prod` byte-identical and the lane untouched;
  6. measures the database and writes an encrypted pre-swap snapshot (off-volume, mode 600, last 4 retained);
  7. rewrites the single `COACH_WEB_IMAGE=` line atomically (owner/mode 600 verified before and after; previous digest retained);
  8. recreates, then requires 3/3 healthy within 120 s **and** `/healthz` reporting the release version;
  9. on any failure after the swap, restores the previous digest and brings the previous image back.
- Pros: no new surface (no listener, no privileged container, no credential); provenance is checked rather than assumed; the digest pin survives; rollback is a two-line state restore; the mechanism is repo-tracked, reviewable bash plus a unit-tested Python helper.
- Cons: updates are not instantaneous (≤5 min after the release is published); the script runs with docker-group privilege; the timer is one more thing to understand.

## Decision Outcome

Chosen option: **"Option 4: systemd user units + provenance-gated pull timer"**, because it is the only option that satisfies zero-manual-step rollout and boot persistence while keeping the provenance guarantee (driver 2), adding no network surface or privileged component (driver 3), and remaining small enough for one person to own (driver 6).

The manual Release Runbook survives, narrowed to two roles: first-time/bootstrap verification and the documented fallback when the automation is disabled or a release is explicitly excluded (a destructive migration).

### Amendment (2026-10-25 — #326, PR #335)

Implementation review found that steps 5–7 as first written (snapshot → rewrite → pull) left a window in which `.env.prod` could pin an image that was neither pulled nor health-gated, while the boot unit or a manual `systemctl --user restart` could recreate the lane from it. The delivered order **pulls the provenance-verified digest before the snapshot and the rewrite**, so the env file only ever pins an image that is already local and verified; the lane unit (`flock -w 900`) and every operator path now take the same lock as the tick. This amendment refines the flow; it does not change the decision.

Residual window after the amendment: an interruption between the atomic rewrite and the completion of `up -d` + gate leaves `.env.prod` pinning a provenance-verified, already-pulled digest while the previous release still serves; the next tick reports `no change` and does not recreate the lane. Recovery: `--force` or `--rollback`. No unverified deployment is possible through this window.

## Consequences

- **Positive:** a tag is sufficient — the lane converges to the approved release within 5 minutes with no human step; the lane comes back by itself after reboot or crash; the deployed digest is always traceable to a release tag; failed updates leave the previous release serving.
- **Negative:** an unattended bad release reaches the daily-use lane within minutes; image rollback does not roll back a schema — a destructive migration coupled with a health-gate miss can strand the data; the update path itself is privileged (docker group) and must be reviewed like production code; polling consumes registry reads every 5 minutes (`docker buildx imagetools inspect` — no image layers pulled when unchanged).
- **Mitigations:** provenance gate (C1) and fail-closed resolution (C2); expand-contract migrations plus a pre-swap encrypted snapshot and a rehearsed restore (C6); health + version gate with automatic image rollback; bounded failure streak and a status file so a broken registry is visible instead of silently retried (C8); a single shared lock so preflight, timer and manual rollouts cannot interleave (C3).

## Security Routing (@cyber-security, MADR-0008)

Verdict: **APPROVE-WITH-CONDITIONS**. Conditions C1–C6 are binding on the implementation; C7–C8 are required but not sprint-blocking.

| # | Condition | Status |
| :--- | :--- | :--- |
| C1 | Never trust the moving `:prod` tag alone: `digest(:prod)` must equal the digest of the latest non-prerelease release tag | binding |
| C2 | Fail-safe: registry unreachable / rate-limited / tag absent / malformed digest ⇒ exit non-zero without any pull or recreate | binding |
| C3 | One lock shared by the tick, the lane unit (start/stop), the prod preflight and the operator paths (`--force`/`--rollback`) — the unattended tick skips, every other actor waits (bounded) | binding |
| C4 | `.env.prod`: `umask 077`, no `set -x`, owner/mode 600 verified before and after, tempfile+rename, exactly one image line, `--env-file` only (never `EnvironmentFile=`), never log `docker compose config` | binding |
| C5 | Unit hygiene: `NoNewPrivileges`, `PrivateTmp`, `UMask=0077`, explicit `PATH`, `Restart=on-failure` + `StartLimit*`, `TimeoutStartSec` above the boot retry window | binding |
| C6 | Image rollback ≠ data rollback: pre-swap encrypted snapshot, expand-contract migrations, destructive-migration releases excluded from unattended update | binding |
| C7 | `enable-linger` widens boot scope to every enabled user unit — audit and record the list | follow-up |
| C8 | Bounded consecutive failures + visible last-run status | follow-up |

Residual risk accepted (verbatim from the review comment archived on fpittelo/coach-web#327):

- Anyone holding a GitHub token with `packages:write` can move `:prod`; C1 makes the lane refuse such an image only if it does not correspond to a real release tag — a *compromised CI workflow publishing a genuine-looking release* would still deploy unattended.
- Auto-deploy means a bad release reaches the daily-use lane within 5 minutes of tagging, with no human in the loop at that moment; recovery depends on the health gate and the C6 snapshot.
- The update script executes with docker-group privilege as `frede`; a flaw in it (or in what `:prod` serves) is root-equivalent on VIDAR.
- Rollback of the image is automatic; rollback of schema/data depends on the C6 snapshot being present and untested-in-anger until it is actually needed.

## Operational Contract

| Property | Target |
| :--- | :--- |
| Boot-to-serving | lane healthy without an interactive login (`linger`), docker readiness retried within `TimeoutStartSec` |
| Update latency | ≤ 5 minutes after the GitHub Release is published (`Persistent=true` timer) |
| Update gate | provenance match + 3/3 healthy ≤ 120 s + `/healthz` version == release tag |
| No-op cost | one digest resolution per tick, no image pull, no container restart |
| Rollback (image) | automatic on failed gate; manual = restore previous digest + `systemctl --user restart coach-web` |
| Rollback (data) | restore the pre-swap encrypted snapshot with the existing maintenance CLI (#274) |
| Snapshot ceiling | ~48 MB raw DB (64 MB container tmpfs ÷ 4/3) — a larger DB aborts the update fail-safe |
| Interrupted swap | env may pin an already-pulled, provenance-verified digest while the previous release serves; next tick is a no-op — recover with `--force` / `--rollback` |
| Excluded from automation | releases carrying a destructive migration (documented manual path) |

## References

- PO directive 2026-10-25; Sprint 29 marker fpittelo/coach-web#331
- fpittelo/coach-web#327 (this record), #328 (boot supervision), #326 (auto-update), #329 (live proof)
- Security review: fpittelo/coach-web#327 comment (C1–C8 + residual risk)
- ADR-007 §10 (local-first deployment), MADR-0008 (security routing), MADR-0017 (trunk + tag), MADR-0020 (release publishing)
- `compose.prod.yml`, `scripts/e2e-preflight.sh`, `docs/admin_guide.md` § Release Runbook
