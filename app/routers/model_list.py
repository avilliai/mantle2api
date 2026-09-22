"""
模型列表接口路由：GET /v1/models
映射 Mantle 平台可用免费模型及通用别名
"""
from fastapi import APIRouter

router = APIRouter()

# 官方支持的免费模型 -> provider
MODEL_MAP: dict[str, str] = {
    "deepseek/deepseek-v4-flash": "deepseek",
    "google/gemini-2.5-flash-lite": "google",
    "openai/gpt-5.6-luna": "openai",
    "openai/gpt-5-nano": "openai",
    "zai/glm-5.3-flash": "zai",
    "mistral/mistral-small-4": "mistral",

    # 常用别名映射
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
    "gpt-5-nano": "openai/gpt-5-nano",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "gemini-2.5-flash-lite": "google/gemini-2.5-flash-lite",
    "glm-5.3-flash": "zai/glm-5.3-flash",
    "mistral-small-4": "mistral/mistral-small-4",
}

SUPPORTED_MODELS = list(MODEL_MAP.keys())
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_PROVIDER = "deepseek"


def resolve_model(requested: str) -> tuple[str, str]:
    """返回真实的 Mantle 模型 ID 和 provider"""
    if requested in MODEL_MAP:
        real_id = MODEL_MAP[requested]
        # 如果是别名指向另一个真实 ID
        if real_id in MODEL_MAP:
            return real_id, MODEL_MAP[real_id]
        return requested, real_id
    return DEFAULT_MODEL, DEFAULT_PROVIDER


@router.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": 1700000000,
                "owned_by": MODEL_MAP.get(m, "laisizhe"),
            }
            for m in SUPPORTED_MODELS
        ],
    }

