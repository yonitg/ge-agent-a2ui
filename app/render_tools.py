"""A2UI v0.9 Server-Side Rendering Tools for the Operations Control Center Agent.

Pure native A2UI v0.9 implementation showcasing:
- MaterialCard container and layout surfaces
- MaterialTabs 5-tab Operations & Incidents Control Center
- MaterialTable incidents directory with category filter tabs
- MaterialGridList responsive visual sketches gallery
- VegaChart with dynamic data model binding (/chart_spec)
- MaterialSlider for interactive SLA forecasting simulation
- Complete Material form controls (Select, Input, Radio, Checkbox, Datepicker, Timepicker)
  two-way bound to /report_form and submitted through data-bound action context
- MaterialDialog (opened by its trigger button, closed by the agent through an
  updateDataModel on the same card) and MaterialMenu demonstrations
"""

import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import COMPOSITE_CATALOG_ID
from app.report_tools import (
    _get_default_sketch_data_uri,
    _get_image_data_uri,
    generate_visual_sketch,
    list_reports,
    submit_maintenance_report,
)

logger = logging.getLogger(__name__)

# Matches SendA2uiToClientToolset._SendA2uiJsonToClientTool.VALIDATED_A2UI_JSON_KEY.
VALIDATED_A2UI_JSON_KEY = "validated_a2ui_json"

# The gallery shows the most recent sketches only. Each inline thumbnail adds
# ~20 KB to the dashboard message, and Gemini Enterprise can only re-render a
# reopened chat when that message stays small (with every report inlined it
# came back as "Unsupported attachment"). The Incidents Table lists all reports.
GALLERY_MAX_TILES = 9

# Canonical Vega-Lite specification for the Analytics & SLA Forecasting tab
VEGA_LITE_INCIDENTS_SPEC: dict[str, Any] = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "description": "Monthly Maintenance Incident Volume by Category",
    "data": {
        "values": [
            {"category": "HVAC", "count": 28, "sla_rate": 78},
            {"category": "Electrical", "count": 19, "sla_rate": 94},
            {"category": "Plumbing", "count": 14, "sla_rate": 65},
            {"category": "Furniture", "count": 22, "sla_rate": 88},
            {"category": "Safety", "count": 8, "sla_rate": 100},
            {"category": "IT Infra", "count": 15, "sla_rate": 91},
        ]
    },
    "mark": {"type": "bar", "cornerRadiusEnd": 4},
    "encoding": {
        "x": {
            "field": "category",
            "type": "nominal",
            "axis": {"labelAngle": 0},
            "title": "Category",
        },
        "y": {
            "field": "count",
            "type": "quantitative",
            "title": "Monthly Incidents",
        },
        "color": {
            "field": "sla_rate",
            "type": "quantitative",
            "scale": {"scheme": "blues"},
            "legend": {"title": "SLA %"},
        },
        "tooltip": [
            {"field": "category", "type": "nominal"},
            {"field": "count", "type": "quantitative", "title": "Total Incidents"},
            {"field": "sla_rate", "type": "quantitative", "title": "On-Time SLA %"},
        ],
    },
}


def _sid(prefix: str) -> str:
    """Generate a fresh, unique surface id per turn."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _fmt_time(ts: str) -> str:
    """Format an ISO timestamp to human-readable string."""
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts or ""


def _categorize_incident(item: str = "", issue: str = "") -> str:
    """Categorize an incident report into one of: HVAC, Electrical, Plumbing, Furniture, Safety."""
    text = f"{item} {issue}".lower()
    if any(k in text for k in ["ac", "air condition", "hvac", "vent", "thermostat", "temperature", "heat", "cool", "chiller"]):
        return "HVAC"
    if any(k in text for k in ["light", "lamp", "socket", "power", "electric", "wiring", "cable", "switch", "outlet", "bulb", "breaker"]):
        return "Electrical"
    if any(k in text for k in ["leak", "pipe", "sink", "faucet", "water", "toilet", "drain", "plumb", "flood", "sewage", "valve"]):
        return "Plumbing"
    if any(k in text for k in ["chair", "desk", "table", "door", "window", "cabinet", "shelf", "furniture", "couch", "seat", "blind", "lock", "handle", "whiteboard", "rug", "carpet"]):
        return "Furniture"
    if any(k in text for k in ["fire", "smoke", "alarm", "hazard", "safety", "emergency", "danger", "extinguisher", "sprinkler", "broken glass", "spill", "sensor"]):
        return "Safety"
    return "Safety" if ("emergency" in text or "hazard" in text or "spill" in text) else "Furniture"


# Data-model location of the Tab 4 dispatch form. Every form control is two-way
# bound under this path and the submit button sends the bound values in its
# action context, so the filed report is exactly what the user entered.
REPORT_FORM_PATH = "/report_form"

# Form priority values (and stored levels) -> level shown in the incidents table.
_PRIORITY_LEVELS = {
    "low": "LOW",
    "med": "MEDIUM",
    "medium": "MEDIUM",
    "high": "HIGH",
    "emergency": "CRITICAL",
    "critical": "CRITICAL",
}

# Form category values (and stored labels) -> label shown in the incidents table.
_CATEGORY_LABELS = {
    "hvac": "HVAC",
    "electrical": "Electrical",
    "plumbing": "Plumbing",
    "furniture": "Furniture",
    "safety": "Safety",
}


def _normalize_priority(value: Any) -> str:
    """Map a form or stored priority to LOW/MEDIUM/HIGH/CRITICAL ('' if unknown)."""
    words = str(value or "").lower().split()
    return _PRIORITY_LEVELS.get(words[0], "") if words else ""


def _normalize_category(value: Any) -> str:
    """Map a form or stored category to its table label ('' if unknown)."""
    words = str(value or "").lower().split()
    return _CATEGORY_LABELS.get(words[0], "") if words else ""


def _form_binding(field: str) -> dict:
    """Two-way data binding to one field of the dispatch form's data model."""
    return {"path": f"{REPORT_FORM_PATH}/{field}"}


def _report_form_defaults() -> dict:
    """Initial dispatch-form values. Free-text fields start empty."""
    service_day = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    return {
        "room": "Yellow Room",
        "item": "Chair",
        "issue": "",
        "reporter_name": "",
        "reporter_email": "",
        "priority": "med",
        "category": "",
        "service_date": {
            "year": service_day.year,
            "month": service_day.month,
            "day": service_day.day,
        },
        "service_time": f"{service_day.isoformat()}T14:00:00Z",
        "notify_director": True,
        "order_parts": False,
    }


# ---------------------------------------------------------------------------
# Core 5-Tab Operations Control Center Builder (A2UI v0.9 Native)
# ---------------------------------------------------------------------------


