"""Unit tests for A2UI v0.9 native render tools and surface builders."""

import copy
import json
import os
from unittest.mock import MagicMock

from a2ui.schema.catalog import CatalogConfig
from a2ui.schema.catalog_provider import FileSystemCatalogProvider
from a2ui.schema.manager import A2uiSchemaManager

from app import render_tools, report_tools
from app.config import COMPOSITE_CATALOG_ID
from app.render_tools import (
    GALLERY_MAX_TILES,
    VALIDATED_A2UI_JSON_KEY,
    build_dialog_demo_surface,
    build_report_confirmation_surface,
    build_tabbed_control_center_surface,
    file_report,
    resolve_escalation,
    run_ticket_action,
    show_analytics,
    show_dialog_demo,
    show_gallery,
    show_report_confirmation,
    show_report_form,
    show_reports_list,
    show_tabbed_control_center,
    show_welcome,
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CATALOG_FILE = os.path.join(_PROJECT_ROOT, "gemini_enterprise_composite_catalog.json")
_SCHEMA_MANAGER = A2uiSchemaManager(
    version="0.9",
    catalogs=[
        CatalogConfig(
            name=COMPOSITE_CATALOG_ID,
            provider=FileSystemCatalogProvider(_CATALOG_FILE),
        )
    ],
)
_CATALOG = _SCHEMA_MANAGER.get_selected_catalog()


def _assert_valid_surface_payload(payload: list[dict]):
    """Verify that a generated surface conforms to A2UI v0.9 specifications."""
    assert isinstance(payload, list)
    assert len(payload) >= 2

    # Validate against Gemini Enterprise A2UI v0.9 catalog schema
    if _CATALOG:
        for msg in payload:
            _CATALOG.validator.validate(msg)

    # Message 0: createSurface
    msg0 = payload[0]
    assert msg0.get("version") == "v0.9"
    assert "createSurface" in msg0
    cs = msg0["createSurface"]
    assert "surfaceId" in cs and len(cs["surfaceId"]) > 0
    assert cs.get("catalogId") == COMPOSITE_CATALOG_ID
    assert "root" not in cs

    # Message 1: updateComponents
    msg1 = payload[1]
    assert msg1.get("version") == "v0.9"
    assert "updateComponents" in msg1
    uc = msg1["updateComponents"]
    assert uc.get("surfaceId") == cs["surfaceId"]
    assert isinstance(uc.get("components"), list)
    assert len(uc["components"]) > 0

    # Validate flat list and ID uniqueness
    seen_ids = set()
    root_found = False
    for comp in uc["components"]:
        assert isinstance(comp, dict)
        cid = comp.get("id")
        assert cid is not None, f"Component missing 'id': {comp}"
        assert cid not in seen_ids, f"Duplicate component ID: {cid}"
        seen_ids.add(cid)
        if cid == "root":
            root_found = True
        assert "component" in comp, f"Component missing 'component' type name: {comp}"
        assert isinstance(comp["component"], str)

    assert root_found, "Surface missing 'root' component"

    # Validate reference integrity (no dangling child IDs)
    for comp in uc["components"]:
        for child_key in ("children",):
            if child_key in comp and isinstance(comp[child_key], list):
                for child_id in comp[child_key]:
                    assert child_id in seen_ids, (
                        f"Dangling child ID '{child_id}' referenced by '{comp['id']}'"
                    )


def test_build_tabbed_control_center_surface_all_tabs():
    """Verify 5-tab Operations Control Center structure and native components."""
    for tab_idx in range(5):
        surface = build_tabbed_control_center_surface(tab_idx)
        _assert_valid_surface_payload(surface)

        # Tabbed control center must have 4 messages (chart + report form data models)
        assert len(surface) == 4
        msg2 = surface[2]
        assert msg2.get("version") == "v0.9"
        assert "updateDataModel" in msg2
        udm = msg2["updateDataModel"]
        assert udm.get("path") == "/chart_spec"
        assert "values" in udm.get("value", {}).get("data", {})

        msg3 = surface[3]
        assert msg3.get("version") == "v0.9"
        form_udm = msg3["updateDataModel"]
        assert form_udm.get("surfaceId") == surface[0]["createSurface"]["surfaceId"]
        assert form_udm.get("path") == "/report_form"
        assert form_udm["value"]["issue"] == "", "The issue field must start empty"

        # Verify specific native Material 3 components in updateComponents
        components = surface[1]["updateComponents"]["components"]
        comp_types = {c["component"] for c in components}
        expected_types = {
            "MaterialCard",
            "MaterialColumn",
            "MaterialRow",
            "MaterialText",
            "MaterialIcon",
            "MaterialButton",
            "MaterialDivider",
            "MaterialTabs",
            "MaterialGridList",
            "MaterialProgressBar",
            "MaterialProgressSpinner",
            "MaterialSlideToggle",
            "MaterialTable",
            "MaterialChips",
            "MaterialExpansionPanel",
            "VegaChart",
            "MaterialSlider",
            "MaterialSelect",
            "MaterialInput",
            "MaterialRadioButton",
            "MaterialDatepicker",
            "MaterialTimepicker",
            "MaterialCheckbox",
        }
        for exp in expected_types:
            assert exp in comp_types, f"Missing expected A2UI v0.9 component '{exp}' in control center"


def test_build_tabbed_control_center_surface_category_filter():
    """Verify category filtering via in-place native MaterialTabs in Tab 1."""
    cat_keys = ["all", "hvac", "electrical", "plumbing", "furniture", "safety"]
    for idx, cat in enumerate(cat_keys):
        surface = build_tabbed_control_center_surface(active_tab=1, category_filter=cat)
        _assert_valid_surface_payload(surface)
        components = surface[1]["updateComponents"]["components"]
        tabs = next(c for c in components if c["id"] == "directory-tabs")
        assert tabs["component"] == "MaterialTabs"
        assert tabs["activeTab"] == idx
        assert len(tabs["tabs"]) == 6
        table = next(c for c in components if c["id"] == f"cat-table-{cat}")
        assert table["component"] == "MaterialTable"
        assert isinstance(table["rows"], list)
        assert len(table["rows"]) > 0


def _dialog_demo_parts(surface: list[dict]) -> tuple[str, dict, dict]:
    """Return (surface id, components by id, initial /demo data model)."""
    surface_id = surface[0]["createSurface"]["surfaceId"]
    components = {c["id"]: c for c in surface[1]["updateComponents"]["components"]}
    update = surface[2]["updateDataModel"]
    assert update["surfaceId"] == surface_id
    assert update["path"] == "/demo"
    return surface_id, components, update["value"]


def _assert_demo_card_update(payload: list[dict], surface_id: str, status_fragment: str):
    """The agent's answer updates the existing card: dialog closed, new status."""
    assert len(payload) == 1, "Only an update for the existing card, no new surface"
    if _CATALOG:
        _CATALOG.validator.validate(payload[0])
    update = payload[0]["updateDataModel"]
    assert update["surfaceId"] == surface_id
    assert update["path"] == "/demo"
    assert update["value"]["dialogOpen"] is False
    assert status_fragment in update["value"]["status"]


def test_build_dialog_demo_surface():
    """Verify MaterialDialog and MaterialMenu showcase surface."""
    surface = build_dialog_demo_surface()
    _assert_valid_surface_payload(surface)
    surface_id, components, model = _dialog_demo_parts(surface)
    comp_types = {c["component"] for c in components.values()}
    assert "MaterialDialog" in comp_types
    assert "MaterialMenu" in comp_types
    assert model["status"] == render_tools.DIALOG_DEMO_INITIAL_STATUS

    # Regression: a static `"open": true` popped the dialog up every time the
    # chat was reopened. It now starts closed and its trigger opens it locally.
    dialog = components["dialog-sample"]
    assert dialog["open"] == {"path": "/demo/dialogOpen"}
    assert model["dialogOpen"] is False
    trigger = components[dialog["trigger"]]
    assert trigger["component"] == "MaterialButton"
    assert "action" not in trigger, "Opening the dialog needs no agent round trip"

    # Confirm/Cancel and the menu tell the agent which card to update.
    for button_id, decision in (("dialog-confirm-btn", "confirm"), ("dialog-cancel-btn", "cancel")):
        event = components[button_id]["action"]["event"]
        assert event["name"] == "resolveEscalation"
        assert event["context"]["decision"] == decision
        assert event["context"]["surface_id"] == surface_id
    menu = components["menu-sample"]
    assert menu["value"] == {"path": "/demo/ticketAction"}
    menu_event = menu["action"]["event"]
    assert menu_event["name"] == "ticketAction"
    assert menu_event["context"]["ticket_action"] == {"path": "/demo/ticketAction"}
    assert menu_event["context"]["surface_id"] == surface_id
    assert {o["value"] for o in menu["options"]} == set(render_tools.TICKET_ACTION_LABELS)


def test_dialog_buttons_close_the_dialog_on_the_same_card():
    """Confirm/Cancel update the card they came from instead of rendering a new one."""
    ctx = MagicMock()
    ctx.state = {}
    surface = show_dialog_demo(ctx)[VALIDATED_A2UI_JSON_KEY]
    surface_id, _, _ = _dialog_demo_parts(surface)
    assert ctx.state[render_tools.DIALOG_DEMO_SURFACES_KEY] == [surface_id]

    confirmed = resolve_escalation(ctx, decision="confirm", surface_id=surface_id)
    assert ctx.actions.skip_summarization is True
    _assert_demo_card_update(confirmed[VALIDATED_A2UI_JSON_KEY], surface_id, "Escalation confirmed")

    cancelled = resolve_escalation(ctx, decision="cancel", surface_id=surface_id)
    _assert_demo_card_update(cancelled[VALIDATED_A2UI_JSON_KEY], surface_id, "Escalation cancelled")


def test_dialog_actions_target_a_card_this_chat_rendered():
    """A mistyped surface id falls back to the newest card rendered in this chat."""
    ctx = MagicMock()
    ctx.state = {}
    first_id, _, _ = _dialog_demo_parts(show_dialog_demo(ctx)[VALIDATED_A2UI_JSON_KEY])
    second_id, _, _ = _dialog_demo_parts(show_dialog_demo(ctx)[VALIDATED_A2UI_JSON_KEY])

    res = resolve_escalation(ctx, decision="confirm", surface_id=first_id)
    _assert_demo_card_update(res[VALIDATED_A2UI_JSON_KEY], first_id, "confirmed")
    res = resolve_escalation(ctx, decision="confirm", surface_id="dialog-demo-typo")
    _assert_demo_card_update(res[VALIDATED_A2UI_JSON_KEY], second_id, "confirmed")


def test_dialog_actions_without_a_known_card_render_a_new_one():
    """After a lost session a well-formed id is still updated; otherwise a card is rendered."""
    ctx = MagicMock()
    ctx.state = {}
    res = resolve_escalation(ctx, decision="confirm", surface_id="dialog-demo-0123456789")
    _assert_demo_card_update(res[VALIDATED_A2UI_JSON_KEY], "dialog-demo-0123456789", "confirmed")

    res = resolve_escalation(ctx, decision="cancel", surface_id="")
    surface = res[VALIDATED_A2UI_JSON_KEY]
    _assert_valid_surface_payload(surface)
    surface_id, _, model = _dialog_demo_parts(surface)
    assert model["dialogOpen"] is False
    assert "Escalation cancelled" in model["status"]
    assert ctx.state[render_tools.DIALOG_DEMO_SURFACES_KEY] == [surface_id]


def test_ticket_action_menu_updates_the_same_card():
    """A menu choice is shown on the card, by its label."""
    ctx = MagicMock()
    ctx.state = {}
    surface_id, _, _ = _dialog_demo_parts(show_dialog_demo(ctx)[VALIDATED_A2UI_JSON_KEY])

    res = run_ticket_action(ctx, ticket_action="export", surface_id=surface_id)
    assert ctx.actions.skip_summarization is True
    _assert_demo_card_update(res[VALIDATED_A2UI_JSON_KEY], surface_id, "Export Incident PDF")
    res = run_ticket_action(ctx, ticket_action="Duplicate Ticket", surface_id=surface_id)
    _assert_demo_card_update(res[VALIDATED_A2UI_JSON_KEY], surface_id, "Duplicate Ticket")
    res = run_ticket_action(ctx, ticket_action="", surface_id=surface_id)
    _assert_demo_card_update(res[VALIDATED_A2UI_JSON_KEY], surface_id, "No ticket action")


def test_build_report_confirmation_surface():
    """Verify confirmation surface with and without sketch image."""
    s1 = build_report_confirmation_surface("Yellow Room", "Desk", "Broken leg", None)
    _assert_valid_surface_payload(s1)

    s2 = build_report_confirmation_surface(
        "Yellow Room", "Desk", "Broken leg", "data:image/jpeg;base64,sample123"
    )
    _assert_valid_surface_payload(s2)
    components2 = s2[1]["updateComponents"]["components"]
    img_comp = next(c for c in components2 if c["component"] == "Image")
    assert img_comp.get("url") == "data:image/jpeg;base64,sample123"


def test_show_tools_return_validated_payloads():
    """Verify all show_* tools set skip_summarization and return valid A2UI payloads."""
    tools = [
        (show_tabbed_control_center, [0]),
        (show_welcome, []),
        (show_reports_list, []),
        (show_gallery, []),
        (show_analytics, []),
        (show_report_form, []),
        (show_dialog_demo, []),
        (show_report_confirmation, ["Yellow Room", "Chair", "Broken"]),
    ]

    for fn, args in tools:
        ctx = MagicMock()
        res = fn(ctx, *args)
        assert ctx.actions.skip_summarization is True
        assert VALIDATED_A2UI_JSON_KEY in res
        _assert_valid_surface_payload(res[VALIDATED_A2UI_JSON_KEY])


def test_file_report_dedup_and_submission(tmp_path, monkeypatch):
    """Verify file_report executes successfully and returns confirmation."""
    _use_temp_report_storage(tmp_path, monkeypatch)
    ctx = MagicMock()
    res = file_report(
        ctx,
        room="Yellow Room",
        item="Whiteboard",
        issue="Mounting bracket loose",
        reporter_name="Tester",
        reporter_email="tester@example.com",
    )
    assert ctx.actions.skip_summarization is True
    assert VALIDATED_A2UI_JSON_KEY in res
    _assert_valid_surface_payload(res[VALIDATED_A2UI_JSON_KEY])


# ---------------------------------------------------------------------------
# Dispatch form (Tab 4): the filed report must be what the user entered.
# ---------------------------------------------------------------------------

_USER_FORM_INPUT = {
    "form-room-select": "Ocean",
    "form-item-select": "Window",
    "form-issue-input": "Cracked glass pane next to the reception desk",
    "form-reporter-name": "Dana Levi",
    "form-reporter-email": "dana@example.com",
    "form-priority-radio": "emergency",
    "form-chips": "safety",
}


def _set_pointer(model: dict, pointer: str, value) -> None:
    """Write `value` at a JSON Pointer path, creating parent objects."""
    *parents, leaf = pointer.strip("/").split("/")
    for key in parents:
        model = model.setdefault(key, {})
    model[leaf] = value


def _get_pointer(model: dict, pointer: str):
    """Read the value at a JSON Pointer path."""
    for key in pointer.strip("/").split("/"):
        model = model[key]
    return model


def _simulate_form_submit(surface: list[dict], user_input: dict) -> dict:
    """Act like an A2UI v0.9 client and return the action context it sends.

    Seeds the data model from `updateDataModel` messages, applies the user's
    edits through each control's two-way binding, then resolves the submit
    button's action context the way a renderer does at click time.
    """
    data_model: dict = {}
    for msg in surface:
        update = msg.get("updateDataModel")
        if update:
            _set_pointer(data_model, update["path"], copy.deepcopy(update["value"]))

    components = {c["id"]: c for c in surface[1]["updateComponents"]["components"]}
    for component_id, new_value in user_input.items():
        component = components[component_id]
        prop = "checked" if component["component"] == "MaterialCheckbox" else "value"
        binding = component.get(prop)
        assert isinstance(binding, dict) and "path" in binding, (
            f"'{component_id}' is not bound to the data model, so what the user "
            f"enters never reaches the submit action: {binding!r}"
        )
        _set_pointer(data_model, binding["path"], new_value)

    context = components["form-submit-btn"]["action"]["event"]["context"]
    return {
        key: _get_pointer(data_model, value["path"]) if isinstance(value, dict) else value
        for key, value in context.items()
    }


def _use_temp_report_storage(tmp_path, monkeypatch):
    """Point report storage at a temp file and stub out sketch generation."""
    storage = tmp_path / "reports.json"
    monkeypatch.setattr(report_tools, "_RUNTIME_DATA_PATH", str(storage))
    monkeypatch.setattr(render_tools, "_recent_reports", {})
    fake_sketch = MagicMock(return_value={
        "status": "success",
        "image_blob": "test_issue.png",
        "image_url": "data:image/png;base64,AAAA",
    })
    monkeypatch.setattr(render_tools, "generate_visual_sketch", fake_sketch)
    monkeypatch.setattr(report_tools, "generate_visual_sketch", fake_sketch)
    return storage


def test_report_form_submits_what_the_user_entered():
    """Regression: Submit used to send a hard-coded prompt, so every form
    submission filed the same Yellow Room / broken chair report."""
    surface = build_tabbed_control_center_surface(4)
    _assert_valid_surface_payload(surface)

    sent = _simulate_form_submit(surface, _USER_FORM_INPUT)

    assert sent["room"] == "Ocean"
    assert sent["item"] == "Window"
    assert sent["issue"] == "Cracked glass pane next to the reception desk"
    assert sent["reporter_name"] == "Dana Levi"
    assert sent["reporter_email"] == "dana@example.com"
    assert sent["priority"] == "emergency"
    assert sent["category"] == "safety"
    assert "Yellow Room" not in json.dumps(sent)
    assert "chair" not in json.dumps(sent).lower()


def test_untouched_report_form_does_not_submit_sample_text():
    """Free-text fields start empty, so no canned issue can be filed by accident."""
    sent = _simulate_form_submit(build_tabbed_control_center_surface(4), {})

    assert sent["issue"] == ""
    assert sent["reporter_name"] == ""
    assert sent["reporter_email"] == ""


def test_file_report_persists_submitted_form_values(tmp_path, monkeypatch):
    """The saved report and the incidents table reflect the submitted form."""
    storage = _use_temp_report_storage(tmp_path, monkeypatch)
    sent = _simulate_form_submit(build_tabbed_control_center_surface(4), _USER_FORM_INPUT)
    sent.pop("prompt")

    ctx = MagicMock()
    res = file_report(ctx, **sent)

    assert ctx.actions.skip_summarization is True
    confirmation = res[VALIDATED_A2UI_JSON_KEY]
    _assert_valid_surface_payload(confirmation)
    assert "Ocean" in json.dumps(confirmation)
    assert "Yellow Room" not in json.dumps(confirmation)

    saved = json.loads(storage.read_text())[-1]
    assert saved["room"] == "Ocean"
    assert saved["item"] == "Window"
    assert saved["issue"] == "Cracked glass pane next to the reception desk"
    assert saved["reporter_name"] == "Dana Levi"
    assert saved["reporter_email"] == "dana@example.com"
    assert saved["priority"] == "CRITICAL"
    assert saved["category"] == "Safety"

    components = build_tabbed_control_center_surface(1)[1]["updateComponents"]["components"]
    table = next(c for c in components if c["id"] == "cat-table-all")
    row = next(r for r in table["rows"] if r["issue"] == saved["issue"])
    assert row["room"] == "Ocean"
    assert row["priority"] == "CRITICAL"
    assert row["category"].endswith("Safety")
    safety_table = next(c for c in components if c["id"] == "cat-table-safety")
    assert any(r["issue"] == saved["issue"] for r in safety_table["rows"])


def test_file_report_rejects_a_blank_issue(tmp_path, monkeypatch):
    """Submitting without an issue description asks for it instead of saving."""
    storage = _use_temp_report_storage(tmp_path, monkeypatch)
    sent = _simulate_form_submit(build_tabbed_control_center_surface(4), {})
    sent.pop("prompt")
    reports_before = json.loads(storage.read_text())

    ctx = MagicMock()
    res = file_report(ctx, **sent)

    assert res["status"] == "error"
    assert "issue" in res["message"].lower()
    assert VALIDATED_A2UI_JSON_KEY not in res
    # The agent must get a turn to tell the user what is missing.
    assert ctx.actions.skip_summarization is not True
    assert json.loads(storage.read_text()) == reports_before


# ---------------------------------------------------------------------------
# Gallery (Tab 2) and input limits.
# ---------------------------------------------------------------------------


def _gallery_tiles(surface: list[dict]) -> list[tuple[str, str]]:
    """Return (title, image url) for each gallery tile, in display order."""
    components = {c["id"]: c for c in surface[1]["updateComponents"]["components"]}
    tiles = []
    for card_id in components["gallery-grid-list"]["children"]:
        column = components[components[card_id]["children"][0]]
        img_id, title_id, _ = column["children"]
        tiles.append((components[title_id]["text"], components[img_id]["url"]))
    return tiles


def test_gallery_shows_the_newest_reports_first(tmp_path, monkeypatch):
    """The gallery leads with a just-filed report and stays small enough to reopen."""
    storage = _use_temp_report_storage(tmp_path, monkeypatch)
    file_report(MagicMock(), room="Ocean", item="Window", issue="Cracked glass pane")
    reports = json.loads(storage.read_text())
    assert len(reports) > GALLERY_MAX_TILES

    surface = build_tabbed_control_center_surface(2)
    _assert_valid_surface_payload(surface)
    tiles = _gallery_tiles(surface)

    assert len(tiles) == GALLERY_MAX_TILES
    assert tiles[0][0] == "Ocean • Window", "The newest report must lead the gallery"
    assert [title for title, _ in tiles] == [
        f"{r['room']} • {r['item']}" for r in list(reversed(reports))[:GALLERY_MAX_TILES]
    ]
    # Images are bundled with the service and inlined, never fetched from a bucket.
    assert all(url.startswith("data:image/") for _, url in tiles)
    components = surface[1]["updateComponents"]["components"]
    count = next(c for c in components if c["id"] == "gallery-count-text")
    assert count["text"] == f"{GALLERY_MAX_TILES} Sketches"
    # MaterialBadge is a corner badge; used as a label it was clipped at the card edge.
    assert not any(c["component"] == "MaterialBadge" for c in components)
    # Every report is still listed in the Incidents Table.
    assert f"all {len(reports)} reports" in next(
        c for c in components if c["id"] == "gallery-info-desc"
    )["text"]
    # Gemini Enterprise re-renders a reopened chat only for small A2UI messages.
    assert len(json.dumps(surface)) < 300_000


def test_file_report_caps_free_text_lengths(tmp_path, monkeypatch):
    """Over-long fields are cut before they reach the image model or storage."""
    storage = _use_temp_report_storage(tmp_path, monkeypatch)

    file_report(
        MagicMock(),
        room="R" * 1000,
        item="I" * 1000,
        issue="  " + "x" * 5000 + "  ",
        reporter_name="N" * 1000,
        reporter_email="e" * 1000 + "@example.com",
    )

    saved = json.loads(storage.read_text())[-1]
    assert saved["room"] == "R" * render_tools.MAX_SHORT_FIELD_CHARS
    assert saved["item"] == "I" * render_tools.MAX_SHORT_FIELD_CHARS
    assert saved["issue"] == "x" * render_tools.MAX_ISSUE_CHARS
    assert saved["reporter_name"] == "N" * render_tools.MAX_SHORT_FIELD_CHARS
    assert len(saved["reporter_email"]) == render_tools.MAX_EMAIL_CHARS
    render_tools.generate_visual_sketch.assert_called_once_with(
        room=saved["room"], item=saved["item"], issue=saved["issue"]
    )
