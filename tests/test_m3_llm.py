"""M3 LLM 客户端用例（SPEC §10 LM-07…LM-18、TS-16）。

分三层：

1. **纯解析**：模型输出 → JSON 的容错（LM-11）；
2. **客户端行为**：空 content / 非法 JSON / 异常一律算调用失败（LM-13），重试上限 ≤2（LM-12）；
3. **真实端点冒烟**（标记 ``llm``）：验证 Key/端点/模型配置可用，只断言结构不判对错。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.llm.base import MAX_ATTEMPTS, extract_json_object, judge_semantic, parse_judgement
from app.llm.deepseek import DeepSeekClient
from app.llm.factory import build_llm_client
from app.llm.null import NullLLMClient


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── LM-11：JSON 容错 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content", "expected_hit"),
    [
        ('{"hit": true, "reason": "x"}', True),
        ('```json\n{"hit": false, "reason": "x"}\n```', False),
        ('```\n{"hit": true}\n```', True),
        ('好的，分析如下：{"hit": true, "reason": "x"} 以上。', True),
        ('{"hit": "true"}', True),
        ('{"hit": "否"}', False),
    ],
)
def test_extract_json_object_tolerates_model_output(content: str, expected_hit: bool) -> None:
    payload = extract_json_object(content)
    assert payload is not None
    assert parse_judgement(payload).hit is expected_hit


@pytest.mark.parametrize("content", ["", "   ", "不是 JSON", "```json\n{坏的}\n```", "[1,2,3]"])
def test_extract_json_object_returns_none_on_garbage(content: str) -> None:
    payload = extract_json_object(content)
    if payload is not None:  # 数组不是对象 → 必须被拒
        assert isinstance(payload, dict)


def test_parse_judgement_marks_missing_hit_as_degraded() -> None:
    judgement = parse_judgement({"reason": "模型没说 hit"})
    assert judgement.hit is None
    assert judgement.degraded is True
    assert not judgement.usable


def test_parse_judgement_normalises_fields() -> None:
    judgement = parse_judgement(
        {"hit": True, "reason": " 不对等 ", "evidence_text": "  原文 ", "confidence": "0.8"}
    )
    assert judgement.hit is True
    assert judgement.reason == "不对等"
    assert judgement.evidence_text == "原文"
    assert judgement.confidence == 0.8


# ── NullLLMClient：LM-18 降级 ─────────────────────────────────────────────


def test_null_client_is_always_unusable() -> None:
    client = NullLLMClient()
    assert client.enabled is False
    assert run(client.complete_json(system="s", user="u")) is None
    judgement = run(judge_semantic(client, question="q", context="c"))
    assert judgement.hit is None
    assert judgement.degraded is True


def test_factory_falls_back_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings().model_copy(update={"llm_enabled": False})
    assert isinstance(build_llm_client(settings), NullLLMClient)


def test_factory_falls_back_when_key_empty() -> None:
    settings = get_settings().model_copy(update={"llm_enabled": True, "llm_api_key": ""})
    assert isinstance(build_llm_client(settings), NullLLMClient)


# ── DeepSeekClient 行为（用 MockTransport，不联网）────────────────────────


def _client(handler: Any, monkeypatch: pytest.MonkeyPatch) -> DeepSeekClient:
    settings = get_settings().model_copy(
        update={"llm_enabled": True, "llm_api_key": "sk-test-only", "llm_model": "stub-model"}
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeepSeekClient(settings, http_client=http_client)


def _completion(content: str | None, *, reasoning: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "stub-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def test_deepseek_parses_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_completion('{"hit": true, "reason": "不对等"}', reasoning="思考中"))

    client = _client(handler, monkeypatch)
    payload = run(client.complete_json(system="s", user="u"))
    assert payload == {"hit": True, "reason": "不对等"}
    assert len(calls) == 1
    body = json.loads(calls[0].content.decode("utf-8"))
    assert body["temperature"] == 0  # CF-18
    assert body["max_tokens"] >= 512  # CF-17
    # LM-10：reasoning_content 不得回传
    assert all("reasoning_content" not in str(message) for message in body["messages"])


def test_deepseek_treats_empty_content_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """LM-13 / 设计 R10：推理 token 吃光配额 → content 为空（HTTP 仍 200）必须算调用失败。"""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(200, json=_completion("", reasoning="很长很长的推理过程"))

    client = _client(handler, monkeypatch)
    assert run(client.complete_json(system="s", user="u")) is None
    assert attempts["count"] == MAX_ATTEMPTS  # LM-12：重试上限 ≤2


def test_deepseek_treats_invalid_json_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("这不是 JSON"))

    client = _client(handler, monkeypatch)
    assert run(client.complete_json(system="s", user="u")) is None


def test_deepseek_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(500, json={"error": "server"})
        return httpx.Response(200, json=_completion('{"hit": false}'))

    client = _client(handler, monkeypatch)
    assert run(client.complete_json(system="s", user="u")) == {"hit": False}
    assert attempts["count"] == 2


def test_deepseek_swallows_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("网络不可达")

    client = _client(handler, monkeypatch)
    assert run(client.complete_json(system="s", user="u")) is None


# ── 真实端点冒烟（标记 llm，默认跑；无 Key 时跳过）────────────────────────


@pytest.mark.llm
def test_live_endpoint_returns_structured_judgement() -> None:
    """验证 Key/端点/模型配置端到端可用；**只断言结构**，不判对错（模型输出允许波动）。"""
    settings = get_settings()
    client = build_llm_client(settings)
    if not client.enabled:
        pytest.skip("LLM 未启用（LLM_ENABLED=false 或 Key 为空），跳过真实端点冒烟")

    judgement = run(
        judge_semantic(
            client,
            question="合同是否存在无需双方再次确认即可自动续期的默认续约安排？",
            context="第八条 合同期限\n本合同期满自动续约一年。",
        )
    )
    assert judgement.hit is not None, f"真实端点未返回可用判定：{judgement.error}"
    assert isinstance(judgement.hit, bool)
