from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

from PIL import Image

from automation_business_scaffold.contracts.handler.shared import coerce_mapping, compact_dict, first_non_empty

FASTMOSS_SLIDER_CANONICAL_TARGET_WIDTH = 120.0
FASTMOSS_SLIDER_CANONICAL_BODY_RIGHT_X = 100.0
FASTMOSS_SLIDER_BODY_RIGHT_RATIO = (
    FASTMOSS_SLIDER_CANONICAL_BODY_RIGHT_X / FASTMOSS_SLIDER_CANONICAL_TARGET_WIDTH
)


def _build_fastmoss_mixed_slider_mapping(
    page: Any,
    *,
    slider_result: Any,
    background_box: Mapping[str, float],
    background_image_size: tuple[int, int],
    piece_image_size: tuple[int, int] = (0, 0),
    piece_box: Mapping[str, float],
    handle_box: Mapping[str, float],
    drag_scale: float,
    drag_offset_x: float,
) -> dict[str, Any]:
    image_width = float(background_image_size[0] or 1)
    rendered_width = float(background_box.get("width") or 0.0)
    raw_target_x = float(getattr(slider_result, "target_x", 0.0) or 0.0)
    raw_target_y = float(getattr(slider_result, "target_y", 0.0) or 0.0)
    raw_payload = coerce_mapping(getattr(slider_result, "raw", None))
    shape_anchor = coerce_mapping(raw_payload.get("fastmoss_shape_anchor"))
    shape_piece_anchor_ratio_x = _optional_float(shape_anchor.get("piece_anchor_ratio_x"))
    target_width_raw = float(piece_image_size[0] or 0.0)
    if target_width_raw <= 0:
        target_width_raw = FASTMOSS_SLIDER_CANONICAL_TARGET_WIDTH
    matched_left_raw = raw_target_x - (target_width_raw / 2.0)
    body_right_offset_raw = target_width_raw * FASTMOSS_SLIDER_BODY_RIGHT_RATIO
    target_anchor_x_raw = matched_left_raw + body_right_offset_raw
    target_anchor_x_display = (target_anchor_x_raw / max(image_width, 1.0)) * rendered_width
    current_piece_center_x = (
        float(piece_box.get("x") or 0.0)
        + (float(piece_box.get("width") or 0.0) / 2)
        - float(background_box.get("x") or 0.0)
    )
    foreground_left_display = float(piece_box.get("x") or 0.0) - float(background_box.get("x") or 0.0)
    current_piece_anchor_x = foreground_left_display + (
        float(piece_box.get("width") or 0.0) * FASTMOSS_SLIDER_BODY_RIGHT_RATIO
    )
    unscaled_drag_distance = target_anchor_x_display - current_piece_anchor_x
    drag_distance = (unscaled_drag_distance * drag_scale) + drag_offset_x
    handle_start_x = float(handle_box.get("x") or 0.0) + (float(handle_box.get("width") or 0.0) / 2)
    handle_start_y = float(handle_box.get("y") or 0.0) + (float(handle_box.get("height") or 0.0) / 2)
    return {
        "raw_target_x": raw_target_x,
        "raw_target_y": raw_target_y,
        "raw_target_box": _raw_target_box(first_non_empty(raw_payload.get("target_box"), raw_payload.get("target"))),
        "source_target_interpretation": first_non_empty(
            shape_anchor.get("target_interpretation"),
            "ddddocr_target_center",
        ),
        "target_interpretation": "target_body_right_anchor_minus_piece_body_right_anchor",
        "background_image_width": background_image_size[0],
        "background_image_height": background_image_size[1],
        "target_image_width": int(piece_image_size[0] or 0),
        "target_image_height": int(piece_image_size[1] or 0),
        "target_width_raw": target_width_raw,
        "matched_left_raw": matched_left_raw,
        "body_right_offset_raw": body_right_offset_raw,
        "body_right_ratio": FASTMOSS_SLIDER_BODY_RIGHT_RATIO,
        "target_anchor_x_raw": target_anchor_x_raw,
        "target_anchor_x_display": target_anchor_x_display,
        "background_box": dict(background_box),
        "piece_box": dict(piece_box),
        "handle_box": dict(handle_box),
        "css_target_x": target_anchor_x_display,
        "current_piece_center_x": current_piece_center_x,
        "foreground_left_display": foreground_left_display,
        "start_anchor_x_display": current_piece_anchor_x,
        "current_piece_anchor_x": current_piece_anchor_x,
        "piece_anchor_ratio_x": FASTMOSS_SLIDER_BODY_RIGHT_RATIO,
        "shape_piece_anchor_ratio_x": shape_piece_anchor_ratio_x,
        "fastmoss_shape_anchor": shape_anchor,
        "unscaled_drag_distance": unscaled_drag_distance,
        "drag_scale": drag_scale,
        "drag_offset_x": drag_offset_x,
        "drag_distance": drag_distance,
        "handle_start_x": handle_start_x,
        "handle_start_y": handle_start_y,
        "handle_end_x": handle_start_x + drag_distance,
        "handle_end_y": handle_start_y,
        "device_pixel_ratio": _device_pixel_ratio(page),
    }


