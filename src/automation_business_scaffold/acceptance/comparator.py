from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


JsonObject = Mapping[str, Any]

_MISSING = object()
_MISSING_REPORT_VALUE = {"__missing__": True}
_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_NUMERIC_RE = re.compile(r"^([-+]?\d+(?:\.\d+)?)([kKmM万亿])?$")
_NUMERIC_UNITS = {
    "k": 1_000,
    "K": 1_000,
    "m": 1_000_000,
    "M": 1_000_000,
    "万": 10_000,
    "亿": 100_000_000,
}

DEFAULT_REQUIRED_PROJECTION_PATHS: dict[str, tuple[str, ...]] = {
    "refresh_current_competitor_table": (
        "feishu_projection.records[*].fields.SKU-ID",
        "fact_projection.persisted_entities",
        "outbox.event_type",
    ),
    "search_keyword_competitor_products": (
        "feishu_projection.records[*].fields.SKU-ID",
        "fact_projection.persisted_entities",
        "outbox.event_type",
    ),
    "sync_tk_influencer_pool": (
        "feishu_projection.records[*].fields.达人ID",
        "fact_projection.persisted_entities",
        "outbox.event_type",
    ),
}


def _jsonable(value: Any) -> Any:
    if value is _MISSING:
        return deepcopy(_MISSING_REPORT_VALUE)
    return deepcopy(value)


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    pieces: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("[*]", index):
            pieces.append(r"\[\d+\]")
            index += 3
            continue
        char = pattern[index]
        if char == "*":
            pieces.append(".*")
        else:
            pieces.append(re.escape(char))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def _path_matches(pattern: str, path: str) -> bool:
    return bool(_pattern_to_regex(pattern).match(path))


def _looks_like_path_rule(rule: str) -> bool:
    return "." in rule or "[" in rule or "*" in rule


def _field_rule_matches(rule: str, *, key: str, path: str) -> bool:
    if not _looks_like_path_rule(rule):
        return rule == key
    return _path_matches(rule, path)


def _any_path_rule_matches(rules: Sequence[str], path: str) -> bool:
    return any(_path_matches(rule, path) for rule in rules)


def _normalize_date_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    match = _DATE_RE.search(value)
    if match is None:
        return value
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _normalize_numeric_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    text = (
        value.strip()
        .replace(",", "")
        .replace(" ", "")
        .replace("$", "")
        .replace("¥", "")
        .replace("￥", "")
    )
    match = _NUMERIC_RE.match(text)
    if match is None:
        return value

    number_text, unit = match.groups()
    number = float(number_text)
    if unit:
        number *= _NUMERIC_UNITS[unit]
    if number.is_integer():
        return int(number)
    return number


@dataclass(frozen=True, slots=True)
class JsonRefResolver:
    """Resolve fixture refs without importing the achieve golden code."""

    base_dir: Path
    artifact_values: Mapping[str, Any] | None = None

    def load_json_ref(self, ref: str) -> Any:
        if ref.startswith("artifact://"):
            if self.artifact_values is None or ref not in self.artifact_values:
                raise FileNotFoundError(f"No fixture value registered for artifact ref: {ref}")
            return deepcopy(self.artifact_values[ref])

        path = Path(ref)
        if not path.is_absolute():
            path = self.base_dir / path
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)


@dataclass(frozen=True, slots=True)
class AcceptanceArtifactWriter:
    root_dir: Path
    uri_prefix: str = "artifact://acceptance"

    def artifact_refs(self, *, workflow_code: str, scenario_id: str) -> dict[str, str]:
        prefix = f"{self.uri_prefix.rstrip('/')}/{workflow_code}/{scenario_id}"
        return {
            "payload": f"{prefix}/payload.json",
            "normalized_baseline": f"{prefix}/baseline-normalized.json",
            "normalized_candidate": f"{prefix}/candidate-normalized.json",
            "diff_report": f"{prefix}/diff-report.json",
        }

    def write(
        self,
        *,
        workflow_code: str,
        scenario_id: str,
        payload: JsonObject,
        normalized_baseline: Any,
        normalized_candidate: Any,
        diff_report: JsonObject,
    ) -> None:
        output_dir = self.root_dir / workflow_code / scenario_id
        output_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "payload.json": payload,
            "baseline-normalized.json": normalized_baseline,
            "candidate-normalized.json": normalized_candidate,
            "diff-report.json": diff_report,
        }
        for file_name, content in files.items():
            (output_dir / file_name).write_text(
                json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )


