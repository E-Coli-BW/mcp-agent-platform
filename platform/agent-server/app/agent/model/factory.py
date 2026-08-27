"""Provider-native chat model construction."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import settings


def create_chat_model(
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """Create the provider-native LangChain chat model."""
    name = model_name or settings.default_model
    resolved_temperature = (
        temperature if temperature is not None else settings.default_temperature
    )
    requested_max_tokens = settings.max_tokens if max_tokens is None else int(max_tokens)
    if requested_max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    resolved_max_tokens = min(requested_max_tokens, settings.max_tokens)

    if name.startswith(("openai/", "gpt-")):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=name.replace("openai/", ""),
            api_key=settings.openai_api_key,
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            max_retries=0,
        )

    if name.startswith(("anthropic/", "claude")):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=name.replace("anthropic/", ""),
            api_key=settings.anthropic_api_key,
            base_url="https://api.anthropic.com/v1",
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            max_retries=0,
        )

    if name.startswith("mlx/"):
        from langchain_openai import ChatOpenAI

        extra_body = None
        if settings.mlx_enable_thinking is not None:
            extra_body = {"enable_thinking": settings.mlx_enable_thinking}
        return ChatOpenAI(
            model=name.replace("mlx/", ""),
            api_key="mlx-local",
            base_url=settings.mlx_base_url + "/v1",
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            streaming=False,
            max_retries=0,
            extra_body=extra_body,
        )

    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=name.replace("ollama/", ""),
        base_url=settings.ollama_base_url,
        temperature=resolved_temperature,
        num_predict=resolved_max_tokens,
    )


def create_chat_model_with_fallback(
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """Wrap the primary model with the configured fallback when present."""
    primary = create_chat_model(
        model_name,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not settings.fallback_model:
        return primary

    from app.agent.model.fallback import ChatModelWithFallback

    fallback = create_chat_model(
        settings.fallback_model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return ChatModelWithFallback(
        primary=primary,
        fallback=fallback,
        primary_name=model_name or settings.default_model,
        fallback_name=settings.fallback_model,
    )