def build_tabbed_control_center_surface(
    active_tab: int = 0, category_filter: str = "all"
) -> list[dict]:
    """Build a pure A2UI v0.9 flat multi-component surface showcasing 5 tabs:
      - Tab 0: Overview & Dispatch (KPI Badges, Progress, Quick Action Buttons)
      - Tab 1: Incidents Directory (In-Place Interactive Table with Chips & Search)
      - Tab 2: Visual Sketches Gallery (Pure Native MaterialGridList with Base64 Thumbnails)
      - Tab 3: Analytics & SLA Dashboard (VegaChart with dynamic data model binding & MaterialSlider)
      - Tab 4: Dispatch & Service Form (MaterialSelect, MaterialInput, MaterialRadioButton,
               MaterialCheckbox, MaterialDatepicker, MaterialTimepicker, MaterialChips, MaterialButton)
    """
    reports_res = list_reports(None)
    reports = reports_res if isinstance(reports_res, list) else []

    # Format all table rows with string values
    all_table_rows = []
    for idx, r in enumerate(reports, start=1):
        rid = str(r.get("id") or idx)
        issue_text = str(r.get("issue", ""))
        item_text = str(r.get("item", "Unknown"))
        sev = _normalize_priority(r.get("priority")) or (
            "CRITICAL" if "emergency" in issue_text.lower() else ("HIGH" if "broken" in issue_text.lower() else "MEDIUM")
        )
        cat = _normalize_category(r.get("category")) or _categorize_incident(item_text, issue_text)
        cat_icon = {
            "HVAC": "❄️",
            "Electrical": "⚡",
            "Plumbing": "💧",
            "Furniture": "🪑",
            "Safety": "🛡️",
        }.get(cat, "📋")
        all_table_rows.append({
            "id": f"#{rid}",
            "priority": sev,
            "category": f"{cat_icon} {cat}",
            "category_key": cat.lower(),
            "room": str(r.get("room", "Unknown")),
            "item": item_text,
            "issue": issue_text,
            "reporter": str(r.get("reporter_name", "Anonymous")),
            "status": "OPEN",
        })

    if not all_table_rows:
        all_table_rows.append({
            "id": "#1",
            "priority": "LOW",
            "category": "🪑 Furniture",
            "category_key": "furniture",
            "room": "Yellow Room",
            "item": "Ergonomic Chair",
            "issue": "Elevation hydraulic lever sticking",
            "reporter": "Facilities Lead",
            "status": "MONITORING",
        })

    cat_counts = {
        "all": len(all_table_rows),
        "hvac": sum(1 for r in all_table_rows if r.get("category_key") == "hvac"),
        "electrical": sum(1 for r in all_table_rows if r.get("category_key") == "electrical"),
        "plumbing": sum(1 for r in all_table_rows if r.get("category_key") == "plumbing"),
        "furniture": sum(1 for r in all_table_rows if r.get("category_key") == "furniture"),
        "safety": sum(1 for r in all_table_rows if r.get("category_key") == "safety"),
    }

    cat_filter = str(category_filter or "all").lower().strip()

    surface_id = _sid("ops-control-center")
    components: list[dict] = []

    # Root container
    components.append({
        "id": "root",
        "component": "MaterialCard",
        "appearance": "raised",
        "children": ["main-column"],
    })

    # Main Column
    components.append({
        "id": "main-column",
        "component": "MaterialColumn",
        "align": "stretch",
        "children": ["header-row", "header-divider", "tabs"],
    })

    # Header Row
    components.append({
        "id": "header-row",
        "component": "MaterialRow",
        "justify": "spaceBetween",
        "align": "center",
        "children": ["header-title-col", "header-actions-row"],
    })

    components.append({
        "id": "header-title-col",
        "component": "MaterialColumn",
        "children": ["header-title", "header-sub"],
    })
    components.append({
        "id": "header-title",
        "component": "MaterialText",
        "text": "Facilities & Incident Operations Center",
        "usageHint": "h2",
    })
    components.append({
        "id": "header-sub",
        "component": "MaterialText",
        "text": "Google ADK with Native A2UI v0.9 • Standalone Cloud Run",
        "usageHint": "caption",
    })

    components.append({
        "id": "header-actions-row",
        "component": "MaterialRow",
        "align": "center",
        "children": ["live-status-row"],
    })

    components.append({
        "id": "live-status-row",
        "component": "MaterialRow",
        "align": "center",
        "children": ["live-status-icon", "live-status-text"],
    })
    components.append({
        "id": "live-status-icon",
        "component": "MaterialIcon",
        "icon": "cloud_done",
        "color": "primary",
    })
    components.append({
        "id": "live-status-text",
        "component": "MaterialText",
        "text": "Telemetry Active",
        "usageHint": "caption",
    })

    components.append({
        "id": "header-divider",
        "component": "MaterialDivider",
    })

    # 5-Tab Navigation Component
    components.append({
        "id": "tabs",
        "component": "MaterialTabs",
        "activeTab": int(active_tab),
        "tabs": [
            {
                "label": f"Overview ({len(reports)} Active)",
                "content": "tab-overview",
            },
            {
                "label": f"Incidents Table ({len(all_table_rows)})",
                "content": "tab-directory",
            },
            {
                "label": "Sketch Gallery",
                "content": "tab-gallery",
            },
            {
                "label": "Analytics & SLA",
                "content": "tab-analytics",
            },
            {
                "label": "Dispatch New Issue",
                "content": "tab-dispatch",
            },
        ],
    })

    # -----------------------------------------------------------------------
    # Tab 0: Overview & Operations Dispatch
    # -----------------------------------------------------------------------
    components.append({
        "id": "tab-overview",
        "component": "MaterialColumn",
        "align": "stretch",
        "children": [
            "kpi-grid",
            "overview-divider",
            "quick-action-row-1",
            "quick-action-row-2",
            "diag-card",
        ],
    })

    components.append({
        "id": "kpi-grid",
        "component": "MaterialGridList",
        "cols": 3,
        "gutterSize": "16px",
        "children": ["kpi-card-1", "kpi-card-2", "kpi-card-3"],
    })

    # KPI 1: Active Incidents
    components.append({
        "id": "kpi-card-1",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["kpi-col-1"],
    })
    components.append({
        "id": "kpi-col-1",
        "component": "MaterialColumn",
        "children": ["kpi-title-1", "kpi-metric-row-1", "kpi-desc-1"],
    })
    components.append({
        "id": "kpi-title-1",
        "component": "MaterialText",
        "text": "Active Incidents",
        "usageHint": "subtitle2",
    })
    components.append({
        "id": "kpi-metric-row-1",
        "component": "MaterialRow",
        "justify": "spaceBetween",
        "align": "center",
        "children": ["kpi-count-1", "kpi-icon-1"],
    })
    components.append({
        "id": "kpi-count-1",
        "component": "MaterialText",
        "text": str(len(reports)),
        "usageHint": "h1",
    })
    components.append({
        "id": "kpi-icon-1",
        "component": "MaterialIcon",
        "icon": "build",
        "color": "warn",
    })
    components.append({
        "id": "kpi-desc-1",
        "component": "MaterialText",
        "text": f"{len(reports)} tickets awaiting dispatch",
        "usageHint": "caption",
    })

    # KPI 2: SLA Compliance
    components.append({
        "id": "kpi-card-2",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["kpi-col-2"],
    })
    components.append({
        "id": "kpi-col-2",
        "component": "MaterialColumn",
        "children": ["kpi-title-2", "kpi-metric-row-2", "kpi-progress-2", "kpi-desc-2"],
    })
    components.append({
        "id": "kpi-title-2",
        "component": "MaterialText",
        "text": "24-Hour SLA Target",
        "usageHint": "subtitle2",
    })
    components.append({
        "id": "kpi-metric-row-2",
        "component": "MaterialRow",
        "justify": "spaceBetween",
        "align": "center",
        "children": ["kpi-sla-val", "kpi-sla-icon"],
    })
    components.append({
        "id": "kpi-sla-val",
        "component": "MaterialText",
        "text": "92%",
        "usageHint": "h1",
    })
    components.append({
        "id": "kpi-sla-icon",
        "component": "MaterialIcon",
        "icon": "verified",
        "color": "primary",
    })
    components.append({
        "id": "kpi-progress-2",
        "component": "MaterialProgressBar",
        "value": 92,
        "color": "primary",
    })
    components.append({
        "id": "kpi-desc-2",
        "component": "MaterialText",
        "text": "Resolved within SLA target",
        "usageHint": "caption",
    })

    # KPI 3: Emergency Mode
    components.append({
        "id": "kpi-card-3",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["kpi-col-3"],
    })
    components.append({
        "id": "kpi-col-3",
        "component": "MaterialColumn",
        "children": ["kpi-title-3", "kpi-toggle-3", "kpi-desc-3"],
    })
    components.append({
        "id": "kpi-title-3",
        "component": "MaterialText",
        "text": "Emergency Response",
        "usageHint": "subtitle2",
    })
    components.append({
        "id": "kpi-toggle-3",
        "component": "MaterialSlideToggle",
        "label": "Critical Priority Lock",
        "checked": False,
    })
    components.append({
        "id": "kpi-desc-3",
        "component": "MaterialText",
        "text": "On-call response ready",
        "usageHint": "caption",
    })

    components.append({
        "id": "overview-divider",
        "component": "MaterialDivider",
    })

    # Quick Action Buttons
    components.append({
        "id": "quick-action-row-1",
        "component": "MaterialRow",
        "justify": "start",
        "align": "center",
        "children": ["btn-open-table", "btn-goto-dispatch"],
    })
    components.append({
        "id": "btn-open-table",
        "component": "MaterialButton",
        "label": "Browse Incidents Table",
        "variant": "primary",
        "leadingIcon": "table_chart",
        "action": {
            "event": {
                "name": "switchTab",
                "context": {"prompt": "Show the incidents table", "tab": 1, "active_tab": 1},
            }
        },
    })
    components.append({
        "id": "btn-goto-dispatch",
        "component": "MaterialButton",
        "label": "Dispatch New Incident",
        "variant": "stroked",
        "leadingIcon": "add_circle",
        "action": {
            "event": {
                "name": "switchTab",
                "context": {"prompt": "Open the new issue dispatch form", "tab": 4, "active_tab": 4},
            }
        },
    })

    components.append({
        "id": "quick-action-row-2",
        "component": "MaterialRow",
        "justify": "start",
        "align": "center",
        "children": ["btn-goto-gallery", "btn-dialog-demo"],
    })
    components.append({
        "id": "btn-goto-gallery",
        "component": "MaterialButton",
        "label": "Visual Sketch Gallery",
        "variant": "tonal",
        "leadingIcon": "photo_library",
        "action": {
            "event": {
                "name": "switchTab",
                "context": {"prompt": "Show visual sketches gallery", "tab": 2, "active_tab": 2},
            }
        },
    })
    components.append({
        "id": "btn-dialog-demo",
        "component": "MaterialButton",
        "label": "Dialog & Action Menu",
        "variant": "stroked",
        "leadingIcon": "open_in_new",
        "action": {
            "event": {
                "name": "showDialogDemo",
                "context": {"prompt": "Show modal dialog and menu demo"},
            }
        },
    })

    # Diagnostic Telemetry Monitor Card
    components.append({
        "id": "diag-card",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["diag-col"],
    })
    components.append({
        "id": "diag-col",
        "component": "MaterialColumn",
        "children": ["diag-title-row", "diag-desc"],
    })
    components.append({
        "id": "diag-title-row",
        "component": "MaterialRow",
        "align": "center",
        "children": ["diag-icon", "diag-title"],
    })
    components.append({
        "id": "diag-icon",
        "component": "MaterialIcon",
        "icon": "sensors",
        "color": "primary",
    })
    components.append({
        "id": "diag-title",
        "component": "MaterialText",
        "text": "IoT Building Telemetry & Sensors Active",
        "usageHint": "subtitle2",
    })
    components.append({
        "id": "diag-desc",
        "component": "MaterialText",
        "text": "Building sensors, HVAC controllers, and IoT gateways operating nominally in us-central1.",
        "usageHint": "caption",
    })

    # -----------------------------------------------------------------------
    # Tab 1: Incidents Directory (In-Place Native Category Sub-Tabs & Tables)
    # -----------------------------------------------------------------------
    components.append({
        "id": "tab-directory",
        "component": "MaterialColumn",
        "align": "stretch",
        "children": [
            "directory-toolbar-card",
            "directory-tabs",
            "audit-accordion",
        ],
    })

    # Toolbar Card: Header Title & Total Incidents Badge
    components.append({
        "id": "directory-toolbar-card",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["directory-toolbar-col"],
    })
    components.append({
        "id": "directory-toolbar-col",
        "component": "MaterialColumn",
        "children": [
            "directory-header-row",
        ],
    })
    components.append({
        "id": "directory-header-row",
        "component": "MaterialRow",
        "justify": "spaceBetween",
        "align": "center",
        "children": ["directory-title-row", "directory-count-row"],
    })
    components.append({
        "id": "directory-title-row",
        "component": "MaterialRow",
        "align": "center",
        "children": ["directory-table-icon", "directory-title-text"],
    })
    components.append({
        "id": "directory-table-icon",
        "component": "MaterialIcon",
        "icon": "table_chart",
        "color": "primary",
    })
    components.append({
        "id": "directory-title-text",
        "component": "MaterialText",
        "text": "Active Incidents Directory",
        "usageHint": "subtitle1",
    })
    # A plain icon + caption: MaterialBadge is a corner badge for a host
    # element and gets clipped at the card edge when used as a label.
    components.append({
        "id": "directory-count-row",
        "component": "MaterialRow",
        "align": "center",
        "children": ["directory-count-icon", "directory-count-text"],
    })
    components.append({
        "id": "directory-count-icon",
        "component": "MaterialIcon",
        "icon": "assignment",
        "color": "primary",
    })
    components.append({
        "id": "directory-count-text",
        "component": "MaterialText",
        "text": f"{len(all_table_rows)} Incidents",
        "usageHint": "caption",
    })

    # Category definitions for in-place client-side tab switching
    cat_tab_defs = [
        ("all", "All", f"All ({cat_counts['all']})", None),
        ("hvac", "HVAC", f"❄️ HVAC ({cat_counts['hvac']})", "hvac"),
        ("electrical", "Electrical", f"⚡ Electrical ({cat_counts['electrical']})", "electrical"),
        ("plumbing", "Plumbing", f"💧 Plumbing ({cat_counts['plumbing']})", "plumbing"),
        ("furniture", "Furniture", f"🪑 Furniture ({cat_counts['furniture']})", "furniture"),
        ("safety", "Safety", f"🛡️ Safety ({cat_counts['safety']})", "safety"),
    ]

    cat_keys = [c[0] for c in cat_tab_defs]
    active_cat_tab = cat_keys.index(cat_filter) if cat_filter in cat_keys else 0

    components.append({
        "id": "directory-tabs",
        "component": "MaterialTabs",
        "activeTab": active_cat_tab,
        "ariaLabel": "Filter Incidents by Category",
        "tabs": [
            {
                "label": tab_label,
                "content": f"cat-panel-{key}",
            }
            for key, label, tab_label, _ in cat_tab_defs
        ],
    })

    for key, label, tab_label, cat_key in cat_tab_defs:
        if cat_key is None:
            c_rows = list(all_table_rows)
        else:
            c_rows = [r for r in all_table_rows if r.get("category_key") == cat_key]

        if c_rows:
            f_rows = [
                {
                    "id": str(r["id"]),
                    "priority": str(r["priority"]),
                    "category": str(r["category"]),
                    "room": str(r["room"]),
                    "item": str(r["item"]),
                    "issue": str(r["issue"]),
                    "reporter": str(r["reporter"]),
                    "status": str(r["status"]),
                }
                for r in c_rows
            ]
            status_text = (
                f"Showing all {len(c_rows)} active incidents across all categories."
                if key == "all"
                else f"Showing {len(c_rows)} of {len(all_table_rows)} incidents • Category: {label.upper()}"
            )
        else:
            f_rows = [{
                "id": "-",
                "priority": "NOMINAL",
                "category": label,
                "room": "-",
                "item": "No active incidents",
                "issue": f"No incidents reported under {label}.",
                "reporter": "System",
                "status": "CLEAR",
            }]
            status_text = f"Showing 0 of {len(all_table_rows)} incidents • Category: {label.upper()} (All Clear)"

        components.append({
            "id": f"cat-panel-{key}",
            "component": "MaterialColumn",
            "align": "stretch",
            "children": [
                f"cat-status-{key}",
                f"cat-table-card-{key}",
            ],
        })
        components.append({
            "id": f"cat-status-{key}",
            "component": "MaterialText",
            "text": status_text,
            "usageHint": "caption",
        })
        components.append({
            "id": f"cat-table-card-{key}",
            "component": "MaterialCard",
            "appearance": "outlined",
            "children": [f"cat-table-{key}"],
        })
        components.append({
            "id": f"cat-table-{key}",
            "component": "MaterialTable",
            "ariaLabel": f"Facilities & Maintenance Incidents Table - {label}",
            "columns": [
                {"header": "# ID", "field": "id"},
                {"header": "Priority", "field": "priority"},
                {"header": "Category", "field": "category"},
                {"header": "Location", "field": "room"},
                {"header": "Equipment / Item", "field": "item"},
                {"header": "Issue Description", "field": "issue"},
                {"header": "Reporter", "field": "reporter"},
                {"header": "Status", "field": "status"},
            ],
            "rows": f_rows,
        })

    components.append({
        "id": "audit-accordion",
        "component": "MaterialExpansionPanel",
        "title": "Incident Resolution Audit Log & Diagnostics",
        "description": "Click to expand automated telemetry and dispatch traces",
        "children": ["audit-content-col"],
    })
    components.append({
        "id": "audit-content-col",
        "component": "MaterialColumn",
        "children": ["audit-log-1", "audit-log-2", "audit-log-3"],
    })
    components.append({
        "id": "audit-log-1",
        "component": "MaterialText",
        "text": "• [10:42 AM] Dispatch confirmed by Operations Lead.",
        "usageHint": "caption",
    })
    components.append({
        "id": "audit-log-2",
        "component": "MaterialText",
        "text": "• [10:30 AM] Photo-realistic incident sketch generated and cached in memory.",
        "usageHint": "caption",
    })
    components.append({
        "id": "audit-log-3",
        "component": "MaterialText",
        "text": "• [10:15 AM] Anomaly telemetry event detected by building IoT gateway Yellow-West.",
        "usageHint": "caption",
    })

    # -----------------------------------------------------------------------
    # Tab 2: Visual Incident Sketches (Pure Native MaterialGridList)
    # -----------------------------------------------------------------------
    components.append({
        "id": "tab-gallery",
        "component": "MaterialColumn",
        "align": "stretch",
        "children": ["gallery-info-card", "gallery-grid-list"],
    })

    components.append({
        "id": "gallery-info-card",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["gallery-info-col"],
    })
    components.append({
        "id": "gallery-info-col",
        "component": "MaterialColumn",
        "children": ["gallery-heading-row", "gallery-info-desc"],
    })
    components.append({
        "id": "gallery-heading-row",
        "component": "MaterialRow",
        "justify": "spaceBetween",
        "align": "center",
        "children": ["gallery-heading", "gallery-count-row"],
    })
    components.append({
        "id": "gallery-heading",
        "component": "MaterialText",
        "text": "Photo-Realistic Incident Sketches",
        "usageHint": "h3",
    })
    # Newest first, so a just-filed report leads the grid.
    gallery_reports = list(reversed(reports))[:GALLERY_MAX_TILES]
    components.append({
        "id": "gallery-count-row",
        "component": "MaterialRow",
        "align": "center",
        "children": ["gallery-count-icon", "gallery-count-text"],
    })
    components.append({
        "id": "gallery-count-icon",
        "component": "MaterialIcon",
        "icon": "photo_library",
        "color": "primary",
    })
    components.append({
        "id": "gallery-count-text",
        "component": "MaterialText",
        "text": f"{len(gallery_reports)} Sketches",
        "usageHint": "caption",
    })
    components.append({
        "id": "gallery-info-desc",
        "component": "MaterialText",
        "text": (
            f"The {len(gallery_reports)} most recent sketches, newest first; the Incidents Table "
            f"lists all {len(reports)} reports. The agent itself serves the images inline as "
            "compressed Base64 data URIs, so no image hosting is needed."
        ),
        "usageHint": "caption",
    })

    grid_tile_ids = []
    for i, r in enumerate(gallery_reports):
        tile_card_id = f"tile-card-{i}"
        tile_col_id = f"tile-col-{i}"
        tile_img_id = f"tile-img-{i}"
        tile_title_id = f"tile-title-{i}"
        tile_desc_id = f"tile-desc-{i}"
        grid_tile_ids.append(tile_card_id)

        image_blob = r.get("image_blob")
        img_url = _get_image_data_uri(image_blob) if image_blob else _get_default_sketch_data_uri()

        components.append({
            "id": tile_card_id,
            "component": "MaterialCard",
            "appearance": "outlined",
            "children": [tile_col_id],
        })
        components.append({
            "id": tile_col_id,
            "component": "MaterialColumn",
            "align": "stretch",
            "children": [tile_img_id, tile_title_id, tile_desc_id],
        })
        components.append({
            "id": tile_img_id,
            "component": "Image",
            "url": img_url,
            "description": f"Sketch of {r.get('item', 'Equipment')}",
            "fit": "cover",
        })
        components.append({
            "id": tile_title_id,
            "component": "MaterialText",
            "text": f"{r.get('room', 'Unknown')} • {r.get('item', 'Item')}",
            "usageHint": "subtitle2",
        })
        components.append({
            "id": tile_desc_id,
            "component": "MaterialText",
            "text": str(r.get("issue", ""))[:75] + ("..." if len(str(r.get("issue", ""))) > 75 else ""),
            "usageHint": "caption",
        })

    components.append({
        "id": "gallery-grid-list",
        "component": "MaterialGridList",
        "cols": 3,
        "rowHeight": "1:1",
        "gutterSize": "12px",
        "children": grid_tile_ids,
    })

    # -----------------------------------------------------------------------
    # Tab 3: Analytics & SLA Dashboard (VegaChart & MaterialSlider)
    # -----------------------------------------------------------------------
    components.append({
        "id": "tab-analytics",
        "component": "MaterialColumn",
        "align": "stretch",
        "children": ["chart-card", "slider-card", "category-progress-card"],
    })

    components.append({
        "id": "chart-card",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["chart-title", "chart-component"],
    })
    components.append({
        "id": "chart-title",
        "component": "MaterialText",
        "text": "Monthly Incident Volume & SLA Resolution by Category",
        "usageHint": "h3",
    })
    components.append({
        "id": "chart-component",
        "component": "VegaChart",
        "spec": {"path": "/chart_spec"},
        "height": 280,
    })

    components.append({
        "id": "slider-card",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["slider-title", "sla-slider", "slider-caption"],
    })
    components.append({
        "id": "slider-title",
        "component": "MaterialText",
        "text": "Interactive SLA Target Simulation (Hours to Resolution)",
        "usageHint": "subtitle1",
    })
    components.append({
        "id": "sla-slider",
        "component": "MaterialSlider",
        "min": 4,
        "max": 48,
        "step": 2,
        "value": 24,
    })
    components.append({
        "id": "slider-caption",
        "component": "MaterialText",
        "text": "Simulate operational on-call technician capacity for SLA thresholds between 4h and 48h.",
        "usageHint": "caption",
    })

    components.append({
        "id": "category-progress-card",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["cat-title", "cat-efficiency-row", "prog-hvac", "prog-elec", "prog-plumb"],
    })
    components.append({
        "id": "cat-title",
        "component": "MaterialText",
        "text": "Category Resolution Compliance Rates",
        "usageHint": "subtitle2",
    })
    components.append({
        "id": "cat-efficiency-row",
        "component": "MaterialRow",
        "align": "center",
        "children": ["cat-spinner", "cat-efficiency-text"],
    })
    components.append({
        "id": "cat-spinner",
        "component": "MaterialProgressSpinner",
        "mode": "determinate",
        "value": 94,
        "diameter": 28,
        "strokeWidth": 4,
        "color": "primary",
    })
    components.append({
        "id": "cat-efficiency-text",
        "component": "MaterialText",
        "text": "Overall Telemetry & Resolution Health: 94% Nominal",
        "usageHint": "caption",
    })
    components.append({
        "id": "prog-hvac",
        "component": "MaterialProgressBar",
        "value": 78,
        "color": "primary",
    })
    components.append({
        "id": "prog-elec",
        "component": "MaterialProgressBar",
        "value": 94,
        "color": "primary",
    })
    components.append({
        "id": "prog-plumb",
        "component": "MaterialProgressBar",
        "value": 65,
        "color": "warn",
    })

    # -----------------------------------------------------------------------
    # Tab 4: Dispatch New Issue Form (All Material Controls)
    # -----------------------------------------------------------------------
    components.append({
        "id": "tab-dispatch",
        "component": "MaterialColumn",
        "align": "stretch",
        "children": ["form-card"],
    })
    components.append({
        "id": "form-card",
        "component": "MaterialCard",
        "appearance": "outlined",
        "children": ["form-col"],
    })
    components.append({
        "id": "form-col",
        "component": "MaterialColumn",
        "children": [
            "form-title",
            "form-room-select",
            "form-item-select",
            "form-issue-input",
            "form-reporter-row",
            "form-radio-label",
            "form-priority-radio",
            "form-date-row",
            "form-checkbox-row",
            "form-chips-label",
            "form-chips",
            "form-submit-btn",
        ],
    })
    components.append({
        "id": "form-title",
        "component": "MaterialText",
        "text": "Log Maintenance Incident / Dispatch Work Order",
        "usageHint": "h3",
    })
    components.append({
        "id": "form-room-select",
        "component": "MaterialSelect",
        "label": "Affected Location / Zone",
        "options": [
            {"label": "🏢 Yellow Room", "value": "Yellow Room"},
            {"label": "🐕 Big Dog", "value": "Big Dog"},
            {"label": "🐕 Little Dog", "value": "Little Dog"},
            {"label": "🐠 Fish Tank", "value": "Fish Tank"},
            {"label": "🌊 Ocean", "value": "Ocean"},
            {"label": "🌲 Forest", "value": "Forest"},
        ],
        "value": _form_binding("room"),
    })
    components.append({
        "id": "form-item-select",
        "component": "MaterialSelect",
        "label": "Equipment or Furniture Item",
        "options": [
            {"label": "🪑 Chair", "value": "Chair"},
            {"label": "🪟 Window", "value": "Window"},
            {"label": "⬜ Whiteboard", "value": "Whiteboard"},
            {"label": "💡 Light", "value": "Light"},
            {"label": "🔌 Power Outlet", "value": "Power Outlet"},
            {"label": "🚰 Sink", "value": "Sink"},
            {"label": "🚪 Door", "value": "Door"},
            {"label": "🖥 Desk", "value": "Desk"},
        ],
        "value": _form_binding("item"),
    })
    components.append({
        "id": "form-issue-input",
        "component": "MaterialInput",
        "label": "Detailed Issue Description",
        "placeholder": "Describe the defect or malfunction",
        "value": _form_binding("issue"),
    })
    components.append({
        "id": "form-reporter-row",
        "component": "MaterialRow",
        "children": ["form-reporter-name", "form-reporter-email"],
    })
    components.append({
        "id": "form-reporter-name",
        "component": "MaterialInput",
        "label": "Reporter Name (Optional)",
        "placeholder": "e.g. Alex Chen",
        "value": _form_binding("reporter_name"),
    })
    components.append({
        "id": "form-reporter-email",
        "component": "MaterialInput",
        "label": "Reporter Email (Optional)",
        "placeholder": "e.g. alex@example.com",
        "value": _form_binding("reporter_email"),
    })
    components.append({
        "id": "form-radio-label",
        "component": "MaterialText",
        "text": "Incident Priority Level",
        "usageHint": "subtitle2",
    })
    components.append({
        "id": "form-priority-radio",
        "component": "MaterialRadioButton",
        "options": [
            {"label": "Low (48h)", "value": "low"},
            {"label": "Medium (24h)", "value": "med"},
            {"label": "High (8h)", "value": "high"},
            {"label": "Emergency (1h)", "value": "emergency"},
        ],
        "value": _form_binding("priority"),
    })
    components.append({
        "id": "form-date-row",
        "component": "MaterialRow",
        "children": ["form-datepicker", "form-timepicker"],
    })
    components.append({
        "id": "form-datepicker",
        "component": "MaterialDatepicker",
        "label": "Preferred Service Date",
        "value": _form_binding("service_date"),
    })
    components.append({
        "id": "form-timepicker",
        "component": "MaterialTimepicker",
        "label": "Service Time Window",
        "value": _form_binding("service_time"),
    })
    components.append({
        "id": "form-checkbox-row",
        "component": "MaterialRow",
        "children": ["form-check-director", "form-check-parts"],
    })
    components.append({
        "id": "form-check-director",
        "component": "MaterialCheckbox",
        "label": "Notify Facilities Director",
        "checked": _form_binding("notify_director"),
    })
    components.append({
        "id": "form-check-parts",
        "component": "MaterialCheckbox",
        "label": "Auto-Order Replacement Parts",
        "checked": _form_binding("order_parts"),
    })
    components.append({
        "id": "form-chips-label",
        "component": "MaterialText",
        "text": "Category Classification (optional, auto-detected if none is selected)",
        "usageHint": "subtitle2",
    })
    components.append({
        "id": "form-chips",
        "component": "MaterialChips",
        "options": [
            {"label": "Furniture", "value": "furniture"},
            {"label": "HVAC", "value": "hvac"},
            {"label": "Electrical", "value": "electrical"},
            {"label": "Plumbing", "value": "plumbing"},
            {"label": "Safety", "value": "safety"},
        ],
        "value": _form_binding("category"),
    })
    components.append({
        "id": "form-submit-btn",
        "component": "MaterialButton",
        "label": "Submit Incident & Generate Visual Sketch",
        "variant": "primary",
        "leadingIcon": "send",
        "action": {
            "event": {
                "name": "submitReportQuick",
                # The client resolves each path at click time, so the agent
                # receives the values currently shown in the form.
                "context": {
                    "prompt": "File a maintenance report with the values entered in the dispatch form",
                    "room": _form_binding("room"),
                    "item": _form_binding("item"),
                    "issue": _form_binding("issue"),
                    "reporter_name": _form_binding("reporter_name"),
                    "reporter_email": _form_binding("reporter_email"),
                    "priority": _form_binding("priority"),
                    "category": _form_binding("category"),
                },
            }
        },
    })

    return [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": COMPOSITE_CATALOG_ID,
            },
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": surface_id,
                "components": components,
            },
        },
        {
            "version": "v0.9",
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/chart_spec",
                "value": VEGA_LITE_INCIDENTS_SPEC,
            },
        },
        {
            "version": "v0.9",
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": REPORT_FORM_PATH,
                "value": _report_form_defaults(),
            },
        },
    ]


