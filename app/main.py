"""FastAPI application for the WeKnora-Dify adapter."""

import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import authenticate
from app.chat import dify_stream
from app.client import WeKnoraClient
from app.config import Settings, get_settings
from app.errors import AdapterError
from app.mapping import map_request, map_response
from app.models import DifyChatRequest, DifyRetrievalRequest, DifyRetrievalResponse
from app.sessions import ConversationStore

logger = logging.getLogger("weknora_dify_adapter")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_UNSAFE_LOG_CHARACTER = re.compile(r"[^A-Za-z0-9._:-]")


def _request_id(value: str | None) -> str:
    if value is not None and _SAFE_REQUEST_ID.fullmatch(value):
        return value
    return str(uuid4())


def _safe_log_id(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return _UNSAFE_LOG_CHARACTER.sub("_", value)[:128] or "unknown"


def create_app(
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create an adapter application with optional test dependencies."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or get_settings()
        logger.setLevel(active_settings.log_level)
        app.state.settings = active_settings
        app.state.conversation_store = ConversationStore(
            active_settings.dify_conversation_db_path
        )
        async with httpx.AsyncClient(
            timeout=active_settings.weknora_timeout_seconds,
            transport=transport,
        ) as http_client:
            app.state.weknora_client = WeKnoraClient(
                api_url=active_settings.weknora_api_url,
                api_key=active_settings.weknora_api_key.get_secret_value(),
                http_client=http_client,
            )
            yield

    application = FastAPI(
        title="WeKnora-Dify Adapter",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = _request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        started_at = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_complete",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    @application.exception_handler(AdapterError)
    async def handle_adapter_error(request: Request, exc: AdapterError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        if request.url.path.startswith("/v1/"):
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "code": exc.error_code,
                    "message": exc.message,
                    "status": exc.status_code,
                },
                headers=headers,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.error_code, "message": exc.message},
            headers=headers,
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, __: RequestValidationError
    ) -> JSONResponse:
        if request.url.path.startswith("/v1/"):
            return JSONResponse(
                status_code=400,
                content={
                    "code": "invalid_param",
                    "message": "Request validation failed",
                    "status": 400,
                },
            )
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "invalid_request",
                "message": "Request validation failed",
            },
        )

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/chat-messages")
    async def chat_messages(
        payload: DifyChatRequest,
        request: Request,
        _: Annotated[None, Depends(authenticate)],
    ) -> StreamingResponse:
        active_settings: Settings = request.app.state.settings
        if not active_settings.weknora_agent_id:
            raise AdapterError(
                503,
                "agent_not_configured",
                "Set WEKNORA_AGENT_ID to enable Agent chat",
            )
        if payload.files:
            raise AdapterError(400, "unsupported_files", "File inputs are not supported")

        store: ConversationStore = request.app.state.conversation_store
        client: WeKnoraClient = request.app.state.weknora_client
        is_new_conversation = not payload.conversation_id
        if payload.conversation_id:
            session_id = store.get(payload.user, payload.conversation_id)
            if session_id is None:
                raise AdapterError(
                    404,
                    "conversation_not_found",
                    "Conversation was not found for this user",
                )
            conversation_id = payload.conversation_id
        else:
            session_id = await client.create_session(
                api_key=active_settings.weknora_chat_key,
                title=payload.query[:80],
            )
            conversation_id = store.create(payload.user, session_id)

        upstream_response = await client.open_agent_chat(
            api_key=active_settings.weknora_chat_key,
            session_id=session_id,
            query=payload.query,
            agent_id=active_settings.weknora_agent_id,
            knowledge_base_ids=active_settings.weknora_kb_ids,
        )
        return StreamingResponse(
            dify_stream(
                response=upstream_response,
                conversation_id=conversation_id,
                client=client,
                weknora_api_key=active_settings.weknora_chat_key,
                session_id=session_id,
                user_query=payload.query,
                generate_title_on_complete=is_new_conversation,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @application.post("/retrieval", response_model=DifyRetrievalResponse)
    async def retrieval(
        payload: DifyRetrievalRequest,
        request: Request,
        _: Annotated[None, Depends(authenticate)],
    ) -> DifyRetrievalResponse:
        started_at = time.perf_counter()
        mapped_request = map_request(payload)
        safe_kb_id = _safe_log_id(mapped_request.knowledge_base_id)
        logger.info(
            "retrieval_started",
            extra={
                "request_id": request.state.request_id,
                "knowledge_base_id": safe_kb_id,
            },
        )
        if mapped_request.short_circuit_empty:
            logger.info(
                "retrieval_complete",
                extra={
                    "request_id": request.state.request_id,
                    "knowledge_base_id": safe_kb_id,
                    "result_count": 0,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
                },
            )
            return DifyRetrievalResponse(records=[])

        envelope = await request.app.state.weknora_client.hybrid_search(
            mapped_request.knowledge_base_id,
            mapped_request.payload,
        )
        mapped_response = map_response(
            envelope,
            knowledge_base_id=mapped_request.knowledge_base_id,
            top_k=payload.retrieval_setting.top_k,
            score_threshold=payload.retrieval_setting.score_threshold,
        )
        logger.info(
            "retrieval_complete",
            extra={
                "request_id": request.state.request_id,
                "knowledge_base_id": safe_kb_id,
                "result_count": len(mapped_response.response.records),
                "skipped_ids": [_safe_log_id(value) for value in mapped_response.skipped_ids],
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            },
        )
        return mapped_response.response

    return application


app = create_app()
