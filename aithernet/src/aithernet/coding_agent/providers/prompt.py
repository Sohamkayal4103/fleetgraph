"""Provider-neutral coding-task prompt construction.

Shared by the Claude Code and Codex CLI providers so the task framing is identical
regardless of which real agent executes it. The prompt frames the agent as the Aithernet
node's coding agent, scopes it to the workspace, and asks for a structured final report —
without prescribing any fixed SDR workflow or pretending unavailable capabilities exist.
"""

from __future__ import annotations

import json

from aithernet.coding_agent.contracts import CodingTaskExecutionInput


def build_task_prompt(task: CodingTaskExecutionInput) -> str:
    """Construct the agent prompt from the task contract (provider-neutral)."""
    context = {
        "objective": task.objective,
        "mission_id": task.mission_id,
        "task_id": task.task_id,
        "context": task.context,
        "available_tools": task.available_tools,
        "expected_outputs": task.expected_outputs,
        "reporting_requirements": task.reporting_requirements,
        "workspace": task.workspace,
        "current_time": task.current_time.isoformat(),
    }
    rendered = json.dumps(context, indent=2, default=str)
    return (
        "You are the coding agent for an Aithernet autonomous SDR node.\n\n"
        "Work ONLY inside the configured workspace/repository directory "
        f"({task.workspace}); do not modify files outside it. Implement the objective "
        "below using the tools actually available to you in this workspace.\n\n"
        "Rules:\n"
        "- Do not fabricate completed work. Only report changes you actually made.\n"
        "- Do not claim GNU Radio MCP access unless the task context explicitly states "
        "it is available; it is not part of your tools by default in this stage.\n"
        "- When you finish, provide a clear final summary that reports: files changed, "
        "commands run, artifacts produced, any errors encountered, and an overall "
        "summary of what was accomplished.\n\n"
        f"Task contract:\n{rendered}\n"
    )
