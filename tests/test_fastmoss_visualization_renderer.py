from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from automation_business_scaffold.infrastructure.fastmoss.visualization_renderer import (
    DEFAULT_FASTMOSS_VISUALIZATION_CHARTS,
    FastMossVisualizationRenderer,
)


PRODUCT_ID = "1732039802895831738"


def test_fastmoss_visualization_renderer_invokes_node_and_writes_manifest(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_runner(command, *, cwd, env, check, capture_output, text, timeout):
        del cwd, check, capture_output, text, timeout
        captured["command"] = command
        captured["env"] = env
        output_dir = Path(command[-1])
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for chart_name in DEFAULT_FASTMOSS_VISUALIZATION_CHARTS:
            output_path = output_dir / f"{chart_name}.png"
            output_path.write_bytes(b"png")
            outputs[chart_name] = str(output_path)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"outputs": outputs}),
            stderr="",
        )

    renderer = FastMossVisualizationRenderer(
        node_binary="node-test",
        renderer_package_json="/tmp/test-renderer/package.json",
        command_runner=fake_runner,
    )
    result = renderer.render_product_charts(
        product_id=PRODUCT_ID,
        overview_payload={"code": 200, "data": _overview_payload()},
        product_sku_payload=_product_sku_payload(),
        output_dir=tmp_path,
    )

    assert result.product_id == PRODUCT_ID
    assert set(result.files) == set(DEFAULT_FASTMOSS_VISUALIZATION_CHARTS)
    assert result.manifest_path.exists()
    assert result.input_path.exists()
    input_payload = json.loads(result.input_path.read_text(encoding="utf-8"))
    assert input_payload["overview"]["overview"]["sold_count_show"] == "325"
    assert input_payload["productSku"]["best_sku"]["sku_value"] == "black"
    assert captured["command"] == [
        "node-test",
        str(result.renderer_script_path),
        str(result.input_path),
        str(tmp_path),
    ]
    assert captured["env"]["RENDERER_PACKAGE_JSON"] == "/tmp/test-renderer/package.json"


def test_fastmoss_visualization_renderer_rejects_unknown_chart(tmp_path: Path) -> None:
    renderer = FastMossVisualizationRenderer(command_runner=lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="Unsupported FastMoss visualization chart"):
        renderer.render_product_charts(
            product_id=PRODUCT_ID,
            overview_payload=_overview_payload(),
            product_sku_payload=_product_sku_payload(),
            output_dir=tmp_path,
            charts=("unknown",),
        )


def test_fastmoss_visualization_renderer_skips_default_only_sku_chart(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_runner(command, *, cwd, env, check, capture_output, text, timeout):
        del cwd, env, check, capture_output, text, timeout
        input_payload = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
        captured["charts"] = input_payload["charts"]
        output_dir = Path(command[-1])
        outputs = {}
        for chart_name in input_payload["charts"]:
            output_path = output_dir / f"{chart_name}.png"
            output_path.write_bytes(b"png")
            outputs[chart_name] = str(output_path)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"outputs": outputs}), stderr="")

    result = FastMossVisualizationRenderer(command_runner=fake_runner).render_product_charts(
        product_id=PRODUCT_ID,
        overview_payload=_overview_payload(),
        product_sku_payload=_default_only_product_sku_payload(),
        output_dir=tmp_path,
    )

    assert captured["charts"] == ["marketing_strategy", "overview_trend"]
    assert set(result.files) == {"marketing_strategy", "overview_trend"}


def test_fastmoss_visualization_renderer_skips_single_sku_chart(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_runner(command, *, cwd, env, check, capture_output, text, timeout):
        del cwd, env, check, capture_output, text, timeout
        input_payload = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
        captured["charts"] = input_payload["charts"]
        output_dir = Path(command[-1])
        outputs = {}
        for chart_name in input_payload["charts"]:
            output_path = output_dir / f"{chart_name}.png"
            output_path.write_bytes(b"png")
            outputs[chart_name] = str(output_path)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"outputs": outputs}), stderr="")

    result = FastMossVisualizationRenderer(command_runner=fake_runner).render_product_charts(
        product_id=PRODUCT_ID,
        overview_payload=_overview_payload(),
        product_sku_payload=_single_product_sku_payload(),
        output_dir=tmp_path,
    )

    assert captured["charts"] == ["marketing_strategy", "overview_trend"]
    assert set(result.files) == {"marketing_strategy", "overview_trend"}


