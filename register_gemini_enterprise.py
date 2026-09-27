#!/usr/bin/env python3
"""Registers or updates an A2A Agent in Google Gemini Enterprise (Discovery Engine)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# Icon shipped with the agent; used when neither --icon-url nor the agent card provides one.
BUNDLED_ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "app", "assets", "agent_icon.png"
)


def get_gcloud_token() -> str:
    res = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def get_identity_token() -> str | None:
    """Return a Google ID token for the active gcloud account, or None."""
    try:
        res = subprocess.run(
            ["gcloud", "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return res.stdout.strip() or None


def get_project_number(project_id: str) -> str:
    res = subprocess.run(
        ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def is_cloud_run_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").endswith(".run.app")


def get_agent_card(service_url: str) -> dict:
    card_url = f"{service_url.rstrip('/')}/.well-known/agent-card.json"
    print(f"Fetching agent card from: {card_url}")
    headers = {"User-Agent": "Gemini-Enterprise-Registrar/1.0"}
    # The Cloud Run service is private by default, so authenticate with your
    # gcloud identity. The token is only ever sent to Cloud Run (*.run.app) hosts.
    if is_cloud_run_url(card_url):
        id_token = get_identity_token()
        if id_token:
            headers["Authorization"] = f"Bearer {id_token}"
    req = urllib.request.Request(card_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Could not fetch the agent card ({e.code}).", file=sys.stderr)
        if e.code in (401, 403):
            print(
                "The service is private: make sure your gcloud account can invoke it "
                "(roles/run.invoker on the Cloud Run service).",
                file=sys.stderr,
            )
        sys.exit(1)


def build_icon(icon_url: str | None, card: dict) -> dict | None:
    """Icon for the registration: --icon-url, else the card's icon, else the bundled PNG.

    Inline icons go in `icon.uri` as a data URI. The Gemini Enterprise UI uses
    `icon.content` as a raw image source, so an icon sent that way shows as a
    letter avatar instead of the image.
    """
    if icon_url:
        return {"uri": icon_url}
    card_icon = card.get("iconUrl") or ""
    is_inline = card_icon.startswith("data:image/") and ";base64," in card_icon
    if card_icon.startswith("https://") or is_inline:
        return {"uri": card_icon}
    if os.path.exists(BUNDLED_ICON_PATH):
        with open(BUNDLED_ICON_PATH, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return {"uri": f"data:image/png;base64,{encoded}"}
    return None


def get_api_endpoint(location: str) -> str:
    if not location or location == "global":
        return "https://discoveryengine.googleapis.com"
    return f"https://{location}-discoveryengine.googleapis.com"


def list_existing_agents(
    project_number: str,
    location: str,
    collection_id: str,
    engine_id: str,
    token: str,
    project_id: str,
) -> list[dict]:
    host = get_api_endpoint(location)
    url = (
        f"{host}/v1alpha/projects/{project_number}/"
        f"locations/{location}/collections/{collection_id}/engines/{engine_id}/"
        f"assistants/default_assistant/agents"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Goog-User-Project": project_id,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return data.get("agents", [])
    except urllib.error.HTTPError as e:
        print(f"Error listing agents: {e.code} - {e.read().decode()}", file=sys.stderr)
        raise


def register_or_update_agent(
    project_id: str,
    engine_id: str,
    service_url: str,
    display_name: str | None = None,
    location: str = "global",
    collection_id: str = "default_collection",
    icon_url: str | None = None,
):
    token = get_gcloud_token()
    project_number = get_project_number(project_id)
    card = get_agent_card(service_url)

    final_display_name = display_name or card.get("name", "A2UI 0.9 ADK Agent")
    description = card.get(
        "description",
        "Facilities & incident operations agent powered by pure native A2UI v0.9 on Google ADK.",
    )
    card_url = str(card.get("url", "")).rstrip("/")
    if card_url != service_url.rstrip("/"):
        print(
            f"Warning: the agent card advertises url '{card_url}', not '{service_url}'. "
            "Gemini Enterprise calls the card's url; set AGENT_URL on the service.",
            file=sys.stderr,
        )
    icon = build_icon(icon_url, card)

    card_str = json.dumps(card)

    existing_agents = list_existing_agents(
        project_number=project_number,
        location=location,
        collection_id=collection_id,
        engine_id=engine_id,
        token=token,
        project_id=project_id,
    )

    matched_agent = None
    for agent in existing_agents:
        if agent.get("displayName") == final_display_name:
            matched_agent = agent
            break
        a2a_def = agent.get("a2aAgentDefinition", {})
        existing_card_str = a2a_def.get("jsonAgentCard")
        if existing_card_str:
            try:
                ex_card = json.loads(existing_card_str)
                if ex_card.get("url", "").rstrip("/") == service_url.rstrip("/"):
                    matched_agent = agent
                    break
            except Exception:
                pass

    request_body = {
        "displayName": final_display_name,
        "description": description,
        "a2aAgentDefinition": {
            "jsonAgentCard": card_str,
        },
        "agentInvocationSpec": {
            "invocationMode": "AUTOMATIC",
        },
    }
    if icon:
        request_body["icon"] = icon

    host = get_api_endpoint(location)

    if matched_agent:
        agent_name = matched_agent["name"]
        print(f"Updating existing Gemini Enterprise agent: {agent_name}")
        url = f"{host}/v1alpha/{agent_name}"
        data_bytes = json.dumps(request_body).encode()
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Goog-User-Project": project_id,
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
    else:
        print(f"Registering new Gemini Enterprise agent '{final_display_name}' in engine '{engine_id}'...")
        url = (
            f"{host}/v1alpha/projects/{project_number}/"
            f"locations/{location}/collections/{collection_id}/engines/{engine_id}/"
            f"assistants/default_assistant/agents"
        )
        data_bytes = json.dumps(request_body).encode()
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Goog-User-Project": project_id,
                "Content-Type": "application/json",
            },
            method="POST",
        )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode())
            print("\nRegistration Successful!")
            print(f"Agent Name:    {res_data.get('name')}")
            print(f"Display Name:  {res_data.get('displayName')}")
            print(f"State:         {res_data.get('state', 'ENABLED')}")
            print(f"Agent URL:     {service_url}")
            return res_data
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode()
        print(f"Registration failed ({e.code}): {err_msg}", file=sys.stderr)
        sys.exit(1)


def delete_agent(
    project_id: str,
    engine_id: str,
    display_name: str | None = None,
    location: str = "global",
    collection_id: str = "default_collection",
):
    if not display_name:
        # An empty name would match every agent in the app.
        print("Refusing to delete: pass the --display-name of the agent to remove.", file=sys.stderr)
        sys.exit(2)
    token = get_gcloud_token()
    project_number = get_project_number(project_id)
    existing_agents = list_existing_agents(
        project_number=project_number,
        location=location,
        collection_id=collection_id,
        engine_id=engine_id,
        token=token,
        project_id=project_id,
    )
    host = get_api_endpoint(location)
    deleted = 0
    for agent in existing_agents:
        if agent.get("displayName") == display_name:
            agent_name = agent["name"]
            print(f"Deleting agent: {agent_name} ({agent.get('displayName')})")
            url = f"{host}/v1alpha/{agent_name}"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Goog-User-Project": project_id,
                },
                method="DELETE",
            )
            try:
                with urllib.request.urlopen(req, timeout=30):
                    print(f"Successfully deleted {agent_name}")
                    deleted += 1
            except urllib.error.HTTPError as e:
                print(f"Error deleting agent: {e.code} - {e.read().decode()}", file=sys.stderr)
    if deleted == 0:
        print(f"No matching agent found with display_name='{display_name}' to delete.")


def main():
    parser = argparse.ArgumentParser(
        description="Register or unregister an A2A Agent with Google Gemini Enterprise"
    )
    parser.add_argument("--project", required=True, help="Google Cloud Project ID")
    parser.add_argument("--service-url", help="Cloud Run service HTTPS URL (https://...run.app)")
    parser.add_argument(
        "--engine",
        required=True,
        help="Gemini Enterprise Engine / App ID (from the Gemini Enterprise console)",
    )
    parser.add_argument(
        "--display-name",
        default="A2UI 0.9 ADK Agent",
        help="Display name in Gemini Enterprise",
    )
    parser.add_argument(
        "--location",
        default="global",
        help="Gemini Enterprise app location: global, eu or us (default: global)",
    )
    parser.add_argument(
        "--collection", default="default_collection", help="Collection ID"
    )
    parser.add_argument(
        "--icon-url",
        default=None,
        help="Optional https URL of an icon to use instead of the icon bundled with the agent",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Unregister / delete the agent from Gemini Enterprise",
    )

    args = parser.parse_args()
    if args.delete:
        delete_agent(
            project_id=args.project,
            engine_id=args.engine,
            display_name=args.display_name,
            location=args.location,
            collection_id=args.collection,
        )
    else:
        if not args.service_url:
            parser.error("--service-url is required when registering an agent")
        register_or_update_agent(
            project_id=args.project,
            engine_id=args.engine,
            service_url=args.service_url,
            display_name=args.display_name,
            location=args.location,
            collection_id=args.collection,
            icon_url=args.icon_url,
        )


if __name__ == "__main__":
    main()

