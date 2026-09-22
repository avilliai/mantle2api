# app/routers/chat.py

"""
对话接口路由：
  POST /v1/chat/completions   OpenAI 兼容（流式 / 非流式）
功能：
  - Function Calling 伪实现（@@TOOL_CALL_BEGIN@@ 标记注入与解析）
  - 流式 SSE 与非流式统一适配
  - 账号失败重试与健康度反馈
"""
import asyncio
import copy
import json
import logging
import random
import re
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.cookie_manager import cm
from app.mantle_client import (
    messages_to_text,
    stream_mantle_response,
    EmptyResponseError,
    ServerError,
)
from app.models import AccountState
from app.routers.model_list import resolve_model, DEFAULT_MODEL

log = logging.getLogger("mantle_proxy.chat")

router = APIRouter()

MAX_RETRIES = 3
RETRY_DELAY = 1.0
RETRY_DELAY_MAX = 3.0

# ══════════════════════════════════════════════════════════
# 工具调用（Function Calling）伪实现
# ══════════════════════════════════════════════════════════
_TC_BEGIN = "@@TOOL_CALL_BEGIN@@"
_TC_END = "@@TOOL_CALL_END@@"
_TC_PATTERN = re.compile(
    rf"{re.escape(_TC_BEGIN)}\s*(.*?)\s*{re.escape(_TC_END)}",
    re.DOTALL,
)


def _backoff(attempt: int) -> float:
    delay = min(RETRY_DELAY * (2 ** (attempt - 1)), RETRY_DELAY_MAX)
    return delay + random.uniform(0, delay * 0.1)


def _convert_messages(messages: list[dict]) -> list[dict]:
    """标准化消息，保证结构安全"""
    converted = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # 如果是 tool 响应角色，转化为 user 身份供模型阅读
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            converted.append({
                "role": "user",
                "content": f"[Tool Result for {tool_call_id}]: {content}",
            })
        else:
            converted.append({"role": role, "content": content})
    return converted


# ── Function Calling 工具注入与解析 ──────────────────────────

def _annotate_tool_declarations(tools: list[dict]) -> list[dict]:
    """在工具参数 description 里注入【必填】/【可选】标签，提升模型遵从率"""
    annotated = copy.deepcopy(tools)
    for tool in annotated:
        func = tool if "parameters" in tool else tool.get("function", {})
        params = func.get("parameters", {})
        required_set = set(params.get("required", []))
        props = params.get("properties", {})
        for param_name, param_def in props.items():
            tag = "【必填】" if param_name in required_set else "【可选】"
            existing = param_def.get("description", "")
            param_def["description"] = f"{tag} {existing}".strip()
        if required_set:
            params["_required_params_notice"] = (
                f"以下参数为必填，绝对不能省略或传空值：{sorted(required_set)}"
            )
    return annotated


def _validate_tool_call(tc: dict, tool_declarations: list[dict]) -> tuple[bool, list[str]]:
    name = tc.get("name", "")
    args = tc.get("arguments", {})
    for tool in tool_declarations:
        func = tool if "parameters" in tool else tool.get("function", {})
        func_name = func.get("name", "")
        if func_name != name:
            continue
        params = func.get("parameters", {})
        required = params.get("required", [])
        missing = [r for r in required if r not in args or args[r] in (None, "", {}, [])]
        if missing:
            log.warning("tool call [%s] ??????: %s", name, missing)
            return False, missing
        return True, []
    log.warning("tool call [%s] ??????????????", name)
    return True, []


