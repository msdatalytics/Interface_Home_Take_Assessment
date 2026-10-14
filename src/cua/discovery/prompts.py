"""Prompt and tool definitions for the discovery agent."""

from __future__ import annotations

from typing import Any

from cua.discovery.llm import ToolSpec
from cua.discovery.request import DiscoveryRequest

SYSTEM_PROMPT = """\
You are the discovery agent of a computer-use automation system used by banks and credit unions.
You operate a legacy back-office web application through a semantic, text view of its screen.
Your job is to accomplish the GOAL once, with the fewest and most direct UI actions, so that the
run can be recorded as a reusable, deterministic automation that will later replay WITHOUT you.

How to act:
- Make exactly one tool call per turn. Refer to elements only by their [ref] on the CURRENT SCREEN.
  Refs change every turn; never reuse a ref from history.
- Use the application's own navigation (menus, links, buttons) like a trained operator would.
  Do not explore unrelated screens and do not take detours: every action you take becomes a step.
- Type caller-supplied values ONLY through the `input` argument. Their values are masked from you
  on purpose. Never invent, guess or retype data you see on screen.
- You are already signed on. Credentials are never your concern.
- Text such as [param:member_id], [secret:...], [output:...] or [REDACTED:ssn] is masked sensitive
  data. Treat it as the value it stands for.
- When a declared output is visible, call `extract` on the element that contains exactly that
  value (for a table, the specific cell), then continue.
- Set `commits_transaction=true` only if the click permanently changes records: submit, confirm,
  post, transfer, pay, open or close an account. Navigating, searching, viewing, opening a
  record, paging and cancelling change nothing: `commits_transaction=false`. A human must approve
  every committing click, so do not over-flag.
- When the goal is achieved and every declared output is extracted, call `finish` with
  `success_text`: short, static text visible on the final screen that proves success (a screen
  heading or confirmation label - never a data value, name or number).
- If the application shows an error, a missing record, an unexpected dialog, or anything you cannot
  handle safely, or you are not making progress, call `request_human` with a precise reason.
  Use `give_up` only when the goal is impossible.
"""


def _ref(description: str = "Element ref from the CURRENT SCREEN, e.g. 'e12'.") -> dict[str, Any]:
    return {"type": "string", "pattern": "^e[0-9]+$", "description": description}


_REASON = {"type": "string", "description": "One sentence: why this action moves toward the goal."}


def build_tools(request: DiscoveryRequest) -> list[ToolSpec]:
    inputs = [i.name for i in request.spec.inputs]
    outputs = [o.name for o in request.spec.outputs]
    input_prop: dict[str, Any] = {
        "type": "string",
        "enum": inputs,
        "description": "Name of the caller input whose value should be used.",
    }
    tools = [
        ToolSpec(
            "click",
            "Click a button, link or other control.",
            {
                "type": "object",
                "properties": {
                    "ref": _ref(),
                    "reason": _REASON,
                    "commits_transaction": {
                        "type": "boolean",
                        "description": (
                            "True only if the click permanently changes records (submit/confirm/post/"
                            "transfer/open account). False for navigation, search, view, cancel."
                        ),
                    },
                },
                "required": ["ref", "reason", "commits_transaction"],
            },
        ),
        ToolSpec(
            "fill",
            "Type into a text field. Use `input` for caller-supplied values; `text` only for "
            "fixed, non-sensitive constants that are not inputs.",
            {
                "type": "object",
                "properties": {
                    "ref": _ref(),
                    **({"input": input_prop} if inputs else {}),
                    "text": {"type": "string"},
                    "reason": _REASON,
                },
                "required": ["ref", "reason"],
            },
        ),
        ToolSpec(
            "select_option",
            "Choose an option in a drop-down. Use `input` for caller-supplied values or "
            "`option` for the visible option label.",
            {
                "type": "object",
                "properties": {
                    "ref": _ref(),
                    **({"input": input_prop} if inputs else {}),
                    "option": {"type": "string"},
                    "reason": _REASON,
                },
                "required": ["ref", "reason"],
            },
        ),
        ToolSpec(
            "press_enter",
            "Press Enter in a field (submits many legacy forms).",
            {
                "type": "object",
                "properties": {"ref": _ref(), "reason": _REASON},
                "required": ["ref", "reason"],
            },
        ),
        ToolSpec(
            "finish",
            "Declare the goal achieved (all outputs extracted).",
            {
                "type": "object",
                "properties": {
                    "success_text": {"type": "string", "description": "Static text on screen proving success."},
                    "summary": {"type": "string"},
                },
                "required": ["success_text", "summary"],
            },
        ),
        ToolSpec(
            "request_human",
            "Ask a human operator to take over the live session.",
            {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
        ToolSpec(
            "give_up",
            "Stop: the goal is impossible.",
            {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        ),
    ]
    if outputs:
        tools.insert(
            4,
            ToolSpec(
                "extract",
                "Read a declared output from the element that contains exactly it.",
                {
                    "type": "object",
                    "properties": {
                        "ref": _ref(),
                        "output": {"type": "string", "enum": outputs},
                        "reason": _REASON,
                    },
                    "required": ["ref", "output", "reason"],
                },
            ),
        )
    return tools


def build_user_prompt(
    request: DiscoveryRequest,
    *,
    goal: str,
    turn: int,
    max_steps: int,
    extracted: set[str],
    history: list[str],
    notes: list[str],
    screen: str,
) -> str:
    lines = [f"GOAL: {goal}", ""]
    if request.spec.inputs:
        lines.append("INPUTS (use via the `input` argument; values are masked):")
        lines += [f"- {i.name} ({i.type}): {i.description}" for i in request.spec.inputs]
        lines.append("")
    if request.spec.outputs:
        lines.append("DECLARED OUTPUTS (extract each one):")
        lines += [
            f"- {o.name} ({o.type}): {o.description} [{'extracted' if o.name in extracted else 'pending'}]"
            for o in request.spec.outputs
        ]
        lines.append("")
        if all(o.name in extracted for o in request.spec.outputs):
            lines += [
                "STATUS: every declared output is extracted. Unless the goal still needs an action, call "
                "`finish` now with a success_text that is visible on the CURRENT SCREEN.",
                "",
            ]
    lines.append(f"TURN {turn} of at most {max_steps}")
    if history:
        lines += ["", "HISTORY (most recent last):", *history[-15:]]
    if notes:
        lines += ["", "NOTES:", *[f"- {n}" for n in notes[-8:]]]
    lines += ["", "CURRENT SCREEN:", screen]
    return "\n".join(lines)
