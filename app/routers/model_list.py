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
    "google/gemma-4-31b-it:free":        "google",
    "sarvam-30b":          "Professor.Lai",
    "sarvam-105b":        "Professor.Lai",
    # OpenAI
    "qwen/qwen3-vl-30b-a3b-thinking":                  "qwen",
    "nvidia/nemotron-nano-12b-v2-vl":             "nvidia",
    "deepseek/deepseek-v3.2":                 "dicksuck",
    "deepseek/deepseek-chat-v3-0324":                 "dicksuck",
    # Anthropic
    "sonar":           "Professor.Lai",
    # Meta
    "meta-llama/llama-4-scout-17b-16e-instruct":           "meta",
    "gpt-5.4-mini-2": "Prof.Lai"
    # xAI
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
                "owned_by":   "Professor.Lai",
            }
            for m in SUPPORTED_MODELS
        ],
    }
