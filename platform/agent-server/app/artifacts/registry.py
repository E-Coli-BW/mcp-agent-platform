"""Static deterministic-artifact plugin registry."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app.artifacts.base import ArtifactPlugin, VerifiedArtifact

logger = logging.getLogger(__name__)


class ArtifactRegistry:
    """Runs trusted in-process plugins and fails open to the normal ReAct path."""

    def __init__(self, plugins: Iterable[ArtifactPlugin] = ()) -> None:
        self._plugins = {plugin.plugin_id: plugin for plugin in plugins}

    def extract_file(
        self,
        *,
        path: str,
        full_text: str,
        selected_text: str,
    ) -> VerifiedArtifact | None:
        for plugin in self._plugins.values():
            try:
                artifact = plugin.extract_file(
                    path=path,
                    full_text=full_text,
                    selected_text=selected_text,
                )
            except Exception:
                logger.exception("Artifact plugin %s failed during extraction", plugin.plugin_id)
                continue
            if artifact is not None and self._validate(plugin, artifact):
                return artifact
        return None

    def select_for_prompt(
        self,
        *,
        prompt: str,
        tool_outputs: Iterable[str],
    ) -> tuple[ArtifactPlugin, VerifiedArtifact] | None:
        for output in reversed(list(tool_outputs)):
            artifact = VerifiedArtifact.from_tool_output(output)
            if artifact is None:
                continue
            plugin = self._plugins.get(artifact.plugin_id)
            if plugin is None or not self._validate(plugin, artifact):
                continue
            try:
                if plugin.accepts_request(prompt, artifact):
                    return plugin, artifact
            except Exception:
                logger.exception(
                    "Artifact plugin %s failed during request matching", plugin.plugin_id
                )
        return None

    def render_answer(
        self,
        plugin: ArtifactPlugin,
        artifact: VerifiedArtifact,
    ) -> str | None:
        try:
            return plugin.render_answer(artifact)
        except Exception:
            logger.exception("Artifact plugin %s failed during rendering", plugin.plugin_id)
            return None

    def required_tools(
        self,
        prompt: str,
        tool_outputs: Iterable[str] = (),
    ) -> tuple[str, ...]:
        observed_outputs = tuple(tool_outputs)
        required: list[str] = []
        for plugin in self._plugins.values():
            try:
                plugin_tools = plugin.required_tools(prompt, observed_outputs)
            except Exception:
                logger.exception(
                    "Artifact plugin %s failed during requirement check", plugin.plugin_id
                )
                continue
            for tool_name in plugin_tools:
                if tool_name not in required:
                    required.append(tool_name)
        return tuple(required)

    def has_verified_artifact(self, prompt: str, tool_outputs: Iterable[str]) -> bool:
        return self.select_for_prompt(prompt=prompt, tool_outputs=tool_outputs) is not None

    @staticmethod
    def _validate(plugin: ArtifactPlugin, artifact: VerifiedArtifact) -> bool:
        if artifact.plugin_id != plugin.plugin_id:
            return False
        try:
            return plugin.validate(artifact)
        except Exception:
            logger.exception("Artifact plugin %s failed during validation", plugin.plugin_id)
            return False


_registry: ArtifactRegistry | None = None


def get_artifact_registry() -> ArtifactRegistry:
    global _registry
    if _registry is None:
        from app.artifacts.step_functions import AwsStepFunctionsArtifactPlugin

        _registry = ArtifactRegistry([AwsStepFunctionsArtifactPlugin()])
    return _registry
