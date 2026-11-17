"""Dify Chat API SSE conversion for WeKnora Agent responses."""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx

from app.client import WeKnoraClient
from app.errors import AdapterError

logger = logging.getLogger("weknora_dify_adapter.chat")


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def _retriever_resource(reference: Any, position: int) -> dict[str, Any] | None:
    if not isinstance(reference, dict):
        return None
    content = reference.get("content")
    if not isinstance(content, str):
        return None
    return {
        "position": position,
        "dataset_id": reference.get("knowledge_base_id", ""),
        "dataset_name": reference.get("knowledge_base_name", ""),
        "document_id": reference.get("knowledge_id", reference.get("document_id", "")),
        "document_name": reference.get(
            "knowledge_title", reference.get("document_name", "")
        ),
        "data_source_type": reference.get("source", ""),
        "segment_id": reference.get("id", reference.get("chunk_id", "")),
        "score": reference.get("score", 0),
        "content": content,
        "hit_count": 0,
        "word_count": len(content),
        "segment_position": reference.get("chunk_index", 0),
        "index_node_hash": "",
        "retriever_from": "weknora",
    }


def _usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    prompt_tokens = value.get("prompt_tokens", 0)
    completion_tokens = value.get("completion_tokens", 0)
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None
    return {
        "prompt_tokens": prompt_tokens,
        "prompt_unit_price": "0",
        "prompt_price_unit": "0",
        "prompt_price": "0",
        "completion_tokens": completion_tokens,
        "completion_unit_price": "0",
        "completion_price_unit": "0",
        "completion_price": "0",
        "total_tokens": value.get("total_tokens", prompt_tokens + completion_tokens),
        "total_price": "0",
        "currency": "USD",
        "latency": 0,
    }


async def _events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                raw_data = "\n".join(data_lines)
                data_lines.clear()
                try:
                    event = json.loads(raw_data)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        try:
            event = json.loads("\n".join(data_lines))
        except ValueError:
            return
        if isinstance(event, dict):
            yield event


async def dify_stream(
    *,
    response: httpx.Response,
    conversation_id: str,
    client: WeKnoraClient,
    weknora_api_key: str,
    session_id: str,
    user_query: str,
    generate_title_on_complete: bool,
) -> AsyncIterator[bytes]:
    """Translate a WeKnora event stream into Dify Chat API events."""

    task_id = str(uuid4())
    message_id = str(uuid4())
    created_at = int(time.time())
    references: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    title_answer = ""
    completed = False
    try:
        async for upstream in _events(response):
            event_type = upstream.get("response_type")
            usage = _usage(upstream.get("usage")) or usage
            if event_type == "answer":
                chunk = upstream.get("content")
                if isinstance(chunk, str) and chunk:
                    if len(title_answer) < 6000:
                        title_answer += chunk[: 6000 - len(title_answer)]
                    yield _sse(
                        {
                            "event": "message",
                            "task_id": task_id,
                            "id": message_id,
                            "message_id": message_id,
                            "conversation_id": conversation_id,
                            "created_at": created_at,
                            "answer": chunk,
                        }
                    )
            elif event_type == "references":
                candidates = upstream.get("knowledge_references")
                if isinstance(candidates, list):
                    for candidate in candidates:
                        mapped = _retriever_resource(candidate, len(references) + 1)
                        if mapped is not None:
                            references.append(mapped)
            elif event_type == "ping":
                yield _sse({"event": "ping"})
            elif event_type == "error":
                yield _sse(
                    {
                        "event": "error",
                        "task_id": task_id,
                        "message_id": message_id,
                        "status": 502,
                        "code": "weknora_agent_error",
                        "message": "WeKnora could not complete the answer",
                    }
                )
                return
            elif event_type == "complete":
                completed = True
                if generate_title_on_complete:
                    try:
                        await client.generate_session_title(
                            api_key=weknora_api_key,
                            session_id=session_id,
                            messages=[
                                {"role": "user", "content": user_query[:6000]},
                                {"role": "assistant", "content": title_answer},
                            ],
                        )
                    except Exception:
                        logger.warning("conversation_title_update_failed")
                metadata: dict[str, Any] = {"retriever_resources": references}
                if usage is not None:
                    metadata["usage"] = usage
                yield _sse(
                    {
                        "event": "message_end",
                        "task_id": task_id,
                        "id": message_id,
                        "message_id": message_id,
                        "conversation_id": conversation_id,
                        "created_at": created_at,
                        "metadata": metadata,
                        "files": None,
                    }
                )
                return
        if not completed:
            yield _sse(
                {
                    "event": "error",
                    "task_id": task_id,
                    "message_id": message_id,
                    "status": 502,
                    "code": "upstream_stream_incomplete",
                    "message": "WeKnora closed the stream before completing the answer",
                }
            )
    except httpx.TimeoutException:
        yield _sse(
            {
                "event": "error",
                "task_id": task_id,
                "message_id": message_id,
                "status": 504,
                "code": "upstream_timeout",
                "message": "WeKnora timed out while generating the answer",
            }
        )
    except httpx.RequestError:
        yield _sse(
            {
                "event": "error",
                "task_id": task_id,
                "message_id": message_id,
                "status": 502,
                "code": "upstream_unavailable",
                "message": "The WeKnora stream was interrupted",
            }
        )
    finally:
        await response.aclose()
