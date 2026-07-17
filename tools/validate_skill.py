#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

from render_skill import GENERATED_MARKER, SKILL_FILE, SKILLS_ROOT, SPEC_FILE, load_spec, render_skill


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "contracts" / "skill_spec.schema.json"
EXAMPLES_FILE = "examples.eval.yaml"

STANDARD_EXPECTED_SECTIONS = [
    "## Purpose",
    "## Source of truth",
    "## When to use",
    "## Do not use this skill",
    "## Required inputs",
    "## Supported workflows",
    "## Workflow",
    "## Intent precedence",
    "## Commands",
    "## Output format",
    "## Guardrails",
    "## Edge cases",
    "## Final checks",
    "## Examples",
    "## Negative activation examples",
]
COMPACT_EXPECTED_SECTIONS = [
    "## Scope",
    "## Trigger",
    "## Input",
    "## Submit",
    "## Output",
    "## Guardrails",
]
OLD_SECTIONS = [
    "## 生成说明",
    "## 触发条件",
    "## Intent 路由",
    "## 输入提取规则",
    "## 固定配置",
    "## 默认入口",
    "## 失败处理",
    "## 输出契约",
]
SENSITIVE_TOKENS = [
    "ACCESS_TOKEN",
    "PASSWORD",
    "SECRET",
    "FASTMOSS_PASSWORD",
    "MUJITASK_FEISHU_ACCESS_TOKEN",
    "TABLE_URL",
    "source skill.local.env",
    "Runtime DB 手工排障",
]
STANDALONE_BROAD_TRIGGERS = [
    "FastMoss",
    "Fastmoss",
    "TK竞品",
    "TikTok竞品",
    "写入当前飞书表",
    "更新当前表",
]
SKILL_RULES = {
    "mujitask-tiktok-feishu-sync": {
        "owner": "domains/tiktok",
        "formal_task_codes": [
            "refresh_current_competitor_table",
            "search_keyword_competitor_products",
            "sync_tk_influencer_pool",
            "tiktok_influencer_outreach_sync",
            "tiktok_fastmoss_product_ingest",
            "search_keyword_selection_products",
        ],
        "required_intents": {
            "competitor_table_refresh",
            "keyword_competitor_search",
            "influencer_pool_sync",
            "influencer_outreach_sync",
            "selection_table_ingest",
            "keyword_selection_search",
            "batch_keyword_search_submit",
            "product_url_complete",
            "competitor_row_by_url",
        },
    },
    "mujitask-amazon-feishu-sync": {
        "owner": "domains/amazon",
        "formal_task_codes": [
            "refresh_amazon_product_row_by_asin",
            "refresh_current_amazon_product_table",
        ],
        "required_intents": {
            "amazon_product_row_refresh",
            "amazon_product_table_refresh",
        },
    },
}
SPECIAL_EXAMPLE_INTENTS = {"do_not_use_skill", "ask_target_table"}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_text_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record(errors: list[str], path: Path, message: str) -> None:
    errors.append(f"{path}: {message}")


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_schema(errors: list[str], schema_path: Path) -> dict[str, Any]:
    if not schema_path.exists():
        errors.append(f"{schema_path}: missing schema")
        return {}
    try:
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact JSON parser text is not stable.
        errors.append(f"{schema_path}: invalid JSON: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(loaded, dict):
        errors.append(f"{schema_path}: schema must be a JSON object")
        return {}
    return loaded


def _json_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _validate_json_schema(
    errors: list[str],
    schema_path: Path,
    schema: dict[str, Any],
    value: Any,
    label: str,
) -> None:
    if "$ref" in schema:
        return
    if "const" in schema and value != schema["const"]:
        _record(errors, schema_path, f"{label} must be {schema['const']!r}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        _record(errors, schema_path, f"{label} must be one of {enum!r}")

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_json_type_matches(value, item) for item in expected_type):
            _record(errors, schema_path, f"{label} has wrong type")
            return
    elif isinstance(expected_type, str) and not _json_type_matches(value, expected_type):
        _record(errors, schema_path, f"{label} must be {expected_type}")
        return

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            _record(errors, schema_path, f"{label} must not be empty")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            _record(errors, schema_path, f"{label} does not match pattern {pattern}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            _record(errors, schema_path, f"{label} must contain at least {min_items} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema(errors, schema_path, item_schema, item, f"{label}[{index}]")

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    _record(errors, schema_path, f"{label}.{key} is required by schema")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, child_value in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, dict):
                    _validate_json_schema(errors, schema_path, child_schema, child_value, f"{label}.{key}")
                elif schema.get("additionalProperties") is False:
                    _record(errors, schema_path, f"{label}.{key} is not allowed by schema")


def _required_mapping(errors: list[str], path: Path, value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    _record(errors, path, f"{label} must be a mapping")
    return {}


def _required_list(errors: list[str], path: Path, value: Any, label: str) -> list[Any]:
    if isinstance(value, list) and value:
        return value
    _record(errors, path, f"{label} must be a non-empty list")
    return []


def _required_text(errors: list[str], path: Path, mapping: dict[str, Any], key: str, label: str) -> str:
    value = _text(mapping.get(key))
    if not value:
        _record(errors, path, f"{label}.{key} is required")
    return value


def _shell_script_from_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    if not parts:
        return ""
    if parts[0] in {"bash", "sh"} and len(parts) >= 2:
        return parts[1]
    return parts[0]


def _validate_command_path(errors: list[str], path: Path, command: str, skill_code: str) -> None:
    script = _shell_script_from_command(command)
    if not script:
        _record(errors, path, f"intent command is not parseable: {command!r}")
        return
    if not script.startswith(f"skills/{skill_code}/"):
        _record(errors, path, f"intent command must call skills/{skill_code}/..., got {script}")
        return
    if not (REPO_ROOT / script).is_file():
        _record(errors, path, f"intent command references missing script: {script}")


def _validate_formal_task_codes(
    errors: list[str], spec_path: Path, spec: dict[str, Any], skill_code: str
) -> None:
    formal_task_codes = _as_text_list(spec.get("formal_task_codes"))
    expected = SKILL_RULES.get(skill_code, {}).get("formal_task_codes")
    if expected is not None and formal_task_codes != expected:
        _record(
            errors,
            spec_path,
            "formal_task_codes must equal: " + ", ".join(expected),
        )


def _validate_intents(errors: list[str], spec_path: Path, spec: dict[str, Any], skill_code: str) -> None:
    formal_task_codes = set(_as_text_list(spec.get("formal_task_codes")))
    inputs = _required_mapping(errors, spec_path, spec.get("inputs"), "inputs")
    input_ids = set(inputs)
    intents = [
        _required_mapping(errors, spec_path, item, "intents[]")
        for item in _required_list(errors, spec_path, spec.get("intents"), "intents")
    ]

    seen_intents: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for intent in intents:
        intent_id = _required_text(errors, spec_path, intent, "id", "intent")
        if intent_id in seen_intents:
            _record(errors, spec_path, f"duplicate intent id: {intent_id}")
        seen_intents.add(intent_id)
        by_id[intent_id] = intent

        if intent_id == "keyword_search":
            _record(errors, spec_path, "generic intent id=keyword_search is not allowed")

        kind = _required_text(errors, spec_path, intent, "kind", f"intent {intent_id}")
        _required_text(errors, spec_path, intent, "title", f"intent {intent_id}")
        _required_text(errors, spec_path, intent, "description", f"intent {intent_id}")
        command = _required_text(errors, spec_path, intent, "command", f"intent {intent_id}")
        _required_list(errors, spec_path, intent.get("target_tables"), f"intent {intent_id}.target_tables")
        _required_list(errors, spec_path, intent.get("side_effects"), f"intent {intent_id}.side_effects")
        for input_id in _as_text_list(intent.get("required_inputs")) + _as_text_list(intent.get("optional_inputs")):
            if input_id not in input_ids:
                _record(errors, spec_path, f"intent {intent_id} references unknown input {input_id}")
        if command and skill_code:
            _validate_command_path(errors, spec_path, command, skill_code)

        if kind == "formal_workflow":
            task_code = _required_text(errors, spec_path, intent, "task_code", f"intent {intent_id}")
            if task_code and task_code not in formal_task_codes:
                _record(errors, spec_path, f"formal_workflow {intent_id}.task_code must be listed in formal_task_codes")
            source_documents = _required_mapping(
                errors,
                spec_path,
                intent.get("source_documents"),
                f"intent {intent_id}.source_documents",
            )
            _required_list(
                errors,
                spec_path,
                source_documents.get("requirements"),
                f"intent {intent_id}.source_documents.requirements",
            )
            _required_list(
                errors,
                spec_path,
                source_documents.get("design"),
                f"intent {intent_id}.source_documents.design",
            )
            _required_text(
                errors,
                spec_path,
                intent,
                "trigger_mode_from_requirements",
                f"intent {intent_id}",
            )
        elif kind == "operational_sub_intent":
            _required_text(errors, spec_path, intent, "parent_task_code", f"intent {intent_id}")
            _required_text(errors, spec_path, intent, "mode", f"intent {intent_id}")
        else:
            _record(errors, spec_path, f"intent {intent_id}.kind must be formal_workflow or operational_sub_intent")

    required_intents = set(SKILL_RULES.get(skill_code, {}).get("required_intents", set()))
    missing_intents = sorted(required_intents - seen_intents)
    if missing_intents:
        _record(errors, spec_path, "missing required intents: " + ", ".join(missing_intents))
    unexpected_intents = sorted(seen_intents - required_intents) if required_intents else []
    if unexpected_intents:
        _record(errors, spec_path, "unexpected intents for skill: " + ", ".join(unexpected_intents))

    if skill_code != "mujitask-tiktok-feishu-sync":
        return

    if "keyword_competitor_search" not in seen_intents or "keyword_selection_search" not in seen_intents:
        _record(errors, spec_path, "keyword_competitor_search and keyword_selection_search must both exist")

    product_url_complete = by_id.get("product_url_complete", {})
    if _text(product_url_complete.get("parent_task_code")) != "tiktok_fastmoss_product_ingest":
        _record(errors, spec_path, "product_url_complete parent_task_code must be tiktok_fastmoss_product_ingest")
    competitor_row_by_url = by_id.get("competitor_row_by_url", {})
    if _text(competitor_row_by_url.get("parent_task_code")) != "refresh_current_competitor_table":
        _record(errors, spec_path, "competitor_row_by_url parent_task_code must be refresh_current_competitor_table")

    keyword_selection = by_id.get("keyword_selection_search", {})
    selection_defaults = keyword_selection.get("default_values") if isinstance(keyword_selection, dict) else {}
    if isinstance(selection_defaults, dict):
        if selection_defaults.get("sales_7d_threshold") != 500:
            _record(errors, spec_path, "keyword_selection_search default sales_7d_threshold must be 500")
        if selection_defaults.get("price_range_max_threshold") != 10.99:
            _record(errors, spec_path, "keyword_selection_search default price_range_max_threshold must be 10.99")
    keyword_competitor = by_id.get("keyword_competitor_search", {})
    competitor_defaults = keyword_competitor.get("default_values") if isinstance(keyword_competitor, dict) else {}
    if isinstance(competitor_defaults, dict) and competitor_defaults.get("max_candidates") != 20:
        _record(errors, spec_path, "keyword_competitor_search default max_candidates must be 20")

    input_sales_defaults = _required_mapping(
        errors,
        spec_path,
        _required_mapping(errors, spec_path, inputs.get("sales_7d_threshold"), "inputs.sales_7d_threshold").get("defaults_by_intent"),
        "inputs.sales_7d_threshold.defaults_by_intent",
    )
    if input_sales_defaults.get("keyword_competitor_search") != 200:
        _record(errors, spec_path, "inputs.sales_7d_threshold keyword_competitor_search default must be 200")
    if input_sales_defaults.get("keyword_selection_search") != 500:
        _record(errors, spec_path, "inputs.sales_7d_threshold keyword_selection_search default must be 500")
    price_defaults = _required_mapping(
        errors,
        spec_path,
        _required_mapping(errors, spec_path, inputs.get("price_range_max_threshold"), "inputs.price_range_max_threshold").get("defaults_by_intent"),
        "inputs.price_range_max_threshold.defaults_by_intent",
    )
    if price_defaults.get("keyword_selection_search") != 10.99:
        _record(errors, spec_path, "inputs.price_range_max_threshold keyword_selection_search default must be 10.99")
    max_candidate_defaults = _required_mapping(
        errors,
        spec_path,
        _required_mapping(errors, spec_path, inputs.get("max_candidates"), "inputs.max_candidates").get("defaults_by_intent"),
        "inputs.max_candidates.defaults_by_intent",
    )
    if max_candidate_defaults.get("keyword_competitor_search") != 20:
        _record(errors, spec_path, "inputs.max_candidates keyword_competitor_search default must be 20")


def _validate_spec_shape(errors: list[str], spec_path: Path, spec: dict[str, Any]) -> None:
    if spec.get("schema_version") != 1:
        _record(errors, spec_path, "schema_version must be 1")
    if spec.get("kind") != "skill":
        _record(errors, spec_path, "kind must be skill")

    metadata = _required_mapping(errors, spec_path, spec.get("metadata"), "metadata")
    skill_code = _required_text(errors, spec_path, metadata, "name", "metadata")
    _required_text(errors, spec_path, metadata, "title", "metadata")
    _required_text(errors, spec_path, metadata, "description", "metadata")
    _required_text(errors, spec_path, metadata, "short_description", "metadata")
    owner = _required_text(errors, spec_path, metadata, "owner", "metadata")
    if not isinstance(metadata.get("side_effects"), bool):
        _record(errors, spec_path, "metadata.side_effects must be boolean")
    if skill_code and spec_path.parent.name != skill_code:
        _record(errors, spec_path, f"metadata.name must match directory name {spec_path.parent.name}")
    expected_owner = SKILL_RULES.get(skill_code, {}).get("owner")
    if expected_owner is not None and owner != expected_owner:
        _record(errors, spec_path, f"metadata.owner must equal {expected_owner}")

    source = _required_mapping(errors, spec_path, spec.get("source_of_truth"), "source_of_truth")
    _required_text(errors, spec_path, source, "business_overview", "source_of_truth")
    _required_text(errors, spec_path, source, "requirements_index", "source_of_truth")
    _validate_formal_task_codes(errors, spec_path, spec, skill_code)
    _validate_intents(errors, spec_path, spec, skill_code)

    for key in (
        "purpose",
        "when_to_use",
        "execution_actions",
        "do_not_use",
        "workflow",
        "intent_precedence",
        "guardrails",
        "edge_cases",
        "final_checks",
        "examples",
        "negative_activation_examples",
    ):
        _required_list(errors, spec_path, spec.get(key), key)

    output_format = _required_mapping(errors, spec_path, spec.get("output_format"), "output_format")
    if _text(output_format.get("success")) != "request_id: <request_id>":
        _record(errors, spec_path, "output_format.success must be request_id: <request_id>")
    if not _text(output_format.get("failure")).startswith("任务提交失败："):
        _record(errors, spec_path, "output_format.failure must start with 任务提交失败：")


def _validate_examples(errors: list[str], skill_dir: Path, spec: dict[str, Any]) -> None:
    examples_path = skill_dir / EXAMPLES_FILE
    if not examples_path.exists():
        _record(errors, examples_path, "missing examples eval file")
        return
    try:
        loaded = _load_yaml(examples_path)
    except Exception as exc:  # pragma: no cover - exact YAML parser text is not stable.
        _record(errors, examples_path, f"failed to parse YAML: {type(exc).__name__}: {exc}")
        return
    if not isinstance(loaded, dict):
        _record(errors, examples_path, "top-level YAML must be a mapping")
        return

    intents = {
        _text(intent.get("id")): _text(intent.get("command"))
        for intent in _as_list(spec.get("intents"))
        if isinstance(intent, dict)
    }
    cases = _required_list(errors, examples_path, loaded.get("cases"), "cases")
    covered: set[str] = set()
    metadata = spec.get("metadata")
    skill_name = _text(metadata.get("name")) if isinstance(metadata, dict) else ""
    is_tiktok_skill = skill_name == "mujitask-tiktok-feishu-sync"
    has_keyword_selection_positive = False
    has_ambiguous_fastmoss_negative = False
    for index, item in enumerate(cases, start=1):
        case = _required_mapping(errors, examples_path, item, f"cases[{index}]")
        user = _required_text(errors, examples_path, case, "user", f"cases[{index}]")
        expected_intent = _required_text(
            errors, examples_path, case, "expected_intent", f"cases[{index}]"
        )
        if expected_intent in intents:
            expected_command = _required_text(
                errors, examples_path, case, "expected_command", f"cases[{index}]"
            )
            if expected_command != intents[expected_intent]:
                _record(
                    errors,
                    examples_path,
                    f"case {index} expected_command does not match spec intent {expected_intent}",
                )
            covered.add(expected_intent)
        elif expected_intent not in SPECIAL_EXAMPLE_INTENTS:
            _record(errors, examples_path, f"case {index} references unknown intent {expected_intent}")
        if not user:
            _record(errors, examples_path, f"case {index} user text is empty")
        if expected_intent == "keyword_selection_search":
            has_keyword_selection_positive = True
        if expected_intent == "ask_target_table" and "FastMoss" in user and "写入飞书" in user:
            has_ambiguous_fastmoss_negative = True

    missing_coverage = sorted(intent_id for intent_id in intents if intent_id and intent_id not in covered)
    if missing_coverage:
        _record(errors, examples_path, "missing eval coverage for intents: " + ", ".join(missing_coverage))
    if is_tiktok_skill and not has_keyword_selection_positive:
        _record(errors, examples_path, "missing keyword_selection_search positive eval case")
    if is_tiktok_skill and not has_ambiguous_fastmoss_negative:
        _record(errors, examples_path, "missing ambiguous FastMoss target-table negative eval case")


def _validate_rendered_sections(
    errors: list[str], skill_path: Path, current: str, *, render_mode: str
) -> None:
    expected_sections = (
        COMPACT_EXPECTED_SECTIONS if render_mode == "compact" else STANDARD_EXPECTED_SECTIONS
    )
    positions: list[int] = []
    for section in expected_sections:
        position = current.find(section)
        if position < 0:
            _record(errors, skill_path, f"missing generated section {section}")
        positions.append(position)
    ordered_positions = [position for position in positions if position >= 0]
    if ordered_positions != sorted(ordered_positions):
        _record(errors, skill_path, "generated sections are not in required order")
    for section in OLD_SECTIONS:
        if section in current:
            _record(errors, skill_path, f"old section is not allowed: {section}")
    if render_mode == "compact":
        for section in STANDARD_EXPECTED_SECTIONS:
            if section not in COMPACT_EXPECTED_SECTIONS and section in current:
                _record(errors, skill_path, f"compact skill contains verbose section {section}")


def _validate_rendered_sensitive_content(errors: list[str], skill_path: Path, current: str) -> None:
    for token in SENSITIVE_TOKENS:
        if token in current:
            _record(errors, skill_path, f"sensitive or implementation token is not allowed: {token}")
    for trigger in STANDALONE_BROAD_TRIGGERS:
        patterns = [
            rf"(?m)^\s*-\s*`{re.escape(trigger)}`\s*$",
            rf"(?m)^User:\s*{re.escape(trigger)}\s*$",
            rf"(?m)^\s*{re.escape(trigger)}\s*$",
        ]
        if any(re.search(pattern, current) for pattern in patterns):
            _record(errors, skill_path, f"standalone broad trigger is not allowed: {trigger}")


def _validate_rendered_skill(errors: list[str], skill_dir: Path, spec: dict[str, Any]) -> None:
    skill_path = skill_dir / SKILL_FILE
    if not skill_path.exists():
        _record(errors, skill_path, "missing generated SKILL.md")
        return
    current = skill_path.read_text(encoding="utf-8")
    if GENERATED_MARKER not in current:
        _record(errors, skill_path, "missing generated marker")
    rendered = render_skill(spec)
    if current != rendered:
        _record(errors, skill_path, "generated output is stale; run tools/render_skill.py")
    metadata = spec.get("metadata")
    render_mode = _text(metadata.get("render_mode")) if isinstance(metadata, dict) else ""
    _validate_rendered_sections(
        errors,
        skill_path,
        current,
        render_mode=render_mode or "standard",
    )
    _validate_rendered_sensitive_content(errors, skill_path, current)


def _skill_dirs(target: Path | None) -> list[Path]:
    if target is not None:
        resolved = target.resolve()
        return [resolved if resolved.is_dir() else resolved.parent]
    if not SKILLS_ROOT.exists():
        return []
    dirs = {path.parent for path in SKILLS_ROOT.glob(f"*/{SPEC_FILE}")}
    dirs.update(path.parent for path in SKILLS_ROOT.glob(f"*/{SKILL_FILE}"))
    return sorted(dirs)


def validate(skill_dir: Path | None = None, *, schema_path: Path = DEFAULT_SCHEMA_PATH) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    passed: list[str] = []
    schema = _load_schema(errors, schema_path)

    dirs = _skill_dirs(skill_dir)
    if not dirs:
        errors.append("No skill directories found.")
        return passed, errors

    for directory in dirs:
        spec_path = directory / SPEC_FILE
        skill_path = directory / SKILL_FILE
        if skill_path.exists() and not spec_path.exists():
            _record(errors, skill_path, f"SKILL.md must be generated from {SPEC_FILE}")
            continue
        if not spec_path.exists():
            _record(errors, spec_path, "missing skill spec")
            continue
        try:
            spec = load_spec(spec_path)
        except Exception as exc:
            _record(errors, spec_path, str(exc))
            continue
        if schema:
            _validate_json_schema(errors, schema_path, schema, spec, "skill_spec")
        _validate_spec_shape(errors, spec_path, spec)
        _validate_examples(errors, directory, spec)
        _validate_rendered_skill(errors, directory, spec)
        passed.append(str(directory.relative_to(REPO_ROOT)))

    return passed, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Mujitask skill specs and generated SKILL.md files.")
    parser.add_argument("skill_dir", nargs="?", type=Path, help="Validate one skill directory. Defaults to all skills.")
    parser.add_argument("--skill-dir", dest="skill_dir_option", type=Path, help="Validate one skill directory.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Skill spec schema path.")
    parser.add_argument("--json", action="store_true", help="Emit JSON result.")
    args = parser.parse_args(argv)

    target = args.skill_dir_option or args.skill_dir
    passed, errors = validate(target, schema_path=args.schema)
    if args.json:
        print(json.dumps({"passed": passed, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        for item in passed:
            print(f"validated: {item}")
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