def _select_fastmoss_shape_anchor_slider_result(
    slider_result: Any,
    *,
    background_image: bytes,
    piece_image: bytes,
    background_box: Mapping[str, float],
    piece_box: Mapping[str, float],
) -> Any:
    """Use FastMoss puzzle outline geometry to correct low-confidence OCR points."""
    background_width, background_height = _image_size(background_image)
    if background_width <= 0 or background_height <= 0:
        return slider_result

    rendered_width = float(background_box.get("width") or 0.0)
    if rendered_width <= 0:
        return slider_result

    raw_target_x = float(getattr(slider_result, "target_x", 0.0) or 0.0)
    raw_target_y = float(getattr(slider_result, "target_y", 0.0) or 0.0)
    current_piece_center_x = (
        float(piece_box.get("x") or 0.0)
        + (float(piece_box.get("width") or 0.0) / 2)
        - float(background_box.get("x") or 0.0)
    )
    current_piece_center_raw_x = current_piece_center_x * background_width / rendered_width
    piece_outline_box = _fastmoss_piece_outline_box(piece_image)
    piece_anchor_ratio_x = _fastmoss_box_center_ratio(piece_outline_box, image_width=_image_size(piece_image)[0])
    candidates = _fastmoss_background_puzzle_outline_candidates(
        background_image,
        current_piece_center_raw_x=current_piece_center_raw_x,
        piece_outline_box=piece_outline_box,
    )
    if not candidates:
        return slider_result

    selected = _select_fastmoss_outline_candidate(
        candidates,
        raw_target_x=raw_target_x,
        raw_target_y=raw_target_y,
        current_piece_center_raw_x=current_piece_center_raw_x,
    )
    if not selected:
        return slider_result

    target_x = int(round(float(selected["anchor_x"])))
    target_y = int(round(float(selected["anchor_y"])))
    raw_payload = dict(coerce_mapping(getattr(slider_result, "raw", None)))
    shape_anchor = {
        "enabled": True,
        "source_target_x": raw_target_x,
        "source_target_y": raw_target_y,
        "source_confidence": getattr(slider_result, "confidence", None),
        "selected_box": _compact_box(selected),
        "candidate_boxes": [_compact_box(candidate) for candidate in candidates[:8]],
        "piece_outline_box": _compact_box(piece_outline_box),
        "piece_anchor_ratio_x": piece_anchor_ratio_x,
        "current_piece_center_raw_x": current_piece_center_raw_x,
        "target_interpretation": "fastmoss_outline_bbox_center_minus_piece_outline_anchor",
        "selection_reason": first_non_empty(selected.get("selection_reason"), "nearest_outline_bbox_to_ocr_target"),
    }
    return SimpleNamespace(
        target_x=target_x,
        target_y=target_y,
        confidence=getattr(slider_result, "confidence", None),
        raw={
            **raw_payload,
            "target_box": [
                int(round(float(selected["x"]))),
                int(round(float(selected["y"]))),
                int(round(float(selected["x"]) + float(selected["width"]))),
                int(round(float(selected["y"]) + float(selected["height"]))),
            ],
            "fastmoss_shape_anchor": shape_anchor,
        },
    )


