"""Unit tests for the ADK agent callbacks."""

import json
from unittest.mock import MagicMock

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from app.agent import RENDERED_SURFACE_NOTE, _before_model_callback
from app.render_tools import VALIDATED_A2UI_JSON_KEY, build_tabbed_control_center_surface


def _function_response(name: str, response: dict) -> types.Content:
    return types.Content(
        role="user",
        parts=[types.Part(function_response=types.FunctionResponse(name=name, response=response))],
    )


def test_rendered_surfaces_are_not_sent_back_to_the_model():
    """A dashboard inlines every sketch; resending it would flood the context."""
    surface = build_tabbed_control_center_surface(2)
    assert "data:image/" in json.dumps(surface)
    error = {"status": "error", "message": "Please provide the issue description."}
    llm_request = LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part(text="show the gallery")]),
            types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name="show_gallery", args={}))],
            ),
            _function_response("show_gallery", {VALIDATED_A2UI_JSON_KEY: surface}),
            _function_response("file_report", error),
        ]
    )
    callback_context = MagicMock()
    callback_context.session.events = []

    assert _before_model_callback(callback_context, llm_request) is None

    assert "data:image/" not in llm_request.model_dump_json()
    assert llm_request.contents[2].parts[0].function_response.response == RENDERED_SURFACE_NOTE
    # Results the model has to act on are kept as they are.
    assert llm_request.contents[3].parts[0].function_response.response == error
    assert llm_request.contents[1].parts[0].function_call.name == "show_gallery"
