"""
模型列表接口路由：GET /v1/models
映射 Cosverse 模型到 provider
"""
from fastapi import APIRouter

router = APIRouter()

# model_id -> provider
MODEL_MAP: dict[str, str] = {
    # Google
    "gemini-3-flash-preview":  "google",
    "gemini-2.0-flash":        "google",
    "gemini-2.5-pro":          "google",
    "gemini-2.5-flash":        "google",
    # OpenAI
    "gpt-4o":                  "openai",
    "gpt-4o-mini":             "openai",
    "o3-mini":                 "openai",
    "o4-mini":                 "openai",
    # Anthropic
    "claude-sonnet-4-5":       "anthropic",
    "claude-opus-4":           "anthropic",
    # Meta
    "llama-3.1-70b":           "meta",
    # xAI
    "grok-3":                  "xai",
    "grok-3-mini":             "xai",
    # DeepSeek
    "deepseek-r1":             "deepseek",
    "deepseek-v3":             "deepseek",
    # Mistral
    "mistral-large":           "mistral",
    "codestral":               "mistral",
}

SUPPORTED_MODELS = list(MODEL_MAP.keys())
DEFAULT_MODEL    = "gemini-3-flash-preview"
DEFAULT_PROVIDER = "google"


def resolve_model(requested: str) -> tuple[str, str]:
    """返回 (model_id, provider)"""
    if requested in MODEL_MAP:
        return requested, MODEL_MAP[requested]
    return DEFAULT_MODEL, DEFAULT_PROVIDER


@router.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id":         m,
                "object":     "model",
                "created":    1700000000,
                "owned_by":   "Cosverse",
            }
            for m in SUPPORTED_MODELS
        ],
    }