# ---------------------------------------------------------------------------
# Dialog & Menu Demo Surface Builder (A2UI v0.9)
# ---------------------------------------------------------------------------

DIALOG_DEMO_PREFIX = "dialog-demo"
_DIALOG_DEMO_ID_PATTERN = re.compile(r"^dialog-demo-[0-9a-f]{10}$")
# Session-state key with the showcase cards rendered in this chat (newest
# last), so dialog and menu actions can update the card they came from.
DIALOG_DEMO_SURFACES_KEY = "dialog_demo_surface_ids"
_MAX_REMEMBERED_DIALOG_DEMOS = 20

DIALOG_DEMO_INITIAL_STATUS = "Incident #1 is open and waiting for a decision."
ESCALATION_STATUS = {
    "confirm": "Escalation confirmed: Incident #1 was sent to Emergency Hazmat (simulated).",
    "cancel": "Escalation cancelled: Incident #1 stays in the regular maintenance queue.",
}
TICKET_ACTION_LABELS = {
    "reassign": "Reassign Technician",
    "export": "Export Incident PDF",
    "duplicate": "Duplicate Ticket",
}


def _dialog_demo_state(status: str) -> dict:
    """Data model of the showcase card: dialog closed, no menu choice, status line."""
    return {"dialogOpen": False, "ticketAction": "", "status": status}


