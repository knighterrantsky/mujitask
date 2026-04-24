from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "automation_business_scaffold"
    / "business"
    / "flows"
    / "achieve"
    / "influencer_pool_support.py"
)
spec = importlib.util.spec_from_file_location("influencer_pool_support_under_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

format_first_influencer_contact = module.format_first_influencer_contact
format_influencer_contacts = module.format_influencer_contacts


def test_format_first_influencer_contact_skips_plain_bio_text() -> None:
    payload = {
        "list": [
            {"name": "email", "id": "", "link": "", "has": False},
            {
                "name": "bio",
                "id": "bio",
                "channel_name": "Mason's Mommy\nTeacher",
                "link": "",
                "has": True,
            },
        ]
    }

    assert format_first_influencer_contact(payload) == ""
    assert format_influencer_contacts(payload) == ""


def test_format_first_influencer_contact_keeps_bio_link() -> None:
    payload = {
        "list": [
            {"name": "email", "id": "", "link": "", "has": False},
            {
                "name": "bio",
                "id": "bio",
                "channel_name": "https://target.com/gift-registry/gift/baby-coleman-2026",
                "link": "https://target.com/gift-registry/gift/baby-coleman-2026",
                "has": True,
            },
        ]
    }

    assert (
        format_first_influencer_contact(payload)
        == "bio:https://target.com/gift-registry/gift/baby-coleman-2026"
    )


def test_format_first_influencer_contact_prefers_email_before_bio() -> None:
    payload = {
        "list": [
            {"name": "email", "id": "iammontelw@gmail.com", "link": "", "has": True},
            {
                "name": "bio",
                "id": "bio",
                "channel_name": "Master of organized chaos\niammontelw@gmail.com",
                "link": "",
                "has": True,
            },
        ]
    }

    assert format_first_influencer_contact(payload) == "email:iammontelw@gmail.com"
