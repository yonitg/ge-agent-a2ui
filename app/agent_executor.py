"""Agent executor for GE UI with A2UI extension support."""

import logging
from typing import override

from a2a.server.agent_execution import RequestContext
from a2a.types import DataPart, Part, TextPart
from a2ui.a2a.extension import try_activate_a2ui_extension
from a2ui.a2a.parts import A2UI_MIME_TYPE, create_a2ui_part
try:
    from a2ui.adk.a2a.event_converter import A2uiEventConverter
except ImportError:
    from a2ui.adk.send_a2ui_to_client_toolset import (
        A2uiEventConverter,
    )
from a2ui.schema.constants import A2UI_CLIENT_CAPABILITIES_KEY
from google.adk.a2a.converters.request_converter import (
    AgentRunRequest,
)
from google.adk.a2a.executor.a2a_agent_executor import (
    A2aAgentExecutor,
    A2aAgentExecutorConfig,
)
from google.adk.agents.invocation_context import new_invocation_context_id
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner

from app.agent import ReportAgent
from app.config import (
    COMPOSITE_CATALOG_ID,
)
from app.session_keys import (
    A2UI_CATALOG_KEY,
    A2UI_ENABLED_KEY,
    A2UI_EXAMPLES_KEY,
    catalog_id_from_state,
    rehydrate_catalog,
    serialize_catalog,
)

logger = logging.getLogger(__name__)


# A2UI message types that must each travel as their own message.
# Renderers reject a single message that contains more than one of these keys.
_A2UI_UPDATE_TYPES = (
    "createSurface",
    "deleteSurface",
    "updateDataModel",
    "updateComponents",
)

_A2UI_MIME_TYPES = ("application/json+a2ui", "application/a2ui+json", A2UI_MIME_TYPE)

# Text line placed above A2UI content when a reply has no text of its own.
SURFACE_HEADER_TEXT = "Facilities & Incident Operations Control Center:"
# Used instead when the reply creates no new card and only updates a card from
# an earlier turn (Gemini Enterprise applies the update to that card in place).
CARD_UPDATED_TEXT = "Updated the card above."


def _is_a2ui_part(part) -> bool:
    root = getattr(part, "root", part)
    return (
        isinstance(root, DataPart)
        and bool(root.metadata)
        and root.metadata.get("mimeType") in _A2UI_MIME_TYPES
    )


def _is_text_part(part) -> bool:
    root = getattr(part, "root", part)
    return isinstance(root, TextPart) or getattr(root, "kind", None) == "text"


def _add_leading_text(parts: list) -> None:
    """Put a text line in front of A2UI content when the reply has no text."""
    a2ui_parts = [p for p in parts if _is_a2ui_part(p)]
    if not a2ui_parts or any(_is_text_part(p) for p in parts):
        return
    creates_surface = any(
        isinstance(getattr(p, "root", p).data, dict)
        and "createSurface" in getattr(p, "root", p).data
        for p in a2ui_parts
    )
    text = SURFACE_HEADER_TEXT if creates_surface else CARD_UPDATED_TEXT
    parts.insert(0, Part(root=TextPart(text=text)))


def _split_combined_a2ui_data(data: dict) -> list[dict]:
    """Split one A2UI v0.9 message containing multiple update types into separate messages."""
    types_present = [t for t in _A2UI_UPDATE_TYPES if t in data]
    if len(types_present) <= 1:
        return [data]
    base = {"version": "v0.9"}
    return [{**base, t: data[t]} for t in types_present]


def _repair_catalog_id(msg: dict, valid_catalog_id: str) -> None:
    """Overwrite a bad `createSurface.catalogId` with the session's active value.

    The LLM occasionally hallucinates IDs instead of copying the value from
    the prompt example. Renderers reject those with ``Catalog not found``
    and the surface never appears.
    """
    create_surface = msg.get("createSurface")
    if not isinstance(create_surface, dict):
        return
    actual = create_surface.get("catalogId")
    official = {
        COMPOSITE_CATALOG_ID,
        "https://www.gstatic.com/vertexaisearch/a2ui/v0_9/gemini_enterprise_composite_catalog.json",
        "https://a2ui.org/specification/v0_9/catalogs/material/catalog.json",
        "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json",
        valid_catalog_id,
    }
    if actual in official:
        return
    logger.warning(
        "Repairing invalid createSurface.catalogId %r -> %r",
        actual,
        valid_catalog_id or COMPOSITE_CATALOG_ID,
    )
    create_surface["catalogId"] = valid_catalog_id or COMPOSITE_CATALOG_ID


def _process_a2ui_parts(parts: list, valid_catalog_id: str | None = None) -> list:
    """Split combined A2UI parts and repair catalogIds."""
    new_parts = []
    for part in parts:
        data_part = getattr(part, "root", None)
        is_a2ui = (
            isinstance(data_part, DataPart)
            and data_part.metadata
            and data_part.metadata.get("mimeType")
            in ("application/json+a2ui", "application/a2ui+json", A2UI_MIME_TYPE)
            and isinstance(data_part.data, dict)
        )
        if not is_a2ui:
            new_parts.append(part)
            continue
        for msg in _split_combined_a2ui_data(data_part.data):
            if valid_catalog_id is not None:
                _repair_catalog_id(msg, valid_catalog_id)
            new_parts.append(create_a2ui_part(msg))
    return new_parts