@dataclass(frozen=True, slots=True)
class _Difference:
    path: str
    baseline: Any
    candidate: Any
    severity: str
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = {
            "path": self.path,
            "baseline": _jsonable(self.baseline),
            "candidate": _jsonable(self.candidate),
            "severity": self.severity,
        }
        if self.reason:
            data["reason"] = self.reason
        return data


class _ComparatorState:
    def __init__(self) -> None:
        self.matched_count = 0
        self.diffs: list[tuple[str, Any, Any]] = []


class AchieveComparator:
    """Compare achieve baseline fixtures with runtime candidate projections."""

    def __init__(
        self,
        *,
        resolver: JsonRefResolver,
        artifact_writer: AcceptanceArtifactWriter | None = None,
    ) -> None:
        self._resolver = resolver
        self._artifact_writer = artifact_writer

    def compare_payload(self, payload: JsonObject) -> dict[str, Any]:
        workflow_code = str(payload["workflow_code"])
        scenario_id = str(payload["scenario_id"])
        baseline, candidate = self._build_compare_documents(payload)

        normalization = payload.get("normalization") or {}
        normalized_baseline = _normalize(
            baseline,
            ignore_fields=tuple(normalization.get("ignore_fields") or ()),
            date_fields=tuple(normalization.get("date_fields") or ()),
            numeric_text_fields=tuple(normalization.get("numeric_text_fields") or ()),
            unordered_list_paths=tuple(normalization.get("unordered_list_paths") or ()),
        )
        normalized_candidate = _normalize(
            candidate,
            ignore_fields=tuple(normalization.get("ignore_fields") or ()),
            date_fields=tuple(normalization.get("date_fields") or ()),
            numeric_text_fields=tuple(normalization.get("numeric_text_fields") or ()),
            unordered_list_paths=tuple(normalization.get("unordered_list_paths") or ()),
        )

        state = _ComparatorState()
        _compare_values(normalized_baseline, normalized_candidate, path="", state=state)

        allowed_differences = tuple(payload.get("allowed_differences") or ())
        decorated_diffs = [
            _decorate_difference(path, baseline_value, candidate_value, allowed_differences)
            for path, baseline_value, candidate_value in state.diffs
        ]
        required_projection_checks = _required_projection_checks(
            normalized_candidate,
            tuple(
                payload.get("required_projection_paths")
                or DEFAULT_REQUIRED_PROJECTION_PATHS.get(workflow_code, ())
            ),
        )

        unexpected_difference_count = sum(
            1 for diff in decorated_diffs if diff.severity == "unexpected"
        )
        allowed_difference_count = sum(1 for diff in decorated_diffs if diff.severity == "allowed")
        needs_review_difference_count = sum(
            1 for diff in decorated_diffs if diff.severity == "needs_review"
        )
        missing_required_count = sum(
            1 for check in required_projection_checks if check["status"] != "pass"
        )

        failure_policy = payload.get("failure_policy") or {}
        fail_on_missing_required = bool(
            failure_policy.get("fail_on_missing_required_projection", True)
        )
        if unexpected_difference_count:
            status = "fail"
        elif fail_on_missing_required and missing_required_count:
            status = "fail"
        elif needs_review_difference_count:
            status = "needs_review"
        else:
            status = "pass"

        artifact_refs: dict[str, str] = {}
        if self._artifact_writer is not None:
            artifact_refs = self._artifact_writer.artifact_refs(
                workflow_code=workflow_code,
                scenario_id=scenario_id,
            )

        report: dict[str, Any] = {
            "status": status,
            "workflow_code": workflow_code,
            "scenario_id": scenario_id,
            "summary": {
                "matched_count": state.matched_count,
                "allowed_difference_count": allowed_difference_count,
                "unexpected_difference_count": unexpected_difference_count,
                "missing_required_count": missing_required_count,
            },
            "diffs": [diff.as_dict() for diff in decorated_diffs],
            "required_projection_checks": required_projection_checks,
            "artifact_refs": artifact_refs,
        }

        if self._artifact_writer is not None:
            self._artifact_writer.write(
                workflow_code=workflow_code,
                scenario_id=scenario_id,
                payload=payload,
                normalized_baseline=normalized_baseline,
                normalized_candidate=normalized_candidate,
                diff_report=report,
            )

        return report

    def _build_compare_documents(self, payload: JsonObject) -> tuple[dict[str, Any], dict[str, Any]]:
        baseline_config = payload.get("baseline") or {}
        candidate_config = payload.get("candidate") or {}
        compare_scope = payload.get("compare_scope") or {}

        baseline_input = self._load_ref_from(baseline_config, "input_ref")
        baseline_trace = self._load_ref_from(baseline_config, "trace_ref")
        baseline_output = self._load_ref_from(baseline_config, "output_ref")
        candidate_request = deepcopy(candidate_config.get("request_payload"))
        if candidate_request is None:
            candidate_request = self._load_ref_from(candidate_config, "request_payload_ref")
        candidate_trace = self._load_ref_from(candidate_config, "runtime_trace_ref")
        candidate_fact = self._load_ref_from(candidate_config, "fact_projection_ref")
        candidate_feishu = self._load_ref_from(candidate_config, "feishu_projection_ref")
        candidate_outbox = self._load_ref_from(candidate_config, "outbox_ref")

        baseline: dict[str, Any] = {}
        candidate: dict[str, Any] = {}

        if compare_scope.get("entry_payload"):
            baseline["entry_payload"] = baseline_input
            candidate["entry_payload"] = candidate_request
        if compare_scope.get("runtime_stages"):
            baseline["runtime_stages"] = _section(baseline_trace, "runtime_stages")
            candidate["runtime_stages"] = _section(candidate_trace, "runtime_stages")
        if compare_scope.get("api_jobs"):
            baseline["api_jobs"] = _section(baseline_trace, "api_jobs")
            candidate["api_jobs"] = _section(candidate_trace, "api_jobs")
        if compare_scope.get("fact_side_effects"):
            baseline["fact_projection"] = _first_present(
                baseline_output,
                "fact_projection",
                "fact_side_effects",
            )
            candidate["fact_projection"] = candidate_fact
        if compare_scope.get("feishu_projection"):
            baseline["feishu_projection"] = _section(baseline_output, "feishu_projection")
            candidate["feishu_projection"] = candidate_feishu
        if compare_scope.get("outbox"):
            baseline["outbox"] = _section(baseline_output, "outbox")
            candidate["outbox"] = candidate_outbox

        return baseline, candidate

    def _load_ref_from(self, config: JsonObject, key: str) -> Any:
        ref = config.get(key)
        if ref is None:
            return None
        return self._resolver.load_json_ref(str(ref))