def _fastmoss_piece_outline_box(piece_image: bytes) -> dict[str, float]:
    image = _open_rgb_image(piece_image)
    if image is None:
        return {}
    candidates = _light_outline_components(image, min_area=12)
    if not candidates:
        return {"x": 0.0, "y": 0.0, "width": float(image.width), "height": float(image.height)}
    merged = _merge_outline_boxes(candidates, max_gap=8.0)
    plausible = [
        box
        for box in merged
        if box["width"] >= image.width * 0.25 and box["height"] >= image.height * 0.25
    ]
    if not plausible:
        plausible = merged
    return max(plausible, key=lambda box: float(box.get("area", 0.0) or 0.0))


def _fastmoss_background_puzzle_outline_candidates(
    background_image: bytes,
    *,
    current_piece_center_raw_x: float,
    piece_outline_box: Mapping[str, Any],
) -> list[dict[str, float | str]]:
    image = _open_rgb_image(background_image)
    if image is None:
        return []
    piece_width = float(piece_outline_box.get("width") or 0.0)
    piece_height = float(piece_outline_box.get("height") or 0.0)
    min_width = max(24.0, piece_width * 0.45)
    max_width = max(150.0, piece_width * 2.8)
    min_height = max(22.0, piece_height * 0.35)
    max_height = max(130.0, piece_height * 2.6)
    raw_components = [
        component
        for component in _light_outline_components(image, min_area=35)
        if float(component.get("width") or 0.0) <= max_width
        and float(component.get("height") or 0.0) <= max_height
    ]
    merged = _merge_outline_boxes(raw_components, max_gap=34.0)
    candidates: list[dict[str, float | str]] = []
    for box in merged:
        width = float(box.get("width") or 0.0)
        height = float(box.get("height") or 0.0)
        if not (min_width <= width <= max_width and min_height <= height <= max_height):
            continue
        if (
            float(box.get("x") or 0.0) <= 2.0
            or float(box.get("y") or 0.0) <= 2.0
            or float(box.get("x") or 0.0) + width >= float(image.width) - 2.0
            or float(box.get("y") or 0.0) + height >= float(image.height) - 2.0
        ):
            continue
        center_x = float(box["x"]) + width / 2.0
        center_y = float(box["y"]) + height / 2.0
        is_current = abs(center_x - current_piece_center_raw_x) <= max(width * 0.8, piece_width * 0.7, 28.0)
        score = float(box.get("area", 0.0) or 0.0) / max(width * height, 1.0)
        candidates.append(
            {
                **box,
                "anchor_x": center_x,
                "anchor_y": center_y,
                "center_x": center_x,
                "center_y": center_y,
                "score": score,
                "is_current_piece": bool(is_current),
            }
        )
    candidates.sort(key=lambda box: (bool(box.get("is_current_piece")), -float(box.get("score") or 0.0)))
    return candidates


def _select_fastmoss_outline_candidate(
    candidates: list[dict[str, Any]],
    *,
    raw_target_x: float,
    raw_target_y: float,
    current_piece_center_raw_x: float,
) -> dict[str, Any]:
    usable = [candidate for candidate in candidates if not candidate.get("is_current_piece")]
    if not usable:
        usable = candidates
    if not usable:
        return {}

    def distance(candidate: Mapping[str, Any]) -> float:
        center_x = float(candidate.get("center_x") or candidate.get("anchor_x") or 0.0)
        center_y = float(candidate.get("center_y") or candidate.get("anchor_y") or 0.0)
        return ((center_x - raw_target_x) ** 2 + ((center_y - raw_target_y) * 0.8) ** 2) ** 0.5

    nearest = min(usable, key=distance)
    nearest = dict(nearest)
    nearest_distance = distance(nearest)
    if nearest_distance <= max(float(nearest.get("width") or 0.0) * 1.2, 72.0):
        nearest["selection_reason"] = "nearest_outline_bbox_to_ocr_target"
        return nearest

    source_looks_like_current_piece = abs(raw_target_x - current_piece_center_raw_x) <= 96.0
    right_side = [
        candidate
        for candidate in usable
        if float(candidate.get("center_x") or 0.0) > current_piece_center_raw_x + max(float(candidate.get("width") or 0.0) * 0.4, 28.0)
    ]
    if source_looks_like_current_piece and right_side:
        selected = max(right_side, key=lambda candidate: float(candidate.get("score") or 0.0))
        selected = dict(selected)
        selected["selection_reason"] = "best_non_current_outline_bbox"
        return selected

    return {}


