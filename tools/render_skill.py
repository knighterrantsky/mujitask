#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
SPEC_FILE = "skill.spec.yaml"
SKILL_FILE = "SKILL.md"
GENERATED_MARKER = "<!-- GENERATED FROM skill.spec.yaml. DO NOT EDIT SKILL.md BY HAND. -->"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_text_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _required_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    return value


def _required_text(mapping: dict[str, Any], key: str, label: str) -> str:
    value = str(mapping.get(key, "")).strip()
    if not value:
        raise ValueError(f"{label}.{key} is required")
    return value


def _frontmatter_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _folded_block(value: str, *, indent: int = 2, width: int = 88) -> str:
    prefix = " " * indent
    wrapped = textwrap.wrap(
        value.strip(),
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not wrapped:
        return f"{prefix}\n"
    return "".join(f"{prefix}{line}\n" for line in wrapped)


def _bullet(items: list[str], *, indent: int = 0) -> list[str]:
    prefix = " " * indent
    return [f"{prefix}- {item}" for item in items]


def _dict_items(mapping: dict[str, Any]) -> list[str]:
    return [f"`{key}`: `{value}`" for key, value in mapping.items()]


def _command_block(command: str) -> list[str]:
    return ["```bash", command, "```"]


def load_spec(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact YAML parser text is not stable.
        raise ValueError(f"{path}: failed to parse YAML: {type(exc).__name__}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return loaded


def find_specs(skill_dir: Path | None = None) -> list[Path]:
    if skill_dir is not None:
        path = skill_dir / SPEC_FILE if skill_dir.is_dir() else skill_dir
        return [path] if path.exists() else []
    if not SKILLS_ROOT.exists():
        return []
    return sorted(SKILLS_ROOT.glob(f"*/{SPEC_FILE}"))


def _render_source_of_truth(lines: list[str], spec: dict[str, Any], intents: list[dict[str, Any]]) -> None:
    source = _required_mapping(spec.get("source_of_truth"), "source_of_truth")
    lines.append("## Source of truth")
    lines.append("")
    lines.append("Business overview:")
    lines.append("")
    lines.append(f"- `{_required_text(source, 'business_overview', 'source_of_truth')}`")
    lines.append(f"- `{_required_text(source, 'requirements_index', 'source_of_truth')}`")
    lines.append("")
    lines.append("Formal workflow requirements:")
    lines.append("")
    for intent in intents:
        if intent.get("kind") != "formal_workflow":
            continue
        task_code = _required_text(intent, "task_code", "intent")
        docs = _as_text_list(_required_mapping(intent.get("source_documents"), "source_documents").get("requirements"))
        doc_text = ", ".join(f"`{item}`" for item in docs)
        lines.append(f"- `{task_code}` -> {doc_text}")
    lines.append("")
    lines.append("Design documents:")
    lines.append("")
    for intent in intents:
        if intent.get("kind") != "formal_workflow":
            continue
        task_code = _required_text(intent, "task_code", "intent")
        docs = _as_text_list(_required_mapping(intent.get("source_documents"), "source_documents").get("design"))
        doc_text = ", ".join(f"`{item}`" for item in docs)
        lines.append(f"- `{task_code}` -> {doc_text}")
    lines.append("")
    lines.append(
        "Do not copy detailed business rules, Runtime internals, credentials, table IDs, "
        "browser profiles, or troubleshooting runbooks into this skill. Use this skill as "
        "the routing and task-submission layer."
    )
    lines.append("")


def _render_required_inputs(lines: list[str], inputs: dict[str, Any]) -> None:
    lines.append("## Required inputs")
    lines.append("")
    for input_id, raw_input in inputs.items():
        item = _required_mapping(raw_input, f"inputs.{input_id}")
        lines.append(f"### `{input_id}`")
        lines.append("")
        lines.append(_required_text(item, "description", f"inputs.{input_id}"))
        lines.append("")
        usage = str(item.get("usage") or "").strip()
        if usage:
            lines.append(usage)
            lines.append("")
        defaults = item.get("defaults_by_intent")
        if isinstance(defaults, dict) and defaults:
            lines.append("Defaults:")
            lines.append("")
            lines.extend(_bullet(_dict_items(defaults)))
            lines.append("")
        extraction_rules = _as_text_list(item.get("extraction_rules"))
        if extraction_rules:
            lines.append("Rules:")
            lines.append("")
            lines.extend(_bullet(extraction_rules))
            lines.append("")
        extraction_examples = _as_text_list(item.get("extraction_examples"))
        if extraction_examples:
            lines.append("Extraction examples:")
            lines.append("")
            lines.extend(_bullet(extraction_examples))
            lines.append("")


def _render_supported_workflows(lines: list[str], intents: list[dict[str, Any]]) -> None:
    lines.append("## Supported workflows")
    lines.append("")
    for intent in intents:
        intent_id = _required_text(intent, "id", "intent")
        lines.append(f"### `{intent_id}`")
        lines.append("")
        lines.append(f"- Kind: {intent.get('kind')}")
        if intent.get("task_code"):
            lines.append(f"- Task code: `{intent['task_code']}`")
        if intent.get("parent_task_code"):
            lines.append(f"- Parent task code: `{intent['parent_task_code']}`")
        if intent.get("mode"):
            lines.append(f"- Mode: {intent['mode']}")
        if intent.get("source_tables"):
            lines.append(f"- Source table: {', '.join(f'`{item}`' for item in _as_text_list(intent.get('source_tables')))}")
        if intent.get("target_tables"):
            lines.append(f"- Target table: {', '.join(f'`{item}`' for item in _as_text_list(intent.get('target_tables')))}")
        if intent.get("trigger_mode_from_requirements"):
            lines.append(f"- Trigger mode from requirements: {intent['trigger_mode_from_requirements']}")
        if intent.get("conversation_activation"):
            lines.append(f"- Conversation activation: {intent['conversation_activation']}")
        lines.append("")
        lines.append(_required_text(intent, "description", "intent"))
        lines.append("")
        do_not_use = str(intent.get("do_not_use") or "").strip()
        if do_not_use:
            lines.append(do_not_use)
            lines.append("")
        behavior_summary = _as_text_list(intent.get("behavior_summary"))
        if behavior_summary:
            lines.append("Business behavior summary:")
            lines.append("")
            lines.extend(_bullet(behavior_summary))
            lines.append("")
        default_values = intent.get("default_values")
        if isinstance(default_values, dict) and default_values:
            lines.append("Default inputs:")
            lines.append("")
            lines.extend(_bullet(_dict_items(default_values)))
            lines.append("")


def _render_commands(lines: list[str], intents: list[dict[str, Any]]) -> None:
    lines.append("## Commands")
    lines.append("")
    lines.append("Prefer the dispatcher command below.")
    lines.append("")
    for intent in intents:
        intent_id = _required_text(intent, "id", "intent")
        command = _required_text(intent, "command", "intent")
        lines.append(f"### `{intent_id}`")
        lines.append("")
        lines.extend(_command_block(command))
        lines.append("")


def _render_output_format(lines: list[str], output_format: dict[str, Any]) -> None:
    lines.append("## Output format")
    lines.append("")
    lines.append("Successful task submission must reply exactly:")
    lines.append("")
    lines.append("```text")
    lines.append(_required_text(output_format, "success", "output_format"))
    lines.append("```")
    lines.append("")
    lines.append("Failed task submission must reply exactly:")
    lines.append("")
    lines.append("```text")
    lines.append(_required_text(output_format, "failure", "output_format"))
    lines.append("```")
    lines.append("")
    missing_examples = _as_text_list(output_format.get("missing_input_examples"))
    if missing_examples:
        lines.append("Missing input may ask only for the missing field.")
        lines.append("")
        lines.append("Examples:")
        lines.append("")
        for example in missing_examples:
            lines.append("```text")
            lines.append(example)
            lines.append("```")
            lines.append("")


def _render_examples(lines: list[str], examples: list[Any]) -> None:
    lines.append("## Examples")
    lines.append("")
    for raw_example in examples:
        example = _required_mapping(raw_example, "examples[]")
        lines.append(f"User: {_required_text(example, 'user', 'examples[]')}")
        lines.append(f"Intent: `{_required_text(example, 'intent', 'examples[]')}`")
        inputs = example.get("inputs")
        if isinstance(inputs, dict) and inputs:
            lines.append("Inputs:")
            lines.append("")
            lines.extend(_bullet(_dict_items(inputs)))
            lines.append("")
        lines.append("Reply:")
        lines.append("")
        lines.append("```text")
        lines.append(_required_text(example, "reply", "examples[]"))
        lines.append("```")
        lines.append("")


def _render_negative_activation_examples(lines: list[str], examples: list[Any]) -> None:
    lines.append("## Negative activation examples")
    lines.append("")
    for raw_example in examples:
        example = _required_mapping(raw_example, "negative_activation_examples[]")
        lines.append(f"User: {_required_text(example, 'user', 'negative_activation_examples[]')}")
        lines.append(f"Reason: {_required_text(example, 'reason', 'negative_activation_examples[]')}")
        lines.append("")


def render_skill(spec: dict[str, Any]) -> str:
    metadata = _required_mapping(spec.get("metadata"), "metadata")
    intents = [_required_mapping(item, "intents[]") for item in _required_list(spec.get("intents"), "intents")]
    inputs = _required_mapping(spec.get("inputs"), "inputs")
    output_format = _required_mapping(spec.get("output_format"), "output_format")

    name = _required_text(metadata, "name", "metadata")
    title = _required_text(metadata, "title", "metadata")
    description = _required_text(metadata, "description", "metadata")
    short_description = _required_text(metadata, "short_description", "metadata")

    lines: list[str] = []
    lines.append("---")
    lines.append(f"name: {_frontmatter_value(name)}")
    lines.append("description: >-")
    lines.append(_folded_block(description).rstrip("\n"))
    lines.append("metadata:")
    lines.append(f"  short-description: {_frontmatter_value(short_description)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(GENERATED_MARKER)
    lines.append("")
    lines.append("## Purpose")
    lines.append("")
    lines.extend(_bullet(_as_text_list(spec.get("purpose"))))
    lines.append("")

    _render_source_of_truth(lines, spec, intents)

    lines.append("## When to use")
    lines.append("")
    lines.append("Use this skill only when the user explicitly asks to submit one of these workflows:")
    lines.append("")
    lines.extend(_bullet(_as_text_list(spec.get("when_to_use"))))
    lines.append("")
    lines.append("The user must express an execution action such as:")
    lines.append("")
    lines.extend(_bullet([f"`{item}`" for item in _as_text_list(spec.get("execution_actions"))]))
    lines.append("")

    lines.append("## Do not use this skill")
    lines.append("")
    lines.append("Do not use this skill when the user is only:")
    lines.append("")
    lines.extend(_bullet(_as_text_list(spec.get("do_not_use"))))
    lines.append("")

    _render_required_inputs(lines, inputs)
    _render_supported_workflows(lines, intents)

    lines.append("## Workflow")
    lines.append("")
    for index, item in enumerate(_as_text_list(spec.get("workflow")), start=1):
        lines.append(f"{index}. {item}")
    lines.append("")

    lines.append("## Intent precedence")
    lines.append("")
    for index, item in enumerate(_as_text_list(spec.get("intent_precedence")), start=1):
        lines.append(f"{index}. {item}")
    lines.append("")

    _render_commands(lines, intents)
    _render_output_format(lines, output_format)

    lines.append("## Guardrails")
    lines.append("")
    lines.extend(_bullet(_as_text_list(spec.get("guardrails"))))
    lines.append("")

    lines.append("## Edge cases")
    lines.append("")
    lines.extend(_bullet(_as_text_list(spec.get("edge_cases"))))
    lines.append("")

    lines.append("## Final checks")
    lines.append("")
    lines.append("Before replying, verify:")
    lines.append("")
    lines.extend(_bullet(_as_text_list(spec.get("final_checks"))))
    lines.append("")

    _render_examples(lines, _required_list(spec.get("examples"), "examples"))
    _render_negative_activation_examples(
        lines,
        _required_list(spec.get("negative_activation_examples"), "negative_activation_examples"),
    )

    return "\n".join(lines).rstrip() + "\n"


def render_path(spec_path: Path) -> str:
    return render_skill(load_spec(spec_path))


def _write_or_check(spec_path: Path, *, check: bool, out: Path | None = None) -> tuple[bool, str]:
    skill_path = out or spec_path.with_name(SKILL_FILE)
    rendered = render_path(spec_path)
    if check:
        if not skill_path.exists():
            return False, f"{skill_path}: missing generated SKILL.md"
        current = skill_path.read_text(encoding="utf-8")
        if current != rendered:
            return False, f"{skill_path}: out of date; run `uv run --extra dev python tools/render_skill.py`"
        return True, f"{skill_path}: up to date"
    skill_path.write_text(rendered, encoding="utf-8")
    return True, f"{skill_path}: rendered"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render generated SKILL.md files from skill.spec.yaml.")
    parser.add_argument("spec", nargs="?", type=Path, help="Render one explicit skill.spec.yaml path.")
    parser.add_argument("--out", type=Path, help="Output path when rendering one explicit spec.")
    parser.add_argument("--skill-dir", type=Path, help="Render a single skill directory. Defaults to all skills.")
    parser.add_argument("--check", action="store_true", help="Check generated files without writing.")
    args = parser.parse_args(argv)

    if args.out and not args.spec:
        print("--out requires an explicit spec path.", file=sys.stderr)
        return 1

    specs = [args.spec] if args.spec else find_specs(args.skill_dir)
    if not specs:
        print("No skill.spec.yaml files found.", file=sys.stderr)
        return 1

    failed = False
    for spec_path in specs:
        try:
            ok, message = _write_or_check(spec_path, check=args.check, out=args.out)
        except Exception as exc:
            ok = False
            message = f"{spec_path}: {type(exc).__name__}: {exc}"
        print(message)
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
