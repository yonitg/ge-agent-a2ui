"""Hardened A2UI payload parser.

The upstream ``a2ui.parser.payload_fixer.parse_and_fix`` only repairs smart
quotes and trailing commas. On large surfaces (notably the reports list, which
runs ~15k characters) the model reliably emits payloads that ``json.loads``
rejects with:

* ``Extra data`` — two or more concatenated top-level JSON values, e.g.
  ``[beginRendering][surfaceUpdate]`` or an array followed by stray narration.
* ``Expecting ',' delimiter`` — a comma dropped between adjacent messages
  (``}{`` / ``]{``) somewhere in the payload.

This module provides a drop-in ``parse_and_fix`` that additionally:

1. strips Markdown code fences and leading/trailing narration,
2. decodes any number of concatenated top-level values and merges them into a
   single ordered list of A2UI messages,
3. as a last resort, inserts the structural commas the model omitted between
   adjacent objects/arrays/strings.

Each step is best-effort and falls through to the next; if everything fails the
original parse error is re-raised so behaviour matches upstream on truly
unrecoverable input. ``install()`` patches the symbol both at its source module
and in every module that imported it by value.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SMART_QUOTES = {
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark
}

# Characters that close a JSON value and so require a comma before the next one.
_VALUE_ENDERS = frozenset("}]\"")
# Characters that open a JSON value when one already ended.
_VALUE_OPENERS = frozenset("{[\"")


def _normalize_smart_quotes(text: str) -> str:
    for smart, straight in _SMART_QUOTES.items():
        text = text.replace(smart, straight)
    return text


def _strip_code_fences(text: str) -> str:
    """Remove a surrounding ```json ... ``` (or bare ``` ... ```) fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", stripped)
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _trim_to_json(text: str) -> str:
    """Drop narration outside the outermost JSON value."""
    start = min(
        (i for i in (text.find("["), text.find("{")) if i != -1),
        default=-1,
    )
    if start == -1:
        return text
    end = max(text.rfind("]"), text.rfind("}"))
    if end < start:
        return text
    return text[start : end + 1]


def _remove_trailing_commas(text: str) -> str:
    return re.sub(r",(?=\s*[\]}])", "", text)


def _flatten(values: list[Any]) -> list[dict[str, Any]]:
    """Flatten decoded top-level values into a single list of message dicts."""
    messages: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, list):
            messages.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            messages.append(value)
    return messages


def _decode_concatenated(text: str) -> list[Any]:
    """Decode one or more concatenated top-level JSON values.

    Whitespace and stray commas between values are skipped. Trailing data that
    is not valid JSON is ignored once at least one value has been decoded (this
    is how dangling narration after a complete payload is tolerated).
    """
    decoder = json.JSONDecoder()
    index, length = 0, len(text)
    values: list[Any] = []
    while index < length:
        while index < length and text[index] in " \t\r\n,":
            index += 1
        if index >= length:
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            if values:
                logger.warning("Ignoring trailing non-JSON data in A2UI payload.")
                break
            raise
        values.append(value)
    return values


def _insert_structural_commas(text: str) -> str:
    """Insert commas the model omitted between adjacent values.

    Scans outside of strings and, whenever a value-ending token is followed by a
    value-opening token, inserts a comma. Handles ``}{``, ``]{``, ``}"`` etc.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    prev_significant = ""
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                prev_significant = '"'
            continue
        if char in " \t\r\n":
            out.append(char)
            continue
        if char in _VALUE_OPENERS and prev_significant in _VALUE_ENDERS:
            out.append(",")
        out.append(char)
        if char == '"':
            in_string = True
            prev_significant = ""
        else:
            prev_significant = char
    return "".join(out)


def parse_and_fix(payload: Any) -> list[dict[str, Any]]:
    """Parse a raw A2UI payload string into a list of message dicts."""
    if isinstance(payload, list):
        return _flatten(payload)
    if isinstance(payload, dict):
        return [payload]

    text = _strip_code_fences(_normalize_smart_quotes(str(payload)))

    # 1. Strict parse (fast path; matches a well-formed payload).
    try:
        return _flatten([json.loads(text)])
    except json.JSONDecodeError as error:
        # Python deletes the ``as`` name when the block exits; keep a copy so the
        # original failure can be reported if every repair below also fails.
        first_error = error

    no_trailing = _remove_trailing_commas(text)
    trimmed = _trim_to_json(no_trailing)

    # 2. Trailing-comma removal + narration trim.
    for candidate in (no_trailing, trimmed):
        try:
            return _flatten([json.loads(candidate)])
        except json.JSONDecodeError:
            continue

    # 3. Concatenated top-level values ("Extra data").
    for candidate in (text, no_trailing, trimmed):
        try:
            values = _decode_concatenated(candidate)
        except json.JSONDecodeError:
            continue
        if values:
            if len(values) > 1:
                logger.warning(
                    "A2UI payload had %d concatenated top-level values; merged "
                    "into a single message list.",
                    len(values),
                )
            return _flatten(values)

    # 4. Last resort: repair missing structural commas, then re-decode.
    repaired = _insert_structural_commas(trimmed)
    if repaired != trimmed:
        try:
            values = _decode_concatenated(repaired)
            if values:
                logger.warning("A2UI payload required structural-comma repair.")
                return _flatten(values)
        except json.JSONDecodeError:
            pass

    # 5. Final safety net: the json-repair library (handles missing commas,
    #    unterminated strings, unquoted keys, etc.). Deterministic rendering
    #    means the model rarely authors JSON anymore, but this catches any
    #    residual `send_a2ui_json_to_client` payloads so a render never loops.
    try:
        from json_repair import repair_json

        obj = repair_json(text, return_objects=True)
        if isinstance(obj, (list, dict)) and obj:
            logger.warning("A2UI payload recovered via json-repair safety net.")
            return _flatten(obj if isinstance(obj, list) else [obj])
    except Exception:
        pass

    logger.error("Failed to parse A2UI JSON after all repairs: %s", first_error)
    raise ValueError(f"Failed to parse JSON: {first_error}")


def install() -> None:
    """Replace the upstream ``parse_and_fix`` with the hardened version."""
    patched = 0
    for module_name in (
        "a2ui.parser.payload_fixer",
        "a2ui.parser.parser",
        "a2ui.adk.send_a2ui_to_client_toolset",
    ):
        try:
            module = __import__(module_name, fromlist=["parse_and_fix"])
        except Exception:  # pragma: no cover - module layout may change
            continue
        if hasattr(module, "parse_and_fix"):
            module.parse_and_fix = parse_and_fix
            patched += 1
    logger.info("Installed hardened A2UI parse_and_fix on %d module(s).", patched)
