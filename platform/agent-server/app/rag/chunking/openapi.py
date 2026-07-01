"""OpenAPI-aware chunker with fixed-size fallback for generic YAML and JSON."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.rag.chunking.code import Chunk
from app.rag.chunking.fixed_size import chunk_fixed_size

HTTP_METHODS = ("get", "post", "put", "delete")


def _load_spec(file_path: Path) -> dict[str, Any] | None:
    text = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        if file_path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _schema_type(schema: dict[str, Any] | None) -> str:
    if not isinstance(schema, dict) or not schema:
        return "object"
    if "$ref" in schema:
        return f"ref:{schema['$ref'].split('/')[-1]}"
    if schema.get("type") == "array":
        return f"array[{_schema_type(schema.get('items'))}]"
    if "enum" in schema:
        return f"enum[{', '.join(map(str, schema['enum']))}]"
    return str(schema.get("type", "object"))


def _format_constraints(schema: dict[str, Any] | None) -> str:
    if not isinstance(schema, dict):
        return ""
    constraint_keys = ("format", "pattern", "minimum", "maximum", "minLength", "maxLength")
    constraints = [f"{key}={schema[key]}" for key in constraint_keys if key in schema]
    if "enum" in schema:
        constraints.append(f"enum={schema['enum']}")
    return ", ".join(constraints)


def _format_parameters(parameters: list[dict[str, Any]]) -> str:
    if not parameters:
        return ""

    lines: list[str] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if "$ref" in parameter:
            lines.append(f"- ref: {parameter['$ref']}")
            continue

        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else parameter
        param_type = _schema_type(schema)
        required = " (required)" if parameter.get("required") else ""
        description = parameter.get("description", "")
        line = (
            f"- {parameter.get('name', 'parameter')} in {parameter.get('in', 'query')}: "
            f"{param_type}{required}"
        )
        if description:
            line += f" — {description}"
        lines.append(line)
    return "\n".join(lines)


def _format_request_body(operation: dict[str, Any]) -> str:
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        lines: list[str] = []
        for media_type, media_spec in request_body.get("content", {}).items():
            schema = media_spec.get("schema", {}) if isinstance(media_spec, dict) else {}
            lines.append(f"- {media_type}: {_schema_type(schema)}")
        return "\n".join(lines)

    lines = []
    for parameter in operation.get("parameters", []):
        if isinstance(parameter, dict) and parameter.get("in") == "body":
            lines.append(f"- body: {_schema_type(parameter.get('schema', {}))}")
    return "\n".join(lines)


def _format_responses(responses: dict[str, Any]) -> str:
    if not isinstance(responses, dict):
        return ""

    lines: list[str] = []
    for status, response in responses.items():
        if not isinstance(response, dict):
            continue
        description = response.get("description", "")
        schemas: list[str] = []
        for media_type, media_spec in response.get("content", {}).items():
            schema = media_spec.get("schema", {}) if isinstance(media_spec, dict) else {}
            schemas.append(f"{media_type}: {_schema_type(schema)}")
        if "schema" in response:
            schemas.append(_schema_type(response.get("schema", {})))
        schema_text = f" [{'; '.join(schemas)}]" if schemas else ""
        lines.append(f"- {status}: {description}{schema_text}".strip())
    return "\n".join(lines)


def _format_schema_fields(schema_name: str, schema: dict[str, Any]) -> str:
    lines = [f"Schema: {schema_name}", f"Type: {_schema_type(schema)}"]
    if description := schema.get("description"):
        lines.append(f"Description: {description}")

    required = set(schema.get("required", []))
    properties = schema.get("properties", {})
    if isinstance(properties, dict) and properties:
        lines.append("Fields:")
        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                field_schema = {}
            field_type = _schema_type(field_schema)
            field_desc = field_schema.get("description", "")
            constraints = _format_constraints(field_schema)
            suffix_parts: list[str] = []
            if field_name in required:
                suffix_parts.append("required")
            if constraints:
                suffix_parts.append(constraints)
            suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
            line = f"- {field_name}: {field_type}{suffix}"
            if field_desc:
                line += f" — {field_desc}"
            lines.append(line)
    return "\n".join(lines)


def chunk_openapi_or_fallback(file_path: Path) -> list[Chunk]:
    """Chunk OpenAPI specs into endpoint and schema chunks, or fall back to fixed size."""
    if not file_path.exists() or not file_path.is_file():
        return []

    spec = _load_spec(file_path)
    if not spec or not any(key in spec for key in ("openapi", "swagger")):
        return chunk_fixed_size(file_path)

    last_modified = datetime.fromtimestamp(file_path.stat().st_mtime)
    chunks: list[Chunk] = []

    for path_name, operations in (spec.get("paths") or {}).items():
        if not isinstance(operations, dict):
            continue
        for method in HTTP_METHODS:
            operation = operations.get(method)
            if not isinstance(operation, dict):
                continue

            lines = [f"Method: {method.upper()}", f"Path: {path_name}"]
            if summary := operation.get("summary"):
                lines.append(f"Summary: {summary}")
            if description := operation.get("description"):
                lines.append(f"Description: {description}")
            parameters = _format_parameters(operation.get("parameters", []))
            if parameters:
                lines.append(f"Parameters:\n{parameters}")
            request_body = _format_request_body(operation)
            if request_body:
                lines.append(f"Request Body:\n{request_body}")
            responses = _format_responses(operation.get("responses", {}))
            if responses:
                lines.append(f"Responses:\n{responses}")

            content = "\n\n".join(lines)
            chunks.append(
                Chunk(
                    content=content,
                    file_path=str(file_path),
                    language="openapi",
                    chunk_type="api_endpoint",
                    name=f"{method.upper()} {path_name}",
                    start_line=1,
                    end_line=max(content.count("\n") + 1, 1),
                    last_modified=last_modified,
                )
            )

    schema_map = (spec.get("components", {}) or {}).get("schemas") or spec.get("definitions", {}) or {}
    if isinstance(schema_map, dict):
        for schema_name, schema in schema_map.items():
            if not isinstance(schema, dict):
                continue
            content = _format_schema_fields(schema_name, schema)
            chunks.append(
                Chunk(
                    content=content,
                    file_path=str(file_path),
                    language="openapi",
                    chunk_type="api_schema",
                    name=f"Schema: {schema_name}",
                    start_line=1,
                    end_line=max(content.count("\n") + 1, 1),
                    last_modified=last_modified,
                )
            )

    return chunks or chunk_fixed_size(file_path)