def _light_outline_components(image: Image.Image, *, min_area: int) -> list[dict[str, float]]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    visited = bytearray(width * height)
    components: list[dict[str, float]] = []

    def is_outline_pixel(x: int, y: int) -> bool:
        red, green, blue = pixels[x, y]
        luminance = (int(red) + int(green) + int(blue)) / 3.0
        saturation = max(red, green, blue) - min(red, green, blue)
        return luminance >= 165.0 and saturation <= 95

    for y in range(height):
        for x in range(width):
            index = (y * width) + x
            if visited[index] or not is_outline_pixel(x, y):
                continue
            stack = [(x, y)]
            visited[index] = 1
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while stack:
                current_x, current_y = stack.pop()
                area += 1
                min_x = min(min_x, current_x)
                max_x = max(max_x, current_x)
                min_y = min(min_y, current_y)
                max_y = max(max_y, current_y)
                for next_x, next_y in (
                    (current_x + 1, current_y),
                    (current_x - 1, current_y),
                    (current_x, current_y + 1),
                    (current_x, current_y - 1),
                ):
                    if not (0 <= next_x < width and 0 <= next_y < height):
                        continue
                    next_index = (next_y * width) + next_x
                    if visited[next_index] or not is_outline_pixel(next_x, next_y):
                        continue
                    visited[next_index] = 1
                    stack.append((next_x, next_y))
            box_width = max_x - min_x + 1
            box_height = max_y - min_y + 1
            if area >= min_area and box_width >= 6 and box_height >= 6:
                components.append(
                    {
                        "x": float(min_x),
                        "y": float(min_y),
                        "width": float(box_width),
                        "height": float(box_height),
                        "area": float(area),
                    }
                )
    return components


def _merge_outline_boxes(boxes: list[dict[str, float]], *, max_gap: float) -> list[dict[str, float]]:
    merged = [dict(box) for box in boxes]
    changed = True
    while changed:
        changed = False
        next_boxes: list[dict[str, float]] = []
        used = [False] * len(merged)
        for index, box in enumerate(merged):
            if used[index]:
                continue
            current = dict(box)
            used[index] = True
            for other_index in range(index + 1, len(merged)):
                if used[other_index]:
                    continue
                other = merged[other_index]
                if not _outline_boxes_should_merge(current, other, max_gap=max_gap):
                    continue
                current = _union_outline_boxes(current, other)
                used[other_index] = True
                changed = True
            next_boxes.append(current)
        merged = next_boxes
    return merged


def _outline_boxes_should_merge(
    first: Mapping[str, float],
    second: Mapping[str, float],
    *,
    max_gap: float,
) -> bool:
    first_x1 = float(first["x"])
    first_y1 = float(first["y"])
    first_x2 = first_x1 + float(first["width"])
    first_y2 = first_y1 + float(first["height"])
    second_x1 = float(second["x"])
    second_y1 = float(second["y"])
    second_x2 = second_x1 + float(second["width"])
    second_y2 = second_y1 + float(second["height"])
    horizontal_overlap = max(0.0, min(first_x2, second_x2) - max(first_x1, second_x1))
    vertical_overlap = max(0.0, min(first_y2, second_y2) - max(first_y1, second_y1))
    min_width = max(min(float(first["width"]), float(second["width"])), 1.0)
    min_height = max(min(float(first["height"]), float(second["height"])), 1.0)
    horizontal_gap = max(0.0, max(first_x1, second_x1) - min(first_x2, second_x2))
    vertical_gap = max(0.0, max(first_y1, second_y1) - min(first_y2, second_y2))
    return (
        (horizontal_overlap / min_width >= 0.35 and vertical_gap <= max_gap)
        or (vertical_overlap / min_height >= 0.35 and horizontal_gap <= max_gap)
    )


