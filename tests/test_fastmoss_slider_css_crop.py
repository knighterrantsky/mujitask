from __future__ import annotations

import io

from PIL import Image

from automation_business_scaffold.capabilities.browser.fastmoss_security.slider_challenge import (
    _crop_fastmoss_css_visible_image,
)


def test_fastmoss_slider_css_visible_crop_uses_background_position_geometry() -> None:
    raw = Image.new("RGBA", (682, 620), (0, 0, 0, 0))
    for x in range(140, 260):
        for y in range(490, 610):
            raw.putpixel((x, y), (255, 0, 0, 255))
    buffer = io.BytesIO()
    raw.save(buffer, format="PNG")

    crop, metadata = _crop_fastmoss_css_visible_image(
        buffer.getvalue(),
        background_size="334.911px 304.464px",
        background_position="-68.75px -240.625px",
        element_width=58.921875,
        element_height=58.921875,
    )

    cropped = Image.open(io.BytesIO(crop)).convert("RGBA")
    assert metadata["status"] == "success"
    assert metadata["crop_box"] == (140, 490, 260, 610)
    assert cropped.size == (120, 120)
    assert cropped.getpixel((60, 60)) == (255, 0, 0, 255)