def _build_tool_system_prompt(tool_declarations: list[dict]) -> str:
    annotated = _annotate_tool_declarations(tool_declarations)
    tool_json = json.dumps(annotated, ensure_ascii=False, indent=2)
    return f"""## 核心设定：外部工具调用系统

你被赋予了调用外部真实系统（工具）的能力。当用户的需求需要真实执行（如画图、查询、搜索）时，你必须精确输出工具调用代码块，而不能假装执行。

### 🚨 绝对禁止的错误模式

❌ 只演戏，不调用（没有输出 `{_TC_BEGIN}` 代码块）
❌ 假装已经拿到结果（没有真正调用工具就给出答案）
❌ 需要参数却传空对象：`"arguments": {{}}` —— 这等于没有调用！

### 🔒 调用前必须完成的自检

在输出任何 `{_TC_BEGIN}` 块之前，逐项确认：
1. 工具名称来自下方【可用工具】列表
2. 每个【必填】参数是否都有具体的、非空的值
3. 如果某个【必填】参数还不知道 → **先向用户提问，不要调用**

### ✅ 正确的调用格式

过渡语（可选）……
{_TC_BEGIN}
{{
  "name": "工具名",
  "arguments": {{
    "参数名": "参数值"
  }}
}}
{_TC_END}

**注意：**
- `{_TC_BEGIN}` 和 `{_TC_END}` 必须独占一行
- 之间只能有纯净的 JSON，不能有 Markdown 代码块标记
- 输出完 `{_TC_END}` 后立即停止，等待系统返回结果
- 【必填】参数不能是空字符串 `""`、`null` 或空对象 `{{}}`

### 📌 当前【可用工具】列表

{tool_json}

### ⚠️ 系统最终警告

- 假装执行而不输出代码块 → 严重惩罚
- 输出代码块但【必填】参数为空或缺失 → 视同未调用，严重惩罚"""


def _parse_tool_calls(text: str) -> tuple[str, list[dict]]:
    """从文本中提取所有工具调用，返回 (去除调用块后的纯文本, tool_calls列表)"""
    tool_calls: list[dict] = []
    for m in _TC_PATTERN.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
            tc = {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "name": obj.get("name", ""),
                "arguments": obj.get("arguments", {}),
            }
            tool_calls.append(tc)
            log.info("解析到工具调用: %s", tc)
        except json.JSONDecodeError:
            log.warning("无法解析工具调用块: %r", raw)
    clean = _TC_PATTERN.sub("", text).strip()
    return clean, tool_calls


def _check_truncation(
    full_text: str, original_tools: list[dict]
) -> tuple[bool, str, list[dict]]:
    """检测工具调用块是否被上游截断（有 BEGIN 但没有 END）"""
    begin_idx = full_text.find(_TC_BEGIN)
    end_idx = full_text.find(_TC_END)
    if begin_idx != -1 and end_idx == -1:
        fallback_text = full_text[:begin_idx].strip()
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', full_text[begin_idx:])
        next_tools = original_tools
        if name_match:
            target_name = name_match.group(1)
            filtered = [
                t for t in original_tools
                if t.get("name") == target_name
                or t.get("function", {}).get("name") == target_name
            ]
            if filtered:
                next_tools = filtered
        return True, fallback_text, next_tools
    return False, "", original_tools


# ── 响应格式化 ────────────────────────────────────────────