def build_dialog_demo_surface(status: str = DIALOG_DEMO_INITIAL_STATUS) -> list[dict]:
    """Build an A2UI v0.9 surface demonstrating MaterialDialog and MaterialMenu.

    The dialog starts closed and its `trigger` button opens it in the browser,
    so reopening the chat never pops it up again. The dialog buttons and the
    menu send actions that carry this card's surface id. The agent answers with
    an `updateDataModel` for this same card: that closes the dialog (`open` is
    bound to `/demo/dialogOpen`) and updates the status line.
    """
    surface_id = _sid(DIALOG_DEMO_PREFIX)
    components = [
        {
            "id": "root",
            "component": "MaterialCard",
            "appearance": "raised",
            "children": ["dialog-demo-col"],
        },
        {
            "id": "dialog-demo-col",
            "component": "MaterialColumn",
            "align": "stretch",
            "children": [
                "demo-title",
                "demo-desc",
                "demo-status-row",
                "dialog-title",
                "dialog-sample",
                "menu-title",
                "menu-sample",
                "back-btn",
            ],
        },
        {
            "id": "demo-title",
            "component": "MaterialText",
            "text": "A2UI v0.9 Dialogs & Action Menus Showcase",
            "usageHint": "h2",
        },
        {
            "id": "demo-desc",
            "component": "MaterialText",
            "text": (
                "The dialog opens in your browser from its trigger button. Its buttons and the "
                "menu send your choice to the agent, which updates this card."
            ),
            "usageHint": "body",
        },
        {
            "id": "demo-status-row",
            "component": "MaterialRow",
            "align": "center",
            "children": ["demo-status-icon", "demo-status-text"],
        },
        {
            "id": "demo-status-icon",
            "component": "MaterialIcon",
            "icon": "info",
            "color": "primary",
        },
        {
            "id": "demo-status-text",
            "component": "MaterialText",
            "text": {"path": "/demo/status"},
            "usageHint": "body",
        },
        {
            "id": "dialog-title",
            "component": "MaterialText",
            "text": "Confirmation Dialog",
            "usageHint": "subtitle1",
        },
        {
            "id": "dialog-sample",
            "component": "MaterialDialog",
            "title": "Escalate Incident #1 to Emergency Hazmat?",
            "trigger": "dialog-trigger-btn",
            "open": {"path": "/demo/dialogOpen"},
            "children": ["dialog-body-col"],
        },
        {
            # Rendered by the dialog as its trigger. No action: clicking it only
            # opens the dialog, without a round trip to the agent.
            "id": "dialog-trigger-btn",
            "component": "MaterialButton",
            "label": "Escalate Incident #1",
            "variant": "flat",
            "color": "warn",
            "leadingIcon": "warning",
        },
        {
            "id": "dialog-body-col",
            "component": "MaterialColumn",
            "children": ["dialog-text", "dialog-actions-row"],
        },
        {
            "id": "dialog-text",
            "component": "MaterialText",
            "text": "Escalating this ticket will immediately notify the Incident Commander and dispatch an emergency crew.",
            "usageHint": "body",
        },
        {
            "id": "dialog-actions-row",
            "component": "MaterialRow",
            "justify": "end",
            "children": ["dialog-cancel-btn", "dialog-confirm-btn"],
        },
        {
            "id": "dialog-cancel-btn",
            "component": "MaterialButton",
            "label": "Cancel",
            "variant": "flat",
            "action": {
                "event": {
                    "name": "resolveEscalation",
                    "context": {"prompt": "Cancel the escalation", "decision": "cancel", "surface_id": surface_id},
                }
            },
        },
        {
            "id": "dialog-confirm-btn",
            "component": "MaterialButton",
            "label": "Confirm Escalation",
            "variant": "primary",
            "action": {
                "event": {
                    "name": "resolveEscalation",
                    "context": {"prompt": "Confirm the escalation", "decision": "confirm", "surface_id": surface_id},
                }
            },
        },
        {
            "id": "menu-title",
            "component": "MaterialText",
            "text": "Context Action Menu",
            "usageHint": "subtitle1",
        },
        {
            "id": "menu-sample",
            "component": "MaterialMenu",
            "label": "Ticket Actions",
            "icon": "more_vert",
            "value": {"path": "/demo/ticketAction"},
            "options": [{"label": label, "value": value} for value, label in TICKET_ACTION_LABELS.items()],
            "action": {
                "event": {
                    "name": "ticketAction",
                    "context": {
                        "prompt": "Run the selected ticket action",
                        "ticket_action": {"path": "/demo/ticketAction"},
                        "surface_id": surface_id,
                    },
                }
            },
        },
        {
            "id": "back-btn",
            "component": "MaterialButton",
            "label": "Return to Operations Center",
            "variant": "stroked",
            "leadingIcon": "arrow_back",
            "action": {
                "event": {
                    "name": "switchTab",
                    "context": {"prompt": "Return to the operations control center", "tab": 0, "active_tab": 0},
                }
            },
        },
    ]

    return [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": COMPOSITE_CATALOG_ID,
            },
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": surface_id,
                "components": components,
            },
        },
        {
            "version": "v0.9",
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/demo",
                "value": _dialog_demo_state(status),
            },
        },
    ]