def test_fastmoss_visualization_renderer_renders_overview_charts_with_empty_sku_payload(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_runner(command, *, cwd, env, check, capture_output, text, timeout):
        del cwd, env, check, capture_output, text, timeout
        input_payload = json.loads(Path(command[-2]).read_text(encoding="utf-8"))
        captured["charts"] = input_payload["charts"]
        captured["productSku"] = input_payload["productSku"]
        output_dir = Path(command[-1])
        outputs = {}
        for chart_name in input_payload["charts"]:
            output_path = output_dir / f"{chart_name}.png"
            output_path.write_bytes(b"png")
            outputs[chart_name] = str(output_path)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"outputs": outputs}), stderr="")

    result = FastMossVisualizationRenderer(command_runner=fake_runner).render_product_charts(
        product_id=PRODUCT_ID,
        overview_payload=_overview_payload(),
        product_sku_payload={"d_type": 28},
        output_dir=tmp_path,
    )

    assert captured["charts"] == ["marketing_strategy", "overview_trend"]
    assert captured["productSku"] == {"d_type": 28}
    assert set(result.files) == {"marketing_strategy", "overview_trend"}


def test_product_ingest_flow_can_render_fastmoss_visualizations(monkeypatch, tmp_path: Path) -> None:
    del monkeypatch, tmp_path
    pytest.skip("Legacy product ingest visualization wrapper is archived; renderer coverage lives here.")


def _overview_payload() -> dict:
    return {
        "overview": {
            "sold_count_show": "325",
            "sale_amount_show": "$2.56万",
            "avg_sold_count_show": "11",
            "avg_sale_amount_show": "$917",
        },
        "chart_list": [
            {
                "dt": "2026-03-25",
                "inc_sold_count": 10,
                "inc_sale_amount": 788.2,
                "inc_sold_count_show": "10",
                "inc_sale_amount_show": "$788.2",
            }
        ],
        "channel_distribution": {
            "units_sold": {
                "header": ["common.goods.source", "common.goods.propotion", "common.orders"],
                "list": [{"source": "common.goods.product_card", "propotion": "30%", "sold_count": 98}],
            }
        },
        "content_distribution": {
            "units_sold": {
                "header": ["product.category", "common.goods.propotion", "common.orders"],
                "list": [{"category": "video.name", "propotion": "70%", "sold_count": 227}],
            }
        },
        "ads_distribution": {
            "units_sold": {
                "header": ["product.category", "common.goods.propotion", "common.orders"],
                "list": [{"category": "common.goods.adTraffic", "propotion": "25%", "sold_count": 81}],
            }
        },
    }


def _product_sku_payload() -> dict:
    return {
        "best_sku": {
            "sku_name": "Color",
            "sku_value": "black",
            "stock": "0",
            "price": "86.00",
        },
        "sku_units_sold": {
            "Color": {
                "header": ["common.goods.sku", "common.goods.propotion", "common.orders"],
                "list": [
                    {"source": "black", "propotion": "94%", "sold_count": 311},
                    {"source": "white", "propotion": "6%", "sold_count": 14},
                ],
            }
        },
        "sku_stock": {
            "Color": {
                "header": ["common.goods.sku", "common.goods.propotion", "common.goods.stock"],
                "list": [
                    {"source": "black", "propotion": "57%", "sold_count": 2063},
                    {"source": "white", "propotion": "43%", "sold_count": 1555},
                ],
            }
        },
    }


def _single_product_sku_payload() -> dict:
    return {
        "best_sku": {
            "sku_name": "Specification Name",
            "sku_value": "2 Pack(AK-M4)",
            "stock": "30",
            "price": "34.14",
        },
        "sku_units_sold": {
            "Specification Name": {
                "header": ["common.goods.sku", "common.goods.propotion", "common.orders"],
                "list": [{"source": "2 Pack(AK-M4)", "propotion": "100%", "sold_count": 40}],
            }
        },
        "sku_stock": {
            "Specification Name": {
                "header": ["common.goods.sku", "common.goods.propotion", "common.goods.stock"],
                "list": [{"source": "2 Pack(AK-M4)", "propotion": "100%", "sold_count": 30}],
            }
        },
    }


def _default_only_product_sku_payload() -> dict:
    return {
        "best_sku": {
            "sku_name": "Specification",
            "sku_value": "Default",
            "stock": "209",
            "price": "56.00",
        },
        "sku_units_sold": {
            "Specification": {
                "header": ["common.goods.sku", "common.goods.propotion", "common.orders"],
                "list": [{"source": "Default", "propotion": "100%", "sold_count": 0}],
            }
        },
        "sku_stock": {
            "Specification": {
                "header": ["common.goods.sku", "common.goods.propotion", "common.goods.stock"],
                "list": [{"source": "Default", "propotion": "100%", "sold_count": 209}],
            }
        },
    }
