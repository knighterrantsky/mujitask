from __future__ import annotations

from automation_business_scaffold.config import BusinessDefaults
from automation_business_scaffold.models import PublishPayload, SourceItem


def map_source_item_to_publish_payload(
    source_item: SourceItem,
    defaults: BusinessDefaults,
) -> PublishPayload:
    return PublishPayload(
        title=source_item.title.strip(),
        price=source_item.price,
        category=source_item.category or defaults.default_category,
        description=source_item.description or defaults.default_description,
        source_url=source_item.source_url,
        source_system=defaults.source_system,
        target_system=defaults.target_system,
    )