def build_dialog_demo_update(surface_id: str, status: str) -> list[dict]:
    """Update an existing showcase card: close its dialog and show `status`."""
    return [
        {
            "version": "v0.9",
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/demo",
                "value": _dialog_demo_state(status),
            },
        }
    ]


def _remembered_dialog_demos(tool_context: Any) -> list[str]:
    try:
        ids = tool_context.state.get(DIALOG_DEMO_SURFACES_KEY)
    except Exception:  # noqa: BLE001 - tolerate contexts without session state
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def _remember_dialog_demo(tool_context: Any, surface_id: str) -> None:
    ids = [*_remembered_dialog_demos(tool_context), surface_id][-_MAX_REMEMBERED_DIALOG_DEMOS:]
    try:
        tool_context.state[DIALOG_DEMO_SURFACES_KEY] = ids
    except Exception:  # noqa: BLE001 - tolerate contexts without session state
        logger.debug("Could not remember dialog demo surface %s", surface_id)


def _resolve_dialog_demo_surface(tool_context: Any, surface_id: str) -> str | None:
    """Pick the showcase card an action came from.

    Prefer the id from the action context when this chat rendered it, else the
    newest card rendered in this chat. A well-formed id is still accepted when
    the session was lost (for example after a Cloud Run restart).
    """
    surface_id = str(surface_id or "").strip()
    known = _remembered_dialog_demos(tool_context)
    if surface_id in known:
        return surface_id
    if known:
        return known[-1]
    if _DIALOG_DEMO_ID_PATTERN.match(surface_id):
        return surface_id
    return None


