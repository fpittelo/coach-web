"""Plan approval orchestration for Coach Web.

Approving a plan proposal performs two independent side effects:

1. Schedule the structured workout on Intervals.icu through ``coach-mcp``
   (``intervals_create_event``).
2. Commit a Markdown rendition of the plan to the training repository through
   ``github-mcp`` (``create_or_update_file``).

Both side effects are attempted independently so a partial failure still
reports exactly what succeeded and what did not.
"""

import json
from datetime import date as date_type
from typing import Any, Literal, Protocol

from coach_web.models import (
    ApprovalStepResult,
    PlanApprovalResponse,
    PlanProposal,
    WorkoutStep,
)

COACH_CREATE_EVENT_TOOL = "coach-mcp__intervals_create_event"
"""Namespaced coach-mcp tool that schedules an Intervals.icu event."""

GITHUB_COMMIT_FILE_TOOL = "github-mcp__create_or_update_file"
"""Namespaced github-mcp tool that commits a single file."""

DEFAULT_PLAN_REPO = "fpittelo/coach"
"""Repository receiving the Markdown training plan."""

DEFAULT_PLAN_BRANCH = "main"
"""Branch receiving the Markdown training plan."""

DEFAULT_PLAN_DIRECTORY = "plans"
"""Directory inside the repository holding the Markdown plans."""

EVENT_START_TIME = "08:00:00"
"""Local start time assigned to a scheduled workout."""

ApprovalStep = Literal["intervals_event", "github_commit"]
"""Identifier of an approval side effect."""


class ToolExecutor(Protocol):
    """Structural contract for the MCP hub used during approval."""

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a tool and return its JSON string payload."""
        ...


def resolve_plan_date(plan: PlanProposal) -> str:
    """Return the scheduled date, defaulting to the ISO week's Monday."""
    if plan.date:
        return plan.date
    year, week = plan.week_id.split("-W")
    return date_type.fromisocalendar(int(year), int(week), 1).isoformat()


def build_workout_doc(plan: PlanProposal) -> str:
    """Render the plan steps as an Intervals.icu workout DSL document."""
    lines: list[str] = []
    for step in plan.steps:
        segments = [f"- {step.label} {_format_duration(step.duration_minutes)}"]
        target = _format_dsl_target(step)
        if target:
            segments.append(target)
        if step.cadence_rpm is not None:
            segments.append(f"{step.cadence_rpm}rpm")
        lines.append(" ".join(segments))
    return "\n".join(lines)


def build_event_arguments(plan: PlanProposal, target_date: str) -> dict[str, Any]:
    """Build the ``intervals_create_event`` argument payload for a plan."""
    arguments: dict[str, Any] = {
        "name": plan.title,
        "type": "Ride",
        "category": "WORKOUT",
        "start_date_local": f"{target_date}T{EVENT_START_TIME}",
        "description": plan.summary,
        "moving_time_seconds": int(round(_total_minutes(plan) * 60)),
    }
    workout_doc = build_workout_doc(plan)
    if workout_doc:
        arguments["workout_doc"] = workout_doc
    return arguments


def build_plan_markdown(plan: PlanProposal, target_date: str) -> str:
    """Render a plan proposal as a Markdown document."""
    lines = [
        f"# {plan.title}",
        "",
        f"- **Week:** {plan.week_id}",
        f"- **Date:** {target_date}",
        f"- **Duration:** {_format_duration(_total_minutes(plan))}",
        "",
        "## Summary",
        "",
        plan.summary,
    ]
    if plan.rationale:
        lines += ["", "## Rationale", "", plan.rationale]
    if plan.steps:
        lines += [
            "",
            "## Intervals",
            "",
            "| Interval | Duration | Target | Cadence |",
            "|:---|:---|:---|:---|",
        ]
        for step in plan.steps:
            target = _format_target(step) or "—"
            cadence = f"{step.cadence_rpm} rpm" if step.cadence_rpm is not None else "—"
            lines.append(
                f"| {step.label} | {_format_duration(step.duration_minutes)} "
                f"| {target} | {cadence} |"
            )
    lines.append("")
    return "\n".join(lines)


async def approve_plan(
    hub: ToolExecutor,
    plan: PlanProposal,
    *,
    repo: str = DEFAULT_PLAN_REPO,
    branch: str = DEFAULT_PLAN_BRANCH,
    directory: str = DEFAULT_PLAN_DIRECTORY,
) -> PlanApprovalResponse:
    """Execute both approval side effects and aggregate their outcomes."""
    target_date = resolve_plan_date(plan)
    steps: list[ApprovalStepResult] = []

    event_raw = await hub.execute_tool(
        COACH_CREATE_EVENT_TOOL,
        build_event_arguments(plan, target_date),
    )
    steps.append(_step_result("intervals_event", event_raw))

    steps.append(await _commit_markdown(hub, plan, target_date, repo, branch, directory))

    successes = sum(1 for step in steps if step.success)
    status: Literal["approved", "partial", "failed"]
    if successes == len(steps):
        status = "approved"
    elif successes:
        status = "partial"
    else:
        status = "failed"

    return PlanApprovalResponse(
        status=status,
        plan_title=plan.title,
        week_id=plan.week_id,
        date=target_date,
        steps=steps,
    )


async def _commit_markdown(
    hub: ToolExecutor,
    plan: PlanProposal,
    target_date: str,
    repo: str,
    branch: str,
    directory: str,
) -> ApprovalStepResult:
    """Commit the Markdown plan through github-mcp, validating the repo slug."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return ApprovalStepResult(
            step="github_commit",
            success=False,
            error=f"Invalid repository slug: {repo!r}",
        )

    arguments: dict[str, Any] = {
        "owner": owner,
        "repo": name,
        "path": f"{directory.strip('/')}/{plan.week_id}.md",
        "content": build_plan_markdown(plan, target_date),
        "message": f"docs(plan): add {plan.week_id} training plan ({plan.title})",
        "branch": branch,
    }
    raw = await hub.execute_tool(GITHUB_COMMIT_FILE_TOOL, arguments)
    return _step_result("github_commit", raw)


def _step_result(step: ApprovalStep, raw: str) -> ApprovalStepResult:
    """Convert a raw tool response into a structured step outcome."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ApprovalStepResult(step=step, success=False, error="Malformed tool response")
    if not isinstance(parsed, dict):
        return ApprovalStepResult(step=step, success=False, error="Malformed tool response")

    error = parsed.get("error")
    if error:
        return ApprovalStepResult(step=step, success=False, error=str(error))
    return ApprovalStepResult(step=step, success=True, detail=parsed)


def _total_minutes(plan: PlanProposal) -> float:
    """Return the summed duration of every plan step in minutes."""
    return sum(step.duration_minutes for step in plan.steps)


def _format_duration(minutes: float) -> str:
    """Format a duration in minutes using the compact ``15m`` notation."""
    return f"{minutes:g}m"


def _format_target(step: WorkoutStep) -> str:
    """Format a step's power target for the Markdown table, preferring watts."""
    if step.target_power_watts is not None:
        if step.target_power_pct is not None:
            return f"{step.target_power_watts} W ({step.target_power_pct:g}%)"
        return f"{step.target_power_watts} W"
    if step.target_power_pct is not None:
        return f"{step.target_power_pct:g}%"
    return ""


def _format_dsl_target(step: WorkoutStep) -> str:
    """Format a step's power target for the Intervals.icu workout DSL."""
    if step.target_power_watts is not None:
        return f"{step.target_power_watts}w"
    if step.target_power_pct is not None:
        return f"{step.target_power_pct:g}%"
    return ""
