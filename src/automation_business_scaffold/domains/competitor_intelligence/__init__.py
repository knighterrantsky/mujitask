"""Competitor intelligence domain facade.

The runtime implementation still lives under ``business`` during the migration.
This package exposes the target domain-shaped import surface without changing
execution behavior.
"""

DOMAIN_CODE = "competitor_intelligence"

__all__ = ["DOMAIN_CODE"]