class _ReportEventConverter(A2uiEventConverter):
    """Post-processes A2A events to keep A2UI parts well-formed for any renderer.

    Two responsibilities:
      1. Split A2UI data parts that combine multiple update types into one
         object (LLM behavior renderers reject).
      2. Repair `createSurface.catalogId` when the LLM substitutes a
         non-registered value for the real catalog ID — renderers reject
         unknown catalog IDs with `Catalog not found`.
    """

    def __call__(
        self,
        event,
        invocation_context,
        task_id=None,
        context_id=None,
        part_converter_func=None,
    ):
        kwargs = {
            "event": event,
            "invocation_context": invocation_context,
            "task_id": task_id,
            "context_id": context_id,
        }
        if part_converter_func is not None:
            kwargs["part_converter_func"] = part_converter_func

        cat_val = invocation_context.session.state.get(A2UI_CATALOG_KEY)
        if isinstance(cat_val, dict):
            invocation_context.session.state[A2UI_CATALOG_KEY] = rehydrate_catalog(cat_val)

        a2a_events = super().__call__(**kwargs)

        valid_catalog_id = catalog_id_from_state(
            invocation_context.session.state.get(A2UI_CATALOG_KEY)
        )

        for a2a_event in a2a_events:
            message = getattr(getattr(a2a_event, "status", None), "message", None)
            if message and message.parts:
                message.parts = _process_a2ui_parts(message.parts, valid_catalog_id)
                _add_leading_text(message.parts)

            # Handle singular artifact on TaskArtifactUpdateEvent
            single_artifact = getattr(a2a_event, "artifact", None)
            if single_artifact and getattr(single_artifact, "parts", None):
                single_artifact.parts = _process_a2ui_parts(
                    single_artifact.parts, valid_catalog_id
                )
                _add_leading_text(single_artifact.parts)

            for artifact in getattr(a2a_event, "artifacts", None) or []:
                if getattr(artifact, "parts", None):
                    artifact.parts = _process_a2ui_parts(
                        artifact.parts, valid_catalog_id
                    )
                    _add_leading_text(artifact.parts)
        return a2a_events


class ReportExecutor(A2aAgentExecutor):
    """Executor for the maintenance report agent with A2UI GE session setup."""

    def __init__(self, base_url: str, agent: ReportAgent):
        self._base_url = base_url
        self._agent = agent

        # bypass_tool_check=True lets the converter emit A2UI parts from any
        # tool that returns a `validated_a2ui_json` payload — not just
        # `send_a2ui_json_to_client`. This is what makes the deterministic
        # `show_*` render tools in app/render_tools.py work (the model routes
        # intent + data; Python builds the validated surface). Tools that don't
        # return that key are unaffected.
        config = A2aAgentExecutorConfig(
            event_converter=_ReportEventConverter(bypass_tool_check=True),
        )
        # `use_legacy=True` forces ADK's legacy execute() path, which calls
        # the overridden `_prepare_session` below. The newer ADK impl
        # (`_A2aAgentExecutor` in `a2a_agent_executor_impl.py`) is opted
        # into by clients that send the `_NEW_A2A_ADK_INTEGRATION_EXTENSION`
        # extension (Gemini Enterprise does this), and that path bypasses
        # `_prepare_session` entirely — so our A2UI session state never gets
        # set and `send_a2ui_json_to_client` is missing from the toolset.
        super().__init__(
            runner=self._agent.get_runner(),
            config=config,
            use_legacy=True,
        )

    @override
    async def _prepare_session(
        self,
        context: RequestContext,
        run_request: AgentRunRequest,
        runner: Runner,
    ):
        logger.info("Loading session for message %s", context.message)

        active_ui_version = try_activate_a2ui_extension(context, self._agent.agent_card)

        if not active_ui_version:
            active_ui_version = "0.9"
            try:
                context.add_activated_extension("https://a2ui.org/a2a-extension/a2ui/v0.9")
            except Exception:
                logger.debug("Could not register fallback A2UI v0.9 extension on context")

        schema_manager = self._agent.get_schema_manager(active_ui_version)

        session = await super()._prepare_session(context, run_request, runner)

        if "base_url" not in session.state:
            session.state["base_url"] = self._base_url

        if active_ui_version and schema_manager:
            capabilities = (
                context.message.metadata.get(A2UI_CLIENT_CAPABILITIES_KEY)
                if context.message and context.message.metadata
                else None
            )
            a2ui_catalog = schema_manager.get_selected_catalog(
                client_ui_capabilities=capabilities
            )
            examples = schema_manager.load_examples(a2ui_catalog, validate=True)

            await runner.session_service.append_event(
                session,
                Event(
                    invocation_id=new_invocation_context_id(),
                    author="system",
                    actions=EventActions(
                        state_delta={
                            A2UI_ENABLED_KEY: True,
                            A2UI_CATALOG_KEY: serialize_catalog(a2ui_catalog),
                            A2UI_EXAMPLES_KEY: examples,
                        }
                    ),
                ),
            )

        return session
