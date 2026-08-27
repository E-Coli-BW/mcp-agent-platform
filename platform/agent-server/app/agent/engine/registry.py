"""Engine-owned Agent definition resolution and instance caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, TypeAlias

from app.agent.agent_graph.checkpoint_store import ensure_async_checkpointer
from app.agent.execution_policy.system_prompts import resolve_system_prompt
from app.auth.middleware import tenant_context
from app.config import settings
from app.context.request_context import set_agent_contract, set_prompt_version

logger = logging.getLogger(__name__)

AgentCacheKey: TypeAlias = tuple[str, float, str, str]


class LangGraphAgentRegistry:
    """Resolve Agent definitions and cache compiled graphs by effective contract."""

    def __init__(self) -> None:
        self._agents: dict[AgentCacheKey, object] = {}
        self._tool_names: dict[AgentCacheKey, set[str]] = {}
        self._config_fingerprints: dict[AgentCacheKey, str] = {}
        self._lock = asyncio.Lock()

    @property
    def agents(self) -> dict[AgentCacheKey, object]:
        return self._agents

    @property
    def tool_names(self) -> dict[AgentCacheKey, set[str]]:
        return self._tool_names

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def invalidate(self) -> None:
        """Atomically replace caches while in-flight Agents retain references."""
        self._agents = {}
        self._tool_names = {}
        self._config_fingerprints = {}

    async def get_agent(
        self,
        model_name: str | None = None,
        temperature: float | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        prompt_version: str | None = None,
    ) -> Any:
        """Return a graph cached by model/config, temperature, tenant, and prompt."""
        from app.registry.config_loader import load_all_configs
        from app.tools.definitions import get_tools

        resolved_temperature = (
            temperature if temperature is not None else settings.default_temperature
        )
        effective_tenant = tenant_id or tenant_context.get() or "default"
        prompt = resolve_system_prompt(
            requested_version=prompt_version,
            tenant_id=effective_tenant,
            session_id=session_id,
        )
        set_prompt_version(prompt.version)
        cache_key: AgentCacheKey = (
            model_name or "coding-agent",
            resolved_temperature,
            effective_tenant,
            prompt.content_hash,
        )

        async with self._lock:
            await ensure_async_checkpointer()
            config = load_all_configs(settings.agent_config_dir).get(cache_key[0])

            if config is not None:
                set_agent_contract(
                    config.id,
                    config.knowledge.get("collection", ""),
                )
                current_tool_names = set(config.tools)
                config_fingerprint = self._config_fingerprint(config)
                self._ensure_configured_agent(
                    cache_key,
                    config,
                    current_tool_names,
                    config_fingerprint,
                    temperature,
                )
                return self._agents[cache_key]

            set_agent_contract("", "")
            current_tools = get_tools()
            current_tool_names = {tool.name for tool in current_tools}
            resolved_model = (
                settings.default_model
                if cache_key[0] == "coding-agent"
                else cache_key[0]
            )
            self._ensure_fallback_agent(
                cache_key,
                resolved_model,
                current_tool_names,
                temperature,
            )
            return self._agents[cache_key]

    def _ensure_configured_agent(
        self,
        cache_key: AgentCacheKey,
        config: Any,
        current_tool_names: set[str],
        config_fingerprint: str,
        temperature: float | None,
    ) -> None:
        cached_tools = self._tool_names.get(cache_key, set())
        cached_fingerprint = self._config_fingerprints.get(cache_key)
        if (
            cache_key in self._agents
            and current_tool_names == cached_tools
            and config_fingerprint == cached_fingerprint
        ):
            return
        self._log_rebuild(cache_key, cached_tools, current_tool_names)
        from app.agent.agent_graph.factory import create_agent_v2_from_config

        agent = create_agent_v2_from_config(config, temperature=temperature)
        self._agents[cache_key] = agent
        self._tool_names[cache_key] = current_tool_names
        self._config_fingerprints[cache_key] = config_fingerprint

    @staticmethod
    def _config_fingerprint(config: Any) -> str:
        graph_contract = {
            key: getattr(config, key, None)
            for key in (
                "id",
                "model",
                "prompt",
                "tools",
                "guardrails",
                "routing",
                "knowledge",
                "runtime",
            )
        }
        encoded = json.dumps(
            graph_contract,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _ensure_fallback_agent(
        self,
        cache_key: AgentCacheKey,
        resolved_model: str,
        current_tool_names: set[str],
        temperature: float | None,
    ) -> None:
        cached_tools = self._tool_names.get(cache_key, set())
        if (
            cache_key in self._agents
            and current_tool_names == cached_tools
            and cache_key not in self._config_fingerprints
        ):
            return
        self._log_rebuild(cache_key, cached_tools, current_tool_names)
        from app.agent.agent_graph.factory import create_agent_v2

        agent = create_agent_v2(resolved_model, temperature=temperature)
        self._agents[cache_key] = agent
        self._tool_names[cache_key] = current_tool_names
        self._config_fingerprints.pop(cache_key, None)

    def _log_rebuild(
        self,
        cache_key: AgentCacheKey,
        cached_tools: set[str],
        current_tool_names: set[str],
    ) -> None:
        if cache_key in self._agents:
            logger.info(
                "Recreating Agent %s (T=%.2f): tools changed %s -> %s",
                cache_key[0],
                cache_key[1],
                cached_tools,
                current_tool_names,
            )


_registry = LangGraphAgentRegistry()


def get_agent_registry() -> LangGraphAgentRegistry:
    return _registry


def invalidate_agent_cache() -> None:
    _registry.invalidate()