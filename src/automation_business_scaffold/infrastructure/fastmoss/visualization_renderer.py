from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_FASTMOSS_VISUALIZATION_CHARTS = (
    "marketing_strategy",
    "overview_trend",
    "sku_analysis",
)
FASTMOSS_VISUALIZATION_RENDERER_NODE_PACKAGES = (
    "echarts",
    "sharp",
)
DEFAULT_FASTMOSS_VISUALIZATION_RENDERER_PACKAGE_JSON = ""


class FastMossVisualizationRenderError(RuntimeError):
    """Raised when FastMoss visualization rendering fails."""


@dataclass(slots=True)
class FastMossVisualizationRenderResult:
    product_id: str
    output_dir: Path
    files: dict[str, Path]
    manifest_path: Path
    input_path: Path
    renderer_script_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "output_dir": str(self.output_dir),
            "files": {key: str(value) for key, value in self.files.items()},
            "manifest_path": str(self.manifest_path),
            "input_path": str(self.input_path),
            "renderer_script_path": str(self.renderer_script_path),
        }


class FastMossVisualizationRenderer:
    """Render FastMoss product analysis charts from FastMoss API payloads.

    The Python layer owns the stable application interface. The bundled Node
    renderer owns ECharts + Sharp rendering so the workflow does not need a
    browser process just to produce PNG chart attachments.
    """

    def __init__(
        self,
        *,
        node_binary: str | None = None,
        renderer_script_path: str | os.PathLike[str] | None = None,
        renderer_package_json: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 60.0,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.node_binary = str(node_binary or os.environ.get("NODE_BINARY") or "node")
        self.renderer_script_path = Path(
            renderer_script_path or Path(__file__).with_name("fastmoss_visualization_renderer.mjs")
        )
        self.renderer_package_json = _first_non_empty(
            renderer_package_json,
            os.environ.get("FASTMOSS_VISUALIZATION_RENDERER_PACKAGE_JSON"),
            os.environ.get("RENDERER_PACKAGE_JSON"),
            _default_renderer_package_json(),
        )
        self.timeout_seconds = float(timeout_seconds)
        self._command_runner = command_runner or subprocess.run

    def validate_runtime_dependencies(self) -> dict[str, Any]:
        """Validate the Node.js renderer and its project-level dependencies."""
        if not self.renderer_script_path.exists():
            raise FastMossVisualizationRenderError(
                f"FastMoss visualization renderer script was not found: {self.renderer_script_path}"
            )
        if not self.renderer_package_json:
            raise FastMossVisualizationRenderError(
                "FastMoss visualization renderer package.json was not found. "
                "Run npm install in the project root before starting writeback workers."
            )
        package_json_path = Path(self.renderer_package_json)
        if not package_json_path.exists():
            raise FastMossVisualizationRenderError(
                "FastMoss visualization renderer package.json was not found: "
                f"{package_json_path}"
            )

        env = dict(os.environ)
        env["RENDERER_PACKAGE_JSON"] = str(package_json_path)
        command = [
            self.node_binary,
            "-e",
            _NODE_DEPENDENCY_CHECK_SCRIPT,
            json.dumps(FASTMOSS_VISUALIZATION_RENDERER_NODE_PACKAGES),
        ]
        try:
            completed = self._command_runner(
                command,
                cwd=str(package_json_path.parent),
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=min(self.timeout_seconds, 20.0),
            )
        except FileNotFoundError as exc:
            raise FastMossVisualizationRenderError(
                f"Node binary was not found for FastMoss visualization rendering: {self.node_binary}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FastMossVisualizationRenderError(
                "FastMoss visualization renderer dependency check timed out."
            ) from exc
        if completed.returncode != 0:
            raise FastMossVisualizationRenderError(
                "FastMoss visualization renderer dependencies are not installed: "
                f"{_trim_process_output(completed.stderr) or _trim_process_output(completed.stdout)}"
            )
        return {
            "node_binary": self.node_binary,
            "renderer_script_path": str(self.renderer_script_path),
            "renderer_package_json": str(package_json_path),
            "dependencies": _safe_json_stdout(completed.stdout),
        }

    def render_product_charts(
        self,
        *,
        product_id: str,
        overview_payload: Mapping[str, Any],
        product_sku_payload: Mapping[str, Any],
        output_dir: str | os.PathLike[str] | None = None,
        charts: Sequence[str] = DEFAULT_FASTMOSS_VISUALIZATION_CHARTS,
    ) -> FastMossVisualizationRenderResult:
        normalized_product_id = str(product_id or "").strip()
        if not normalized_product_id:
            raise ValueError("product_id is required")
        if not self.renderer_script_path.exists():
            raise FastMossVisualizationRenderError(
                f"FastMoss visualization renderer script was not found: {self.renderer_script_path}"
            )

        normalized_overview = _normalize_payload(
            overview_payload,
            expected_keys=(
                "overview",
                "chart_list",
                "channel_distribution",
                "content_distribution",
                "ads_distribution",
            ),
            payload_name="overview_payload",
        )
        normalized_product_sku = _normalize_payload(
            product_sku_payload,
            expected_keys=(
                "sku_units_sold",
                "sku_gmv",
                "sku_stock",
                "sku_detail",
                "best_sku",
                "sku_list",
            ),
            payload_name="product_sku_payload",
            allow_empty=True,
        )

        resolved_output_dir = Path(
            output_dir
            or Path("runtime")
            / "visualization_exports"
            / normalized_product_id
            / "png_fastmoss_page_logic"
        )
        resolved_output_dir.mkdir(parents=True, exist_ok=True)
        input_path = resolved_output_dir / "fastmoss_visualization_input.json"
        manifest_path = resolved_output_dir / "manifest.json"
        selected_charts = _normalize_chart_names(charts)
        if "sku_analysis" in selected_charts and not _has_meaningful_sku_analysis(normalized_product_sku):
            selected_charts = [chart_name for chart_name in selected_charts if chart_name != "sku_analysis"]
        if not selected_charts:
            raise ValueError("At least one meaningful FastMoss visualization chart is required")

        input_payload = {
            "product_id": normalized_product_id,
            "overview": normalized_overview,
            "productSku": normalized_product_sku,
            "charts": selected_charts,
        }
        input_path.write_text(json.dumps(input_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        completed = self._run_renderer(input_path=input_path, output_dir=resolved_output_dir)
        if completed.returncode != 0:
            raise FastMossVisualizationRenderError(
                "FastMoss visualization renderer failed: "
                f"{_trim_process_output(completed.stderr) or _trim_process_output(completed.stdout)}"
            )

        files = {
            chart_name: resolved_output_dir / f"{chart_name}.png"
            for chart_name in selected_charts
        }
        missing = [str(path) for path in files.values() if not path.exists() or path.stat().st_size <= 0]
        if missing:
            raise FastMossVisualizationRenderError(
                "FastMoss visualization renderer did not create expected PNG files: "
                + ", ".join(missing)
            )

        result = FastMossVisualizationRenderResult(
            product_id=normalized_product_id,
            output_dir=resolved_output_dir,
            files=files,
            manifest_path=manifest_path,
            input_path=input_path,
            renderer_script_path=self.renderer_script_path,
        )
        manifest_path.write_text(
            json.dumps(
                {
                    **result.to_dict(),
                    "charts": selected_charts,
                    "renderer_stdout": _safe_json_stdout(completed.stdout),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return result

    def _run_renderer(
        self,
        *,
        input_path: Path,
        output_dir: Path,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if self.renderer_package_json:
            env["RENDERER_PACKAGE_JSON"] = str(self.renderer_package_json)
        command = [
            self.node_binary,
            str(self.renderer_script_path),
            str(input_path),
            str(output_dir),
        ]
        try:
            return self._command_runner(
                command,
                cwd=str(Path.cwd()),
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise FastMossVisualizationRenderError(
                f"Node binary was not found for FastMoss visualization rendering: {self.node_binary}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FastMossVisualizationRenderError(
                f"FastMoss visualization rendering timed out after {self.timeout_seconds:g}s"
            ) from exc


def _normalize_payload(
    payload: Mapping[str, Any],
    *,
    expected_keys: Sequence[str],
    payload_name: str,
    allow_empty: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{payload_name} must be a mapping")
    value = dict(payload)
    data = value.get("data")
    if not any(key in value for key in expected_keys) and isinstance(data, Mapping):
        value = dict(data)
    if not any(key in value for key in expected_keys):
        if allow_empty:
            return value
        raise ValueError(f"{payload_name} does not look like a FastMoss product payload")
    return value


def _normalize_chart_names(charts: Sequence[str]) -> list[str]:
    allowed = set(DEFAULT_FASTMOSS_VISUALIZATION_CHARTS)
    normalized: list[str] = []
    for chart in charts:
        chart_name = str(chart or "").strip()
        if not chart_name:
            continue
        if chart_name not in allowed:
            raise ValueError(f"Unsupported FastMoss visualization chart: {chart_name}")
        if chart_name not in normalized:
            normalized.append(chart_name)
    if not normalized:
        raise ValueError("At least one FastMoss visualization chart is required")
    return normalized


def _has_meaningful_sku_analysis(product_sku_payload: Mapping[str, Any]) -> bool:
    sku_units_sold = _as_mapping(product_sku_payload.get("sku_units_sold"))
    sku_stock = _as_mapping(product_sku_payload.get("sku_stock"))
    best_sku = _as_mapping(product_sku_payload.get("best_sku"))
    if not sku_units_sold and not sku_stock:
        return False

    values: set[str] = set()
    for table in (sku_units_sold, sku_stock):
        for _prop_key, payload in table.items():
            data = _as_mapping(payload)
            rows = data.get("list")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                row_value = _normalize_sku_text(
                    row.get("source")
                    or row.get("sku")
                    or row.get("sku_value")
                    or row.get("name")
                )
                if row_value:
                    values.add(row_value)

    best_value = _normalize_sku_text(best_sku.get("sku_value"))
    if best_value:
        values.add(best_value)

    meaningful_values = {value for value in values if value not in {"default", "默认", "specification"}}
    return len(meaningful_values) > 1


def _normalize_sku_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _default_renderer_package_json() -> str:
    start = Path(__file__).resolve()
    for directory in (start.parent, *start.parents):
        candidate = directory / "package.json"
        if _package_json_declares_renderer_dependencies(candidate):
            return str(candidate)
    return DEFAULT_FASTMOSS_VISUALIZATION_RENDERER_PACKAGE_JSON


def _package_json_declares_renderer_dependencies(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    dependencies = _as_mapping(payload.get("dependencies"))
    return all(package_name in dependencies for package_name in FASTMOSS_VISUALIZATION_RENDERER_NODE_PACKAGES)


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _trim_process_output(value: str | None, *, limit: int = 2000) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _safe_json_stdout(stdout: str | None) -> dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"text": _trim_process_output(text)}
    return payload if isinstance(payload, dict) else {"value": payload}


_NODE_DEPENDENCY_CHECK_SCRIPT = r"""
const { createRequire } = require("node:module");
const packageJson = process.env.RENDERER_PACKAGE_JSON;
const requiredPackages = JSON.parse(process.argv[1] || "[]");
const requireFromProject = createRequire(packageJson);
const versions = {};
for (const packageName of requiredPackages) {
  versions[packageName] = requireFromProject(`${packageName}/package.json`).version;
  requireFromProject(packageName);
}
console.log(JSON.stringify({ ok: true, versions }));
"""