def compare_achieve_payload(
    payload: JsonObject,
    *,
    base_dir: Path,
    artifact_dir: Path | None = None,
    artifact_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_writer = (
        AcceptanceArtifactWriter(root_dir=artifact_dir) if artifact_dir is not None else None
    )
    comparator = AchieveComparator(
        resolver=JsonRefResolver(base_dir=base_dir, artifact_values=artifact_values),
        artifact_writer=artifact_writer,
    )
    return comparator.compare_payload(payload)


def _section(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return deepcopy(value.get(key))
    return None


def _first_present(value: Any, *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        if key in value:
            return deepcopy(value[key])
    return None


def _normalize(
    value: Any,
    *,
    ignore_fields: Sequence[str],
    date_fields: Sequence[str],
    numeric_text_fields: Sequence[str],
    unordered_list_paths: Sequence[str],
    path: str = "",
    key: str = "",
) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_child in value.items():
            child_key = str(raw_key)
            child_path = child_key if not path else f"{path}.{child_key}"
            if any(_field_rule_matches(rule, key=child_key, path=child_path) for rule in ignore_fields):
                continue
            normalized[child_key] = _normalize(
                raw_child,
                ignore_fields=ignore_fields,
                date_fields=date_fields,
                numeric_text_fields=numeric_text_fields,
                unordered_list_paths=unordered_list_paths,
                path=child_path,
                key=child_key,
            )
        return normalized

    if isinstance(value, list):
        normalized_items = [
            _normalize(
                item,
                ignore_fields=ignore_fields,
                date_fields=date_fields,
                numeric_text_fields=numeric_text_fields,
                unordered_list_paths=unordered_list_paths,
                path=f"{path}[{index}]",
                key=key,
            )
            for index, item in enumerate(value)
        ]
        if _any_path_rule_matches(unordered_list_paths, path):
            return sorted(
                normalized_items,
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
        return normalized_items

    normalized_value = value
    if any(_field_rule_matches(rule, key=key, path=path) for rule in date_fields):
        normalized_value = _normalize_date_text(normalized_value)
    if any(_field_rule_matches(rule, key=key, path=path) for rule in numeric_text_fields):
        normalized_value = _normalize_numeric_text(normalized_value)
    return normalized_value


def _compare_values(
    baseline: Any,
    candidate: Any,
    *,
    path: str,
    state: _ComparatorState,
) -> None:
    if isinstance(baseline, Mapping) and isinstance(candidate, Mapping):
        keys = sorted(set(baseline.keys()) | set(candidate.keys()), key=str)
        for key in keys:
            child_path = str(key) if not path else f"{path}.{key}"
            _compare_values(
                baseline.get(key, _MISSING),
                candidate.get(key, _MISSING),
                path=child_path,
                state=state,
            )
        return

    if isinstance(baseline, list) and isinstance(candidate, list):
        max_length = max(len(baseline), len(candidate))
        for index in range(max_length):
            child_path = f"{path}[{index}]"
            baseline_value = baseline[index] if index < len(baseline) else _MISSING
            candidate_value = candidate[index] if index < len(candidate) else _MISSING
            _compare_values(baseline_value, candidate_value, path=child_path, state=state)
        return

    if baseline == candidate:
        state.matched_count += 1
        return

    state.diffs.append((path or "$", baseline, candidate))


def _decorate_difference(
    path: str,
    baseline_value: Any,
    candidate_value: Any,
    allowed_differences: Sequence[JsonObject],
) -> _Difference:
    for allowed in allowed_differences:
        allowed_path = str(allowed.get("path") or "")
        if not allowed_path:
            continue
        if _path_matches(allowed_path, path):
            severity = "needs_review" if allowed.get("requires_review") else "allowed"
            if str(allowed.get("severity") or "") == "needs_review":
                severity = "needs_review"
            return _Difference(
                path=path,
                baseline=baseline_value,
                candidate=candidate_value,
                severity=severity,
                reason=str(allowed.get("reason") or ""),
            )

    return _Difference(
        path=path,
        baseline=baseline_value,
        candidate=candidate_value,
        severity="unexpected",
    )


def _required_projection_checks(candidate: Any, required_paths: Sequence[str]) -> list[dict[str, str]]:
    all_paths = tuple(_iter_paths(candidate))
    checks: list[dict[str, str]] = []
    for required_path in required_paths:
        status = "pass" if any(_path_matches(required_path, path) for path in all_paths) else "fail"
        checks.append({"path": required_path, "status": status})
    return checks


def _iter_paths(value: Any, *, path: str = "") -> list[str]:
    paths: list[str] = []
    if path:
        paths.append(path)
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = str(key) if not path else f"{path}.{key}"
            paths.extend(_iter_paths(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_iter_paths(child, path=f"{path}[{index}]"))
    return paths