def _update_or_rerender_dialog_demo(tool_context: Any, surface_id: str, status: str) -> list[dict]:
    target = _resolve_dialog_demo_surface(tool_context, surface_id)
    if target:
        return build_dialog_demo_update(target, status)
    # No card to update: render a fresh showcase that shows the result.
    surface = build_dialog_demo_surface(status)
    _remember_dialog_demo(tool_context, surface[0]["createSurface"]["surfaceId"])
    return surface


# ---------------------------------------------------------------------------
# Report Confirmation Surface Builder (A2UI v0.9)
# ---------------------------------------------------------------------------


def build_report_confirmation_surface(
    room: str,
    item: str,
    issue: str,
    image_url: str | None = None,
) -> list[dict]:
    """Build a flat, validated A2UI v0.9 confirmation surface."""
    surface_id = _sid("report-confirmation")
    summary = f"{room} — {item}: {issue}".strip(" —:")

    content_children = ["confirm-icon-row", "confirm-summary", "confirm-details-row"]
    if image_url:
        content_children.append("confirm-sketch")
    content_children.append("confirm-actions-row")

    components: list[dict] = [
        {
            "id": "root",
            "component": "MaterialCard",
            "appearance": "raised",
            "children": ["confirm-col"],
        },
        {
            "id": "confirm-col",
            "component": "MaterialColumn",
            "align": "stretch",
            "children": content_children,
        },
        {
            "id": "confirm-icon-row",
            "component": "MaterialRow",
            "align": "center",
            "children": ["confirm-icon", "confirm-heading"],
        },
        {
            "id": "confirm-icon",
            "component": "MaterialIcon",
            "icon": "check_circle",
            "color": "primary",
        },
        {
            "id": "confirm-heading",
            "component": "MaterialText",
            "text": "Maintenance Work Order Filed Successfully",
            "usageHint": "h2",
        },
        {
            "id": "confirm-summary",
            "component": "MaterialText",
            "text": summary,
            "usageHint": "subtitle1",
        },
        {
            "id": "confirm-details-row",
            "component": "MaterialRow",
            "align": "center",
            "children": ["confirm-loc-icon", "confirm-loc-text", "confirm-item-icon", "confirm-item-text"],
        },
        {
            "id": "confirm-loc-icon",
            "component": "MaterialIcon",
            "icon": "place",
            "color": "primary",
        },
        {
            "id": "confirm-loc-text",
            "component": "MaterialText",
            "text": f"Location: {room or 'Unspecified'}",
            "usageHint": "body",
        },
        {
            "id": "confirm-item-icon",
            "component": "MaterialIcon",
            "icon": "build",
            "color": "accent",
        },
        {
            "id": "confirm-item-text",
            "component": "MaterialText",
            "text": f"Equipment: {item or 'General'}",
            "usageHint": "body",
        },
    ]

    if image_url:
        components.append({
            "id": "confirm-sketch",
            "component": "Image",
            "url": image_url,
            "description": f"Sketch for {room} {item}",
            "fit": "cover",
        })

    components.append({
        "id": "confirm-actions-row",
        "component": "MaterialRow",
        "justify": "start",
        "children": ["btn-view-table-after", "btn-file-another"],
    })
    components.append({
        "id": "btn-view-table-after",
        "component": "MaterialButton",
        "label": "View Incidents Table",
        "variant": "primary",
        "leadingIcon": "table_chart",
        "action": {
            "event": {
                "name": "switchTab",
                "context": {"prompt": "Show the incidents table", "tab": 1, "active_tab": 1},
            }
        },
    })
    components.append({
        "id": "btn-file-another",
        "component": "MaterialButton",
        "label": "File Another Incident",
        "variant": "stroked",
        "leadingIcon": "add",
        "action": {
            "event": {
                "name": "switchTab",
                "context": {"prompt": "Log another maintenance issue", "tab": 4, "active_tab": 4},
            }
        },
    })

    return [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": COMPOSITE_CATALOG_ID,
            },
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": surface_id,
                "components": components,
            },
        },
    ]


