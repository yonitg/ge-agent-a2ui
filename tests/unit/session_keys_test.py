"""Unit tests for A2UI catalog (de)serialization in session state.

Regression coverage for the Vertex AI Sessions round-trip: session state is
persisted as JSON, so a catalog stored as an ``A2uiCatalog`` dataclass comes
back as a plain dict. The toolset then failed with
``'dict' object has no attribute 'render_as_llm_instructions'``.
"""

import json

from a2ui.schema.catalog import A2uiCatalog

from app.session_keys import (
    catalog_id_from_state,
    rehydrate_catalog,
    serialize_catalog,
)

_CATALOG = A2uiCatalog(
    version="v0.9",
    name="report",
    s2c_schema={"type": "object"},
    common_types_schema={"$defs": {}},
    catalog_schema={"catalogId": "https://example.com/report.json"},
)


def _vertex_round_trip(value):
    """Simulate what Vertex AI Sessions hands back: JSON-serialized state."""
    return json.loads(json.dumps(value))


def test_serialize_catalog_produces_json_serializable_dict():
    serialized = serialize_catalog(_CATALOG)
    assert isinstance(serialized, dict)
    # Must survive JSON serialization (Vertex Sessions persists state as JSON).
    assert _vertex_round_trip(serialized) == serialized


def test_rehydrate_after_round_trip_restores_dataclass():
    restored = rehydrate_catalog(_vertex_round_trip(serialize_catalog(_CATALOG)))
    assert isinstance(restored, A2uiCatalog)
    assert restored == _CATALOG
    # The method that broke in production must work on the rehydrated object.
    assert restored.render_as_llm_instructions()


def test_rehydrate_passes_through_existing_catalog():
    """In-memory path may already hold the dataclass; return it unchanged."""
    assert rehydrate_catalog(_CATALOG) is _CATALOG


def test_rehydrate_handles_none():
    assert rehydrate_catalog(None) is None
    assert serialize_catalog(None) is None
    assert catalog_id_from_state(None) is None


def test_catalog_id_from_state_reads_serialized_catalog():
    serialized = _vertex_round_trip(serialize_catalog(_CATALOG))
    assert catalog_id_from_state(serialized) == "https://example.com/report.json"
