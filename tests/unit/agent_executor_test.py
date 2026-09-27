"""Unit tests for agent_executor post-processing helpers."""

from a2a.types import DataPart, Part, TextPart
from a2ui.a2a.parts import create_a2ui_part

from app.agent_executor import (
    CARD_UPDATED_TEXT,
    SURFACE_HEADER_TEXT,
    _add_leading_text,
    _process_a2ui_parts,
    _repair_catalog_id,
)

VALID_CATALOG_ID = "https://a2ui.org/specification/v0_9/basic_catalog.json"
HALLUCINATED_CATALOG_ID = "made_up_catalog:v0_9"


def test_repair_leaves_correct_catalog_id_unchanged():
    msg = {
        "version": "v0.9",
        "createSurface": {"surfaceId": "s1", "catalogId": VALID_CATALOG_ID},
    }
    _repair_catalog_id(msg, VALID_CATALOG_ID)
    assert msg["createSurface"]["catalogId"] == VALID_CATALOG_ID


def test_repair_replaces_hallucinated_catalog_id():
    msg = {
        "version": "v0.9",
        "createSurface": {"surfaceId": "s1", "catalogId": HALLUCINATED_CATALOG_ID},
    }
    _repair_catalog_id(msg, VALID_CATALOG_ID)
    assert msg["createSurface"]["catalogId"] == VALID_CATALOG_ID


def test_repair_fills_missing_catalog_id():
    msg = {"version": "v0.9", "createSurface": {"surfaceId": "s1"}}
    _repair_catalog_id(msg, VALID_CATALOG_ID)
    assert msg["createSurface"]["catalogId"] == VALID_CATALOG_ID


def test_repair_ignores_non_create_surface_messages():
    msg = {
        "version": "v0.9",
        "updateDataModel": {"surfaceId": "s1", "path": "/x", "value": 1},
    }
    _repair_catalog_id(msg, VALID_CATALOG_ID)
    assert "catalogId" not in msg["updateDataModel"]


def test_repair_skips_v0_8_begin_rendering():
    """v0.8 beginRendering carries no catalogId; should be untouched."""
    msg = {"beginRendering": {"surfaceId": "s1", "root": "root"}}
    _repair_catalog_id(msg, VALID_CATALOG_ID)
    assert "catalogId" not in msg["beginRendering"]


def test_process_parts_repairs_catalog_id_end_to_end():
    bad_part = create_a2ui_part(
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": "s1",
                "catalogId": HALLUCINATED_CATALOG_ID,
            },
        }
    )
    out = _process_a2ui_parts([bad_part], valid_catalog_id=VALID_CATALOG_ID)
    assert len(out) == 1
    assert out[0].root.data["createSurface"]["catalogId"] == VALID_CATALOG_ID


def test_process_parts_no_op_when_no_valid_catalog_id():
    """When no session catalog is known, leave catalogId untouched."""
    bad_part = create_a2ui_part(
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": "s1",
                "catalogId": HALLUCINATED_CATALOG_ID,
            },
        }
    )
    out = _process_a2ui_parts([bad_part], valid_catalog_id=None)
    assert out[0].root.data["createSurface"]["catalogId"] == HALLUCINATED_CATALOG_ID


def test_process_parts_passes_through_non_a2ui_parts():
    plain_part = Part(root=DataPart(data={"foo": "bar"}, metadata={"mimeType": "x"}))
    out = _process_a2ui_parts([plain_part], valid_catalog_id=VALID_CATALOG_ID)
    assert out == [plain_part]


def _texts(parts):
    return [p.root.text for p in parts if isinstance(p.root, TextPart)]


def test_leading_text_heads_a_new_card():
    parts = [
        create_a2ui_part(
            {"version": "v0.9", "createSurface": {"surfaceId": "s1", "catalogId": VALID_CATALOG_ID}}
        ),
        create_a2ui_part({"version": "v0.9", "updateComponents": {"surfaceId": "s1", "components": []}}),
    ]
    _add_leading_text(parts)
    assert _texts(parts) == [SURFACE_HEADER_TEXT]
    assert isinstance(parts[0].root, TextPart)


def test_leading_text_for_an_update_to_an_earlier_card():
    """An update-only reply has no card of its own, so it must not look like an empty dashboard."""
    parts = [
        create_a2ui_part(
            {"version": "v0.9", "updateDataModel": {"surfaceId": "s1", "path": "/demo", "value": {}}}
        )
    ]
    _add_leading_text(parts)
    assert _texts(parts) == [CARD_UPDATED_TEXT]


def test_leading_text_keeps_existing_text_and_ignores_plain_replies():
    with_text = [
        Part(root=TextPart(text="Here you go")),
        create_a2ui_part({"version": "v0.9", "createSurface": {"surfaceId": "s1"}}),
    ]
    _add_leading_text(with_text)
    assert _texts(with_text) == ["Here you go"]

    plain = [Part(root=DataPart(data={"foo": "bar"}, metadata={"mimeType": "x"}))]
    _add_leading_text(plain)
    assert len(plain) == 1


def test_vertex_model_name_qualification(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "test-project")
    res = config.build_vertex_model_name("gemini-3.7-flash")
    assert "publishers/google/models/gemini-3.7-flash" in res
    assert res.startswith("projects/test-project/")