# ---------------------------------------------------------------------------
# ADK Tool Callables (invoked by the LLM agent)
# ---------------------------------------------------------------------------


def show_tabbed_control_center(
    tool_context: Any,
    active_tab: int = 0,
    category_filter: str = "all",
) -> dict:
    """Render the 5-Tab A2UI 0.9 Operations & Incident Control Center surface.

    Args:
        tool_context: ADK tool context.
        active_tab: The index of the tab to focus (0=Overview, 1=Table,
                    2=Gallery, 3=Analytics, 4=Dispatch Form).
        category_filter: Optional category filter for Tab 1 ('all', 'hvac',
                         'electrical', 'plumbing', 'furniture', 'safety').
    """
    tool_context.actions.skip_summarization = True
    return {
        VALIDATED_A2UI_JSON_KEY: build_tabbed_control_center_surface(
            int(active_tab), str(category_filter or "all")
        )
    }


def show_welcome(tool_context: Any) -> dict:
    """Render the welcome A2UI 0.9 Operations Control Center dashboard (Tab 0)."""
    tool_context.actions.skip_summarization = True
    return {VALIDATED_A2UI_JSON_KEY: build_tabbed_control_center_surface(0)}


def show_reports_list(tool_context: Any, category_filter: str = "all") -> dict:
    """Render the A2UI 0.9 Control Center focused on Tab 1 (Incidents Table)."""
    tool_context.actions.skip_summarization = True
    return {
        VALIDATED_A2UI_JSON_KEY: build_tabbed_control_center_surface(
            1, str(category_filter or "all")
        )
    }