def _union_outline_boxes(first: Mapping[str, float], second: Mapping[str, float]) -> dict[str, float]:
    x1 = min(float(first["x"]), float(second["x"]))
    y1 = min(float(first["y"]), float(second["y"]))
    x2 = max(float(first["x"]) + float(first["width"]), float(second["x"]) + float(second["width"]))
    y2 = max(float(first["y"]) + float(first["height"]), float(second["y"]) + float(second["height"]))
    return {
        "x": x1,
        "y": y1,
        "width": x2 - x1,
        "height": y2 - y1,
        "area": float(first.get("area", 0.0) or 0.0) + float(second.get("area", 0.0) or 0.0),
    }


def _compact_box(box: Mapping[str, Any]) -> dict[str, Any]:
    if not box:
        return {}
    keys = ("x", "y", "width", "height", "anchor_x", "anchor_y", "score", "is_current_piece")
    return compact_dict({key: box.get(key) for key in keys})


def _fastmoss_box_center_ratio(box: Mapping[str, Any], *, image_width: int) -> float | None:
    if not box or image_width <= 0:
        return None
    return (float(box.get("x") or 0.0) + (float(box.get("width") or 0.0) / 2.0)) / float(image_width)


def _open_rgb_image(image_bytes: bytes) -> Image.Image | None:
    if not image_bytes:
        return None
    try:
        from io import BytesIO

        return Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None



def _raw_target_box(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        return [int(value[0]), int(value[1]), int(value[2]), int(value[3])]
    except (TypeError, ValueError):
        return None


def _device_pixel_ratio(page: Any) -> float | None:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        return float(evaluate("() => window.devicePixelRatio"))
    except Exception:
        return None



def _calculate_slider_drag_distance(
    *,
    slider_match: Any,
    background_box: Mapping[str, float],
    background_image_size: tuple[int, int],
    target_image_size: tuple[int, int] = (0, 0),
    target_box: Mapping[str, float],
    handle_box: Mapping[str, float],
) -> float:
    raw_target_x = float(getattr(slider_match, "target_x", 0))
    target_width_raw = float(target_image_size[0] or 0.0)
    if target_width_raw <= 0:
        target_width_raw = FASTMOSS_SLIDER_CANONICAL_TARGET_WIDTH
    target_anchor = raw_target_x - (target_width_raw / 2.0) + (
        target_width_raw * FASTMOSS_SLIDER_BODY_RIGHT_RATIO
    )
    image_width = int(background_image_size[0] or 0)
    rendered_width = float(background_box.get("width") or 0)
    if image_width > 0 and rendered_width > 0:
        target_anchor = target_anchor * rendered_width / float(image_width)
    if target_box:
        current_left = float(target_box.get("x") or 0) - float(background_box.get("x") or 0)
        current_anchor = current_left + (
            float(target_box.get("width") or 0.0) * FASTMOSS_SLIDER_BODY_RIGHT_RATIO
        )
    else:
        current_anchor = float(handle_box.get("x") or 0) - float(background_box.get("x") or 0)
    drag_distance = target_anchor - current_anchor
    return target_anchor if abs(drag_distance) < 1 else drag_distance


def _image_size(image_bytes: bytes) -> tuple[int, int]:
    if not image_bytes:
        return (0, 0)
    try:
        import io

        with Image.open(io.BytesIO(image_bytes)) as image:
            return (int(image.width), int(image.height))
    except Exception:
        return (0, 0)



def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
