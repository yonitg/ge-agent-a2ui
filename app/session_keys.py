"""Shared session state key constants and (de)serialization for A2UI integration."""

import dataclasses

from a2ui.schema.catalog import A2uiCatalog

A2UI_ENABLED_KEY = "system:a2ui_enabled"
A2UI_CATALOG_KEY = "system:a2ui_catalog"
A2UI_EXAMPLES_KEY = "system:a2ui_examples"


def serialize_catalog(catalog: A2uiCatalog | None) -> dict | None:
    """Convert an ``A2uiCatalog`` to a JSON-serializable dict for session state.

    Vertex AI Sessions persists session state as JSON, so the catalog
    dataclass cannot be stored directly — it would come back as a bare dict
    and break ``.render_as_llm_instructions()`` / ``.catalog_id``. We store a
    plain dict on write (both in-memory and Vertex paths) and rehydrate on
    read so behavior is identical across session backends.
    """
    if catalog is None or isinstance(catalog, dict):
        return catalog
    return dataclasses.asdict(catalog)


def rehydrate_catalog(value: object) -> A2uiCatalog | None:
    """Rebuild an ``A2uiCatalog`` from session state (a dict after JSON round-trip)."""
    if value is None or isinstance(value, A2uiCatalog):
        return value
    if isinstance(value, dict):
        return A2uiCatalog(**value)
    return None


def catalog_id_from_state(value: object) -> str | None:
    """Extract the catalog id from a (possibly serialized) catalog in session state."""
    catalog = rehydrate_catalog(value)
    if catalog is None:
        return None
    try:
        return catalog.catalog_id
    except Exception:
        return None