def show_gallery(tool_context: Any) -> dict:
    """Render the A2UI 0.9 Visual Sketch Gallery (Tab 2)."""
    tool_context.actions.skip_summarization = True
    return {VALIDATED_A2UI_JSON_KEY: build_tabbed_control_center_surface(2)}


def show_analytics(tool_context: Any) -> dict:
    """Render the A2UI 0.9 Analytics & SLA VegaChart Dashboard (Tab 3)."""
    tool_context.actions.skip_summarization = True
    return {VALIDATED_A2UI_JSON_KEY: build_tabbed_control_center_surface(3)}


def show_report_form(tool_context: Any) -> dict:
    """Render the A2UI 0.9 Dispatch & Service Form (Tab 4)."""
    tool_context.actions.skip_summarization = True
    return {VALIDATED_A2UI_JSON_KEY: build_tabbed_control_center_surface(4)}


def show_dialog_demo(tool_context: Any) -> dict:
    """Render the A2UI 0.9 MaterialDialog and MaterialMenu showcase surface."""
    tool_context.actions.skip_summarization = True
    surface = build_dialog_demo_surface()
    _remember_dialog_demo(tool_context, surface[0]["createSurface"]["surfaceId"])
    return {VALIDATED_A2UI_JSON_KEY: surface}


def resolve_escalation(tool_context: Any, decision: str, surface_id: str = "") -> dict:
    """Close the showcase's escalation dialog and show the decision on that same card.

    Args:
        tool_context: ADK tool context.
        decision: "confirm" or "cancel", from the `resolveEscalation` action context.
        surface_id: The `surface_id` from the action context.
    """
    tool_context.actions.skip_summarization = True
    key = "confirm" if str(decision or "").strip().lower().startswith("confirm") else "cancel"
    return {
        VALIDATED_A2UI_JSON_KEY: _update_or_rerender_dialog_demo(
            tool_context, surface_id, ESCALATION_STATUS[key]
        )
    }


def run_ticket_action(tool_context: Any, ticket_action: str, surface_id: str = "") -> dict:
    """Show the "Ticket Actions" menu choice on the showcase card it came from.

    Args:
        tool_context: ADK tool context.
        ticket_action: The chosen option ("reassign", "export" or "duplicate"),
            from the `ticketAction` action context.
        surface_id: The `surface_id` from the action context.
    """
    tool_context.actions.skip_summarization = True
    value = str(ticket_action or "").strip()
    label = TICKET_ACTION_LABELS.get(value.lower()) or next(
        (lbl for lbl in TICKET_ACTION_LABELS.values() if lbl.lower() == value.lower()), None
    )
    status = (
        f"Ticket action received: {label} for Incident #1 (simulated)."
        if label
        else "No ticket action was selected. Pick one from the Ticket Actions menu."
    )
    return {VALIDATED_A2UI_JSON_KEY: _update_or_rerender_dialog_demo(tool_context, surface_id, status)}


def show_report_confirmation(
    tool_context: Any,
    room: str,
    item: str,
    issue: str,
    image_url: str = "",
) -> dict:
    """Render the standalone A2UI 0.9 confirmation card."""
    tool_context.actions.skip_summarization = True
    return {VALIDATED_A2UI_JSON_KEY: build_report_confirmation_surface(
        room, item, issue, image_url or None
    )}


_submit_lock = threading.Lock()
_recent_reports: dict[str, tuple[float, dict]] = {}
_DEDUP_WINDOW_SECONDS = 120

# Upper bounds for free-text report fields. They keep image-generation prompts,
# the stored reports and every later dashboard render small.
MAX_SHORT_FIELD_CHARS = 80
MAX_EMAIL_CHARS = 120
MAX_ISSUE_CHARS = 500


def _clean_text(value: Any, max_chars: int) -> str:
    """Strip a free-text field and cut it to at most `max_chars` characters."""
    return str(value or "").strip()[:max_chars].strip()


def _dedup_key(
    room: str,
    item: str,
    issue: str,
    reporter_name: str,
    priority: str = "",
    category: str = "",
) -> str:
    return "|".join((
        room or "", item or "", issue or "", reporter_name or "", priority or "", category or ""
    )).strip().lower()


def file_report(
    tool_context: Any,
    room: str,
    item: str,
    issue: str,
    reporter_name: str = "",
    reporter_email: str = "",
    priority: str = "",
    category: str = "",
) -> dict:
    """File a maintenance report in ONE call, then render the confirmation.

    Generates or selects the sketch image, saves the report, and returns
    the confirmation surface. Call it EXACTLY ONCE per submission.

    For a `submitReportQuick` form submission, pass the values from the
    action's `context` exactly as received.

    Args:
        room: Affected room or zone, e.g. "Ocean".
        item: Affected equipment or furniture item, e.g. "Window".
        issue: What is wrong, in the user's words.
        reporter_name: Optional reporter name.
        reporter_email: Optional reporter email.
        priority: Optional priority: "low", "med", "high" or "emergency".
        category: Optional category: "hvac", "electrical", "plumbing",
            "furniture" or "safety". Leave empty to auto-detect.
    """
    room = _clean_text(room, MAX_SHORT_FIELD_CHARS)
    item = _clean_text(item, MAX_SHORT_FIELD_CHARS)
    issue = _clean_text(issue, MAX_ISSUE_CHARS)
    reporter_name = _clean_text(reporter_name, MAX_SHORT_FIELD_CHARS)
    reporter_email = _clean_text(reporter_email, MAX_EMAIL_CHARS)
    missing = [
        label
        for label, value in (("room", room), ("item", item), ("issue description", issue))
        if not value
    ]
    if missing:
        # No skip_summarization: the agent tells the user what to fill in.
        return {
            "status": "error",
            "message": (
                f"The report was not filed. Please provide the {', '.join(missing)} "
                "and submit again."
            ),
        }
    priority_level = _normalize_priority(priority)
    category_label = _normalize_category(category)

    key = _dedup_key(room, item, issue, reporter_name, priority_level, category_label)
    with _submit_lock:
        now = time.monotonic()
        for stale in [k for k, (t, _) in _recent_reports.items()
                      if now - t > _DEDUP_WINDOW_SECONDS]:
            _recent_reports.pop(stale, None)

        cached = _recent_reports.get(key)
        if cached:
            logger.warning(
                "Duplicate file_report for %r within %ss ignored; "
                "returning the prior confirmation.", key, _DEDUP_WINDOW_SECONDS
            )
            tool_context.actions.skip_summarization = True
            return cached[1]

        sketch = generate_visual_sketch(room=room, item=item, issue=issue)
        image_blob = sketch.get("image_blob", "") if isinstance(sketch, dict) else ""
        image_url = sketch.get("image_url") if isinstance(sketch, dict) else None

        submit_maintenance_report(
            tool_context,
            issue=issue,
            room=room,
            item=item,
            reporter_name=reporter_name,
            reporter_email=reporter_email,
            image_blob=image_blob or "",
            priority=priority_level,
            category=category_label,
        )

        result = {
            VALIDATED_A2UI_JSON_KEY: build_report_confirmation_surface(
                room, item, issue, image_url
            )
        }
        _recent_reports[key] = (now, result)
        tool_context.actions.skip_summarization = True
        return result