def _make_tool_call_response(tool_calls: list[dict], clean_text: str, model: str, usage: Optional[dict] = None) -> dict:
    openai_tool_calls = [
        {
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["name"],
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
            },
        }
        for tc in tool_calls
    ]
    message: dict[str, Any] = {
        "role": "assistant",
        "content": clean_text or None,
        "tool_calls": openai_tool_calls,
    }
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _make_tool_call_sse(tool_calls: list[dict], clean_text: str, model: str, req_id: str, created_time: int) -> list[str]:
    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    chunks = [
        _sse({"id": req_id, "object": "chat.completion.chunk", "created": created_time, "model": model,
              "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
    ]
    if clean_text:
        chunks.append(
            _sse({"id": req_id, "object": "chat.completion.chunk", "created": created_time, "model": model,
                  "choices": [{"index": 0, "delta": {"content": clean_text}, "finish_reason": None}]})
        )
    openai_tool_calls_delta = [
        {
            "index": i,
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["name"],
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
            },
        }
        for i, tc in enumerate(tool_calls)
    ]
    chunks.append(
        _sse({"id": req_id, "object": "chat.completion.chunk", "created": created_time, "model": model,
              "choices": [{"index": 0, "delta": {"tool_calls": openai_tool_calls_delta}, "finish_reason": None}]})
    )
    chunks.append(
        _sse({"id": req_id, "object": "chat.completion.chunk", "created": created_time, "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    )
    chunks.append("data: [DONE]\n\n")
    return chunks


# ══════════════════════════════════════════════════════════
# 主路由
# ══════════════════════════════════════════════════════════

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    raw_messages = body.get("messages", [])
    if not raw_messages:
        raise HTTPException(400, "messages list cannot be empty")

    stream = bool(body.get("stream", False))
    raw_model = body.get("model", DEFAULT_MODEL)
    tools = body.get("tools", [])
    has_tools = bool(tools)

    real_model, _ = resolve_model(raw_model)
    clean_messages = _convert_messages(raw_messages)

    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_time = int(time.time())

    # ══ 流式 ═══════════════════════════════════════════════
    if stream:
        async def sse_generator():
            account = None
            current_tools = list(tools)
            attempt = 0

            while attempt < MAX_RETRIES:
                attempt += 1
                try:
                    account = await cm.get_account()

                    # 若开启 tools，当请求包含工具调用系统时，为了完整捕获并格式化返回 tool_calls，做缓冲/解析
                    # 或者常规流式正常直出
                    attempt_messages = list(clean_messages)
                    if has_tools and current_tools:
                        tool_sys = _build_tool_system_prompt(current_tools)
                        insert_pos = next(
                            (i + 1 for i, m in enumerate(attempt_messages) if m.get("role") == "system"), 0
                        )
                        attempt_messages.insert(insert_pos, {"role": "system", "content": tool_sys})

                    prompt = messages_to_text(attempt_messages)
                    chat_id = str(uuid.uuid4())

                    if has_tools:
                        # 收集流式输出进行 tool_calls 探测
                        collected_text = []
                        last_usage = None
                        async for chunk in stream_mantle_response(account, real_model, prompt, chat_id):
                            if "delta" in chunk:
                                collected_text.append(chunk["delta"])
                            if "usage" in chunk and chunk["usage"]:
                                last_usage = chunk["usage"]

                        full_text = "".join(collected_text)
                        if not full_text:
                            raise EmptyResponseError("Empty stream content")

                        # 截断检测
                        is_truncated, fallback_text, next_tools = _check_truncation(full_text, tools)
                        if is_truncated and attempt < MAX_RETRIES:
                            current_tools = next_tools
                            log.warning("Stream tools 调用被截断，缩减后重试...")
                            continue

                        clean_text, tool_calls = _parse_tool_calls(full_text)

                        # ??????
                        if tool_calls:
                            invalid = []
                            for tc in tool_calls:
                                valid, missing = _validate_tool_call(tc, tools)
                                if not valid:
                                    invalid.append((tc["name"], missing))
                            if invalid:
                                if attempt < MAX_RETRIES:
                                    log.warning("stream(tools) attempt %d ?????? %s???", attempt, invalid)
                                    continue
                                else:
                                    log.error("stream(tools) ?????? %s????????????", invalid)

                        if tool_calls:
                            sse_chunks = _make_tool_call_sse(tool_calls, clean_text, raw_model, req_id, created_time)
                            for c in sse_chunks:
                                yield c
                            await cm.report_success(account)
                            return

                        # 无工具调用，退化为普通流式文本输出
                        header = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": raw_model,
                            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(header, ensure_ascii=False)}\n\n"

                        content_chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": raw_model,
                            "choices": [{"index": 0, "delta": {"content": full_text}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"

                        finish_chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": raw_model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": last_usage
                        }
                        yield f"data: {json.dumps(finish_chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        await cm.report_success(account)
                        return

                    else:
                        # 纯对话标准实时流式直出
                        first_chunk = True
                        has_yielded = False
                        async for chunk in stream_mantle_response(account, real_model, prompt, chat_id):
                            if first_chunk:
                                header_data = {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": raw_model,
                                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                                }
                                yield f"data: {json.dumps(header_data, ensure_ascii=False)}\n\n"
                                first_chunk = False

                            if "delta" in chunk:
                                has_yielded = True
                                chunk_data = {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": raw_model,
                                    "choices": [{"index": 0, "delta": {"content": chunk["delta"]}, "finish_reason": None}]
                                }
                                yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"

                            if "finish_reason" in chunk:
                                end_data = {
                                    "id": req_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": raw_model,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": chunk["finish_reason"]}],
                                    "usage": chunk.get("usage")
                                }
                                yield f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n"

                        yield "data: [DONE]\n\n"
                        await cm.report_success(account)
                        return

                except Exception as e:
                    log.warning(f"流式调用重试 {attempt}/{MAX_RETRIES}, 账号={account.email if account else 'None'}: {e}")
                    if account:
                        await cm.report_failure(account, str(e))
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(_backoff(attempt))
                    else:
                        err_chunk = {
                            "error": {
                                "message": f"All retries failed: {e}",
                                "type": "server_error"
                            }
                        }
                        yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ══ 非流式 ═════════════════════════════════════════════
    last_err = None
    current_tools = list(tools)

    for attempt in range(1, MAX_RETRIES + 1):
        account = None
        try:
            account = await cm.get_account()

            attempt_messages = list(clean_messages)
            if has_tools and current_tools:
                tool_sys = _build_tool_system_prompt(current_tools)
                insert_pos = next(
                    (i + 1 for i, m in enumerate(attempt_messages) if m.get("role") == "system"), 0
                )
                attempt_messages.insert(insert_pos, {"role": "system", "content": tool_sys})

            prompt = messages_to_text(attempt_messages)
            chat_id = str(uuid.uuid4())

            collected_text = []
            finish_reason = "stop"
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

            async for chunk in stream_mantle_response(account, real_model, prompt, chat_id):
                if "delta" in chunk:
                    collected_text.append(chunk["delta"])
                if "finish_reason" in chunk:
                    finish_reason = chunk["finish_reason"]
                if "usage" in chunk and chunk["usage"]:
                    usage = chunk["usage"]

            full_content = "".join(collected_text)
            if not full_content:
                raise EmptyResponseError("Empty content received")

            # 工具调用检测与解析
            if has_tools:
                is_truncated, fallback_text, next_tools = _check_truncation(full_content, tools)
                if is_truncated and attempt < MAX_RETRIES:
                    current_tools = next_tools
                    log.warning("非流式 tools 调用被截断，缩减后重试...")
                    continue

                clean_text, tool_calls = _parse_tool_calls(full_content)

                # ??????
                if tool_calls:
                    invalid = []
                    for tc in tool_calls:
                        valid, missing = _validate_tool_call(tc, tools)
                        if not valid:
                            invalid.append((tc["name"], missing))
                    if invalid:
                        if attempt < MAX_RETRIES:
                            log.warning("non-stream(tools) attempt %d ?????? %s???", attempt, invalid)
                            continue
                        else:
                            log.error("non-stream(tools) ?????? %s????????????", invalid)

                if tool_calls:
                    await cm.report_success(account)
                    return JSONResponse(
                        content=_make_tool_call_response(tool_calls, clean_text, raw_model, usage)
                    )

            await cm.report_success(account)

            return JSONResponse(
                content={
                    "id": req_id,
                    "object": "chat.completion",
                    "created": created_time,
                    "model": raw_model,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_content
                        },
                        "finish_reason": finish_reason
                    }],
                    "usage": usage
                }
            )
        except Exception as e:
            last_err = e
            log.warning(f"非流式调用重试 {attempt}/{MAX_RETRIES}, 账号={account.email if account else 'None'}: {e}")
            if account:
                await cm.report_failure(account, str(e))
            if attempt < MAX_RETRIES:
                await asyncio.sleep(_backoff(attempt))

    raise HTTPException(500, f"Chat completion failed after retries: {last_err}")