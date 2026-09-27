"""Operations & Maintenance Incident Report Agent with Native A2UI v0.9 for Google ADK.

Demonstrates all pure native A2UI v0.9 features including Material 3 components,
dynamic MaterialTable, responsive MaterialGridList, Vega-Lite charts, and visual sketches.
"""

import base64
import logging
import os
from typing import ClassVar

from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2ui.a2a.extension import get_a2ui_agent_extension
from a2ui.adk.send_a2ui_to_client_toolset import (
    SendA2uiToClientToolset,
)
from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.catalog import CatalogConfig
from a2ui.schema.catalog_provider import FileSystemCatalogProvider
from a2ui.schema.common_modifiers import remove_strict_validation
from a2ui.schema.manager import A2uiSchemaManager
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.app_utils import a2ui_payload_patch
from app.config import (
    COMPOSITE_CATALOG_ID,
    DEFAULT_MODEL,
    build_vertex_model_name,
)
from app.render_tools import (
    VALIDATED_A2UI_JSON_KEY,
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
from app.session_keys import (
    A2UI_CATALOG_KEY,
    A2UI_ENABLED_KEY,
    A2UI_EXAMPLES_KEY,
    rehydrate_catalog,
)

logger = logging.getLogger(__name__)

# Harden the A2UI payload parser against malformed JSON
a2ui_payload_patch.install()

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_DEFINITION_JSON_V0_9 = os.path.join(
    _APP_DIR, "catalog_schemas", "0.9", "gemini_enterprise_composite_catalog.json"
)
if not os.path.exists(CATALOG_DEFINITION_JSON_V0_9):
    CATALOG_DEFINITION_JSON_V0_9 = os.path.join(
        _APP_DIR, "catalog_schemas", "0.9", "report_catalog_definition.json"
    )

AGENT_ICON_PATH = os.path.join(_APP_DIR, "assets", "agent_icon.png")


def agent_icon_data_uri() -> str | None:
    """Return the bundled agent icon as a data URI (no external image hosting)."""
    try:
        with open(AGENT_ICON_PATH, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except OSError:
        logger.warning("Agent icon not found at %s", AGENT_ICON_PATH)
        return None


ROLE_DESCRIPTION = """
You are the Facilities & Operations Incident Report Agent powered by native A2UI v0.9 on Google ADK.
You help users file, browse, inspect visual sketch galleries, simulate SLA forecasts, and resolve maintenance tickets.

RENDERING — IMPORTANT:
- All rich UI is rendered server-side by dedicated tools that produce validated A2UI v0.9 payloads:
  - `show_tabbed_control_center(active_tab=0, category_filter="all")` — renders the 5-tab Operations & Incidents Control Center dashboard:
    - Tab 0 (`active_tab=0`): Overview & Dispatch (KPI Badges, Progress Bars, Emergency Slide Toggle, Quick Action Buttons).
    - Tab 1 (`active_tab=1`, `category_filter="all"|"hvac"|"electrical"|"plumbing"|"furniture"|"safety"`): Incidents Directory (Sortable & Filterable native `MaterialTable` with category tags, interactive category filter chips).
    - Tab 2 (`active_tab=2`): Visual Sketch Gallery (Responsive native `MaterialGridList` displaying 16:9 inspection sketches).
    - Tab 3 (`active_tab=3`): Analytics & SLA Dashboard (`VegaChart` dynamic bar chart, interactive `MaterialSlider` simulation).
    - Tab 4 (`active_tab=4`): Dispatch & Service Form (`MaterialSelect`, `MaterialInput`, `MaterialRadioButton`, `MaterialCheckbox`, `MaterialDatepicker`, `MaterialTimepicker`, `MaterialChips`).
  - `show_welcome` — opens Tab 0 (Overview).
  - `show_reports_list(category_filter="all")` — opens Tab 1 (Table), optionally filtered by category (`hvac`, `electrical`, `plumbing`, `furniture`, `safety`).
  - `show_gallery` — opens Tab 2 (Visual Sketch Gallery).
  - `show_analytics` — opens Tab 3 (Analytics).
  - `show_report_form` — opens Tab 4 (Form).
  - `show_dialog_demo` — opens the `MaterialDialog` & `MaterialMenu` modal demonstration.
  - `resolve_escalation(decision, surface_id)` — handles Confirm/Cancel in that demo's escalation dialog: closes the dialog and shows the decision on the same card.
  - `run_ticket_action(ticket_action, surface_id)` — handles a choice from that demo's "Ticket Actions" menu and shows it on the same card.
- You do NOT write A2UI JSON. Do NOT call `send_a2ui_json_to_client` and do NOT put raw JSON in your text. Just call the right tool above with the required arguments.

STATUS LINES — IMPORTANT:
- Before every tool action, output one short plain-text status line saying what you are about to do, so the user sees progress. Keep it to a single friendly sentence.
"""

WORKFLOW_DESCRIPTION = """
Analyze the user's request, output a one-line status, and call the right tool. You never write A2UI JSON — the tools render the persistent A2UI 0.9 surfaces.

1.  **Determine intent & target surface:**
    * "hi", "hello", "start", "menu", "home", "dashboard", "overview", "panel", "control panel", "console", "main console", "operations center", "control center" -> `show_tabbed_control_center(active_tab=0)` (Tab 0: Overview & Dispatch).
    * "list", "table", "issues", "show reports", "tickets", "directory", "all incidents" -> `show_tabbed_control_center(active_tab=1, category_filter="all")` (Tab 1: Table).
    * "hvac", "filter by hvac", "electrical", "plumbing", "furniture", "safety", "filter by ...", category filter button clicks (`filterCategory` event with `category` context) -> `show_tabbed_control_center(active_tab=1, category_filter="<category>")` or `show_reports_list(category_filter="<category>")`.
    * "gallery", "sketches", "photos", "images", "visual records" -> `show_tabbed_control_center(active_tab=2)` (Tab 2: Visual Sketches Gallery).
    * "sla", "analytics", "stats", "chart", "metrics" -> `show_tabbed_control_center(active_tab=3)` (Tab 3: Analytics).
    * "report", "file", "new issue", "form", "dispatch", "log" -> `show_tabbed_control_center(active_tab=4)` (Tab 4: Dispatch Form).
    * "dialog", "modal", "menu", "popup" -> `show_dialog_demo()`.
    * Action button clicks (`switchTab`, `reportIssue`, `listReports`, `showDialogDemo`, `filterCategory`) -> Call the matching tool.
    * A `resolveEscalation` action (Confirm/Cancel in the escalation dialog) -> `resolve_escalation(decision=<context.decision>, surface_id=<context.surface_id>)`. Do not call `show_dialog_demo` for it.
    * A `ticketAction` action (a choice from the "Ticket Actions" menu) -> `run_ticket_action(ticket_action=<context.ticket_action>, surface_id=<context.surface_id>)`. Do not call `show_dialog_demo` for it.
    * A `submitReportQuick` action is a completed form submission -> go to step 2 (do not re-open the form).

2.  **New Report flow (`file_report`):**
    * Form submission (`submitReportQuick` action): its `context` holds the values the user entered — `room`, `item`, `issue`, `reporter_name`, `reporter_email`, `priority`, `category`. Output "Filing your report and generating the photo-realistic sketch...", then call `file_report` **exactly once**, passing each of those context values exactly as received (empty strings stay empty). Never substitute example values or details from earlier reports.
    * Chat request that gives room + item + issue: output the same status line, then call `file_report(room=..., item=..., issue=..., reporter_name=..., reporter_email=...)` **exactly once** using what the user said. If the room, item or issue is missing, open the form with `show_tabbed_control_center(active_tab=4)`.
    * `file_report` generates the sketch, saves the report, and shows the updated confirmation. If it returns `status: "error"`, tell the user in one sentence what to fill in; do not retry with made-up values.
"""

UI_DESCRIPTION = """
This agent uses pure native A2UI v0.9 composite catalog components:
- MaterialCard: elevated structural surfaces.
- MaterialTabs: 5-tab operations workflow.
- MaterialTable: dynamic tabular data grid for tickets.
- MaterialGridList: responsive image grid for visual sketches.
- VegaChart: interactive analytics and SLA compliance chart.
- MaterialSlider: dynamic simulation controller for SLA targets.
- Form inputs: MaterialSelect, MaterialInput, MaterialRadioButton, MaterialCheckbox, MaterialDatepicker, MaterialTimepicker, MaterialChips.
- Modals & context: MaterialDialog and MaterialMenu.
"""

_UI_KEYWORDS = {
    "dashboard",
    "overview",
    "control center",
    "control panel",
    "panel",
    "console",
    "operations",
    "table",
    "list",
    "reports",
    "tickets",
    "issues",
    "directory",
    "gallery",
    "sketches",
    "photos",
    "images",
    "analytics",
    "sla",
    "chart",
    "metrics",
    "form",
    "dispatch",
    "report",
    "file",
    "dialog",
    "modal",
    "menu",
    "category",
    "filter",
    "filtercategory",
    "hvac",
    "electrical",
    "plumbing",
    "furniture",
    "safety",
}

_TOOL_CALL_REMINDER = (
    "\n\n[Reminder] This request is best answered with a visual UI response. "
    "Output a one-line status, then call the matching tool "
    "(`show_tabbed_control_center`, `show_welcome`, `show_reports_list`, "
    "`show_gallery`, `show_analytics`, `show_report_form`, `show_dialog_demo`, "
    "`resolve_escalation`, `run_ticket_action`, or `file_report`) to render it. "
    "Do not write A2UI JSON."
)

# What the model sees instead of a surface it already rendered to the user.
RENDERED_SURFACE_NOTE = {
    "status": "success",
    "result": "Rendered to the user as an A2UI surface.",
}


def _compact_rendered_surfaces(llm_request: LlmRequest) -> None:
    """Replace A2UI payloads of earlier tool calls with a short note.

    The client has already rendered them. Sent back verbatim, a single
    dashboard (it inlines every sketch as base64) costs ~280k tokens, so a
    few tab clicks in one chat would overflow the model's context window.
    ADK builds `llm_request.contents` from copies, so session history is kept.
    """
    for content in llm_request.contents or []:
        for part in content.parts or []:
            function_response = part.function_response
            if (
                function_response
                and isinstance(function_response.response, dict)
                and VALIDATED_A2UI_JSON_KEY in function_response.response
            ):
                function_response.response = dict(RENDERED_SURFACE_NOTE)


def _before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Drop rendered A2UI payloads from the prompt and nudge UI intents to tools."""
    _compact_rendered_surfaces(llm_request)
    events = callback_context.session.events or []

    last_user_msg = ""
    for event in reversed(events):
        if event.author == "user" and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    last_user_msg = part.text.lower()
                    break
            break

    if not last_user_msg:
        return None

    if any(kw in last_user_msg for kw in _UI_KEYWORDS):
        llm_request.append_instructions([_TOOL_CALL_REMINDER])
    return None


def _get_a2ui_enabled(ctx: ReadonlyContext):
    return ctx.state.get(A2UI_ENABLED_KEY, False)


def _get_a2ui_catalog(ctx: ReadonlyContext):
    return rehydrate_catalog(ctx.state.get(A2UI_CATALOG_KEY))


def _get_a2ui_examples(ctx: ReadonlyContext):
    return ctx.state.get(A2UI_EXAMPLES_KEY)


class ReportAgent:
    """Facilities & Incident Operations Agent with pure native A2UI v0.9 support."""

    SUPPORTED_CONTENT_TYPES: ClassVar[list[str]] = ["text/plain"]

    def __init__(self, base_url: str):
        self.base_url = base_url
        self._agent_name = "a2ui_operations_agent_v09"
        self._user_id = "remote_agent"

        # In-memory Sessions & Memory for Cloud Run
        self._session_service = InMemorySessionService()
        self._memory_service = InMemoryMemoryService()
        self._artifact_service = InMemoryArtifactService()
        self._llm_agent = None

        # Pure A2UI v0.9 schema manager
        self._schema_managers: dict[str, A2uiSchemaManager] = {
            "0.9": self._build_schema_manager("0.9")
        }

        # Runner with SendA2uiToClientToolset
        self._runner = self._build_runner(self._build_llm_agent())

        self._agent_card = self._build_agent_card()

    @property
    def agent_card(self) -> AgentCard:
        return self._agent_card

    def get_runner(self) -> Runner:
        return self._runner

    def get_schema_manager(self, version: str | None) -> A2uiSchemaManager | None:
        if version is None:
            return None
        norm = version.lstrip("v")
        return self._schema_managers.get(norm, self._schema_managers.get("0.9"))

    def _build_schema_manager(self, version: str = "0.9") -> A2uiSchemaManager:
        cat_file = CATALOG_DEFINITION_JSON_V0_9
        examples_path = os.path.join(_APP_DIR, "examples", "report_catalog", version)
        return A2uiSchemaManager(
            version=version,
            catalogs=[
                CatalogConfig(
                    name=COMPOSITE_CATALOG_ID,
                    provider=FileSystemCatalogProvider(cat_file),
                    examples_path=examples_path,
                ),
                CatalogConfig(
                    name="composite",
                    provider=FileSystemCatalogProvider(cat_file),
                    examples_path=examples_path,
                ),
                CatalogConfig(
                    name="report",
                    provider=FileSystemCatalogProvider(cat_file),
                    examples_path=examples_path,
                ),
                BasicCatalog.get_config(version=version),
            ],
            accepts_inline_catalogs=True,
            schema_modifiers=[remove_strict_validation],
        )

    def _build_agent_card(self) -> AgentCard:
        extensions = []
        for version, sm in self._schema_managers.items():
            if version == "0.9":
                ext = get_a2ui_agent_extension(
                    version,
                    sm.accepts_inline_catalogs,
                    sm.supported_catalog_ids,
                )
                extensions.append(ext)

        capabilities = AgentCapabilities(
            streaming=True,
            extensions=extensions,
        )

        return AgentCard(
            name="A2UI 0.9 Operations Control Center",
            description=(
                "Facilities & incident operations agent powered by pure native A2UI v0.9 Material 3 "
                "components, MaterialTable, MaterialGridList, and Vega-Lite charts."
            ),
            url=self.base_url,
            icon_url=agent_icon_data_uri(),
            version="1.0.0",
            default_input_modes=self.SUPPORTED_CONTENT_TYPES,
            default_output_modes=self.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[
                AgentSkill(
                    id="file_report",
                    name="File a Maintenance Incident",
                    description=(
                        "File a building maintenance / incident report with an "
                        "auto-generated photo-realistic 16:9 sketch of the issue."
                    ),
                    tags=["maintenance", "report", "incident", "facilities", "sketch"],
                    examples=[
                        "Report a broken chair in the Yellow Room.",
                        "The light in the Fish Tank room is flickering.",
                        "I want to file an issue.",
                    ],
                ),
                AgentSkill(
                    id="manage_operations",
                    name="Operations Control Center & SLA Analytics",
                    description=(
                        "Inspect the 5-tab operations dashboard, view active tickets table, "
                        "browse visual sketch gallery, and analyze SLA forecasting charts."
                    ),
                    tags=["operations", "dashboard", "table", "analytics", "sla"],
                    examples=[
                        "Show me the operations control center.",
                        "List all active maintenance reports.",
                        "Show the visual sketch gallery.",
                        "Show SLA analytics chart.",
                    ],
                ),
            ],
        )

    def _build_runner(self, agent: LlmAgent) -> Runner:
        return Runner(
            app_name=self._agent_name,
            agent=agent,
            artifact_service=self._artifact_service,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )

    def _build_llm_agent(self) -> LlmAgent:
        """Builds the LLM agent with A2UI toolset."""
        if hasattr(self, "_llm_agent") and self._llm_agent:
            return self._llm_agent

        model = Gemini(
            model=build_vertex_model_name(DEFAULT_MODEL),
            retry_options=types.HttpRetryOptions(attempts=3),
        )

        schema_manager = next(iter(self._schema_managers.values()), None)

        instruction = (
            schema_manager.generate_system_prompt(
                role_description=ROLE_DESCRIPTION,
                workflow_description=WORKFLOW_DESCRIPTION,
                ui_description=UI_DESCRIPTION,
                include_schema=False,
                include_examples=False,
                validate_examples=False,
            )
            if schema_manager
            else ROLE_DESCRIPTION
        )

        tools = [
            show_tabbed_control_center,
            show_welcome,
            show_reports_list,
            show_gallery,
            show_analytics,
            show_report_form,
            show_dialog_demo,
            resolve_escalation,
            run_ticket_action,
            show_report_confirmation,
            file_report,
            SendA2uiToClientToolset(
                a2ui_enabled=_get_a2ui_enabled,
                a2ui_catalog=_get_a2ui_catalog,
                a2ui_examples=_get_a2ui_examples,
            ),
        ]

        return LlmAgent(
            model=model,
            name=self._agent_name,
            description="A2UI v0.9 Facilities Operations Agent",
            instruction=instruction,
            before_model_callback=_before_model_callback,
            tools=tools,
        )
