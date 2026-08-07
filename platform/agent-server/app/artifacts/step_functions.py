"""Deterministic AWS Step Functions artifact plugin."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.artifacts.base import VerifiedArtifact

_STATES_BLOCK = re.compile(r'^\s*(?:"States"|States)\s*:\s*(?:\{\s*)?$')
_OBJECT_KEY = re.compile(
    r'^\s*(?:"(?P<quoted>[^"]+)"|(?P<plain>[A-Za-z][A-Za-z0-9 _?/-]*))'
    r"\s*:\s*(?:\{\s*)?$"
)
_STRING_PROPERTY = re.compile(
    r'"?(?P<key>Type|StartAt|Next|Default)"?\s*:\s*"?(?P<value>[^",}\n]+)'
)
_END_PROPERTY = re.compile(r'"?End"?\s*:\s*true\b', re.IGNORECASE)
_TRANSITION_CONTAINER = re.compile(r'^\s*"?(?P<name>Branches|Choices|Catch)"?\s*:\s*\[')
_REQUEST_PATTERN = re.compile(
    r"\b(?:workflow|state\s+transitions?|state\s+machine|step\s+functions?)\b",
    re.IGNORECASE,
)
_EXPLICIT_STEP_FUNCTIONS = re.compile(r"\bstep\s+functions?\b", re.IGNORECASE)
_SELECTED_TRANSITION = re.compile(r'"?(?:StartAt|States|Next|Default|End|Catch)"?\s*:')


def build_transition_index(lines: list[str]) -> str:
    """Extract a compact ASL transition map from JSON-style or YAML states."""
    states_contexts: list[dict[str, int | None]] = []
    state_stack: list[tuple[int, str]] = []
    transition_containers: list[tuple[int, str]] = []
    entries: list[str] = []
    states: dict[str, dict[str, object]] = {}

    for raw_line in lines:
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip())

        while state_stack and indent <= state_stack[-1][0]:
            state_stack.pop()
        while states_contexts and indent <= int(states_contexts[-1]["indent"]):
            states_contexts.pop()
        while transition_containers and indent <= transition_containers[-1][0]:
            transition_containers.pop()

        container_match = _TRANSITION_CONTAINER.match(raw_line)
        if container_match:
            transition_containers.append((indent, container_match.group("name")))

        if _STATES_BLOCK.match(raw_line):
            states_contexts.append({"indent": indent, "child_indent": None})
            continue

        object_match = _OBJECT_KEY.match(raw_line)
        if states_contexts and object_match:
            context = states_contexts[-1]
            if context["child_indent"] is None:
                context["child_indent"] = indent
            if indent == context["child_indent"]:
                name = object_match.group("quoted") or object_match.group("plain")
                state_stack.append((indent, name))
                states.setdefault(name, {"type": "", "transitions": []})
                continue

        property_matches = list(_STRING_PROPERTY.finditer(raw_line))
        if property_matches:
            for property_match in property_matches:
                key = property_match.group("key")
                value = property_match.group("value").strip()
                if state_stack:
                    state_indent, state_name = state_stack[-1]
                    state = states[state_name]
                    if key == "Type" and indent == state_indent + 2:
                        state["type"] = value
                    elif key != "Type":
                        active_containers = [
                            name
                            for container_indent, name in transition_containers
                            if container_indent > state_indent
                        ]
                        transition_key = key
                        if key == "StartAt" and "Branches" in active_containers:
                            transition_key = "Branch StartAt"
                        elif key == "Next" and "Catch" in active_containers:
                            transition_key = "Catch Next"
                        transition = f"{transition_key} -> {value}"
                        transitions = state["transitions"]
                        if transition not in transitions:
                            transitions.append(transition)
                elif key == "StartAt" and value not in entries:
                    entries.append(value)
            continue

        if state_stack and _END_PROPERTY.search(raw_line):
            transitions = states[state_stack[-1][1]]["transitions"]
            if "End" not in transitions:
                transitions.append("End")

    if not states:
        return ""

    output = [f"- Machine StartAt -> {entry}" for entry in entries]
    for name, state in states.items():
        state_type = (
            f" [{state['type']}]" if state["type"] and state["type"] not in {"Pass", "Task"} else ""
        )
        transitions = state["transitions"]
        detail = "; ".join(transitions) if transitions else "no explicit transition"
        output.append(f"- {name}{state_type}: {detail}")
    return "\n".join(output)


class AwsStepFunctionsArtifactPlugin:
    plugin_id = "aws_sfn"
    artifact_type = "workflow_transitions"

    def extract_file(
        self,
        *,
        path: str,
        full_text: str,
        selected_text: str,
    ) -> VerifiedArtifact | None:
        if "AWS::StepFunctions::StateMachine" not in full_text:
            return None
        if not _SELECTED_TRANSITION.search(selected_text):
            return None
        content = build_transition_index(full_text.splitlines(keepends=True))
        if not content:
            return None
        return VerifiedArtifact(
            plugin_id=self.plugin_id,
            artifact_type=self.artifact_type,
            source_path=path,
            content=content,
        )

    def accepts_request(self, prompt: str, artifact: VerifiedArtifact) -> bool:
        return artifact.artifact_type == self.artifact_type and bool(
            _REQUEST_PATTERN.search(prompt)
        )

    def required_tools(
        self,
        prompt: str,
        tool_outputs: Iterable[str],
    ) -> tuple[str, ...]:
        applies = bool(_EXPLICIT_STEP_FUNCTIONS.search(prompt)) or any(
            "AWS::StepFunctions::StateMachine" in output for output in tool_outputs
        )
        return ("file_read",) if applies else ()

    def validate(self, artifact: VerifiedArtifact) -> bool:
        return artifact.artifact_type == self.artifact_type and all(
            token in artifact.content for token in ("StartAt", "Next", "End")
        )

    def render_answer(self, artifact: VerifiedArtifact) -> str:
        return f"State transitions from `{artifact.source_path}`:\n\n{artifact.content}"
