"""Verify every Spring Boot application packages an executable jar."""

import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = (
    "memory-server",
    "auth-service",
    "agent-server-java",
    "codeexec-server",
    "completion-server",
    "filesearch-server",
    "model-router",
    "review-agent",
)
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def text(element: ET.Element | None, path: str) -> str:
    if element is None:
        return ""
    value = element.find(path, NS)
    return value.text.strip() if value is not None and value.text else ""


missing = []
for module in MODULES:
    pom_path = ROOT / "platform" / module / "pom.xml"
    project = ET.parse(pom_path).getroot()
    parent_is_boot = text(project, "m:parent/m:artifactId") == "spring-boot-starter-parent"
    plugin = next(
        (
            candidate
            for candidate in project.findall("m:build/m:plugins/m:plugin", NS)
            if text(candidate, "m:artifactId") == "spring-boot-maven-plugin"
        ),
        None,
    )
    goals = {
        goal.text.strip()
        for goal in plugin.findall("m:executions/m:execution/m:goals/m:goal", NS)
        if goal.text
    } if plugin is not None else set()
    if plugin is None or (not parent_is_boot and "repackage" not in goals):
        missing.append(module)

if missing:
    joined = ", ".join(missing)
    raise SystemExit(f"Spring Boot package does not bind repackage: {joined}")

print(f"ok executable jar contracts ({len(MODULES)} modules)")