"""Asynchronous WeKnora HTTP client."""

from typing import Any
from urllib.parse import quote

import httpx

from app.errors import AdapterError


class WeKnoraClient:
    """Client for WeKnora retrieval, session, and chat endpoints."""

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._http_client = http_client

    async def hybrid_search(
        self,
        knowledge_base_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a WeKnora search and return its decoded JSON envelope."""

        path_id = quote(knowledge_base_id, safe="")
        url = f"{self._api_url}/knowledge-bases/{path_id}/hybrid-search"
        try:
            response = await self._http_client.post(
                url,
                headers={"X-API-Key": self._api_key},
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise AdapterError(
                504,
                "upstream_timeout",
                "WeKnora did not respond before the configured timeout",
            ) from exc
        except httpx.RequestError as exc:
            raise AdapterError(
                502,
                "upstream_unavailable",
                "WeKnora could not be reached",
            ) from exc

        if response.status_code == 401:
            raise AdapterError(401, "upstream_unauthorized", "WeKnora rejected the API key")
        if response.status_code == 403:
            raise AdapterError(403, "upstream_forbidden", "WeKnora denied knowledge-base access")
        if not response.is_success:
            raise AdapterError(
                502,
                "upstream_error",
                f"WeKnora returned HTTP {response.status_code}",
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise AdapterError(
                502,
                "invalid_upstream_response",
                "WeKnora returned invalid JSON",
            ) from exc
        if not isinstance(result, dict):
            raise AdapterError(
                502,
                "invalid_upstream_response",
                "WeKnora returned an invalid JSON document",
            )
        return {str(key): value for key, value in result.items()}

    async def create_session(
        self, *, api_key: str, title: str, external_user_id: str | None = None
    ) -> str:
        """Create a WeKnora conversation and return its session ID."""

        headers = {"X-API-Key": api_key}
        if external_user_id:
            headers["X-External-User-ID"] = external_user_id
        try:
            response = await self._http_client.post(
                f"{self._api_url}/sessions",
                headers=headers,
                json={"title": title},
            )
        except httpx.TimeoutException as exc:
            raise AdapterError(504, "upstream_timeout", "WeKnora timed out") from exc
        except httpx.RequestError as exc:
            raise AdapterError(502, "upstream_unavailable", "WeKnora could not be reached") from exc

        self._raise_for_chat_status(response, stage="session creation")
        try:
            envelope = response.json()
            session_id = envelope.get("data", {}).get("id")
        except (ValueError, AttributeError):
            session_id = None
        if not isinstance(session_id, str) or not session_id:
            raise AdapterError(
                502,
                "invalid_upstream_response",
                "WeKnora returned an invalid session response",
            )
        return session_id

    async def open_agent_chat(
        self,
        *,
        api_key: str,
        session_id: str,
        query: str,
        agent_id: str,
        knowledge_base_ids: list[str],
        external_user_id: str | None = None,
    ) -> httpx.Response:
        """Open WeKnora's agent SSE response, leaving its body streaming."""

        path_session_id = quote(session_id, safe="")
        payload: dict[str, Any] = {
            "query": query,
            "channel": "api",
            "agent_id": agent_id,
            "agent_enabled": True,
        }
        if knowledge_base_ids:
            payload["knowledge_base_ids"] = knowledge_base_ids
        headers = {"X-API-Key": api_key, "Accept": "text/event-stream"}
        if external_user_id:
            headers["X-External-User-ID"] = external_user_id
        request = self._http_client.build_request(
            "POST",
            f"{self._api_url}/agent-chat/{path_session_id}",
            headers=headers,
            json=payload,
        )
        try:
            response = await self._http_client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise AdapterError(504, "upstream_timeout", "WeKnora timed out") from exc
        except httpx.RequestError as exc:
            raise AdapterError(502, "upstream_unavailable", "WeKnora could not be reached") from exc
        try:
            self._raise_for_chat_status(response, stage="Agent chat")
        except AdapterError:
            await response.aclose()
            raise
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            await response.aclose()
            raise AdapterError(
                502,
                "invalid_upstream_response",
                "WeKnora did not return an event stream",
            )
        return response

    async def generate_session_title(
        self,
        *,
        api_key: str,
        session_id: str,
        messages: list[dict[str, str]],
        external_user_id: str | None = None,
    ) -> str:
        """Generate and persist a WeKnora session title from conversation messages."""

        path_session_id = quote(session_id, safe="")
        headers = {"X-API-Key": api_key}
        if external_user_id:
            headers["X-External-User-ID"] = external_user_id
        try:
            response = await self._http_client.post(
                f"{self._api_url}/sessions/{path_session_id}/generate_title",
                headers=headers,
                json={"messages": messages},
            )
        except httpx.TimeoutException as exc:
            raise AdapterError(504, "upstream_timeout", "WeKnora timed out") from exc
        except httpx.RequestError as exc:
            raise AdapterError(502, "upstream_unavailable", "WeKnora could not be reached") from exc

        self._raise_for_chat_status(response, stage="title generation")
        try:
            envelope = response.json()
            title = envelope.get("data")
        except (ValueError, AttributeError):
            title = None
        if not isinstance(title, str) or not title.strip():
            raise AdapterError(
                502,
                "invalid_upstream_response",
                "WeKnora returned an invalid generated title",
            )
        return title.strip()

    @staticmethod
    def _raise_for_chat_status(response: httpx.Response, *, stage: str) -> None:
        if response.status_code == 401:
            raise AdapterError(401, "upstream_unauthorized", "WeKnora rejected the chat API key")
        if response.status_code == 403:
            raise AdapterError(403, "upstream_forbidden", "WeKnora denied chat access")
        if response.status_code == 404:
            if stage == "session creation":
                raise AdapterError(
                    502,
                    "upstream_not_found",
                    "WeKnora session creation endpoint was not found",
                )
            if stage == "title generation":
                raise AdapterError(
                    404,
                    "conversation_not_found",
                    "WeKnora session was not found while generating its title",
                )
            raise AdapterError(
                404,
                "conversation_not_found",
                "WeKnora session was not found during Agent chat",
            )
        if response.status_code == 409:
            raise AdapterError(409, "conversation_busy", "WeKnora session already has a running answer")
        if not response.is_success:
            raise AdapterError(
                502,
                "upstream_error",
                f"WeKnora returned HTTP {response.status_code}",
            )
