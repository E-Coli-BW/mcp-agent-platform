"""Start each packaged Spring Boot application and require a healthy response."""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
JWT_SECRET = "entrypoint-smoke-secret-at-least-32-bytes"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


@dataclass(frozen=True)
class Application:
    module: str
    health_path: str
    arguments: tuple[str, ...] = ()


APPLICATIONS = (
    Application(
        "memory-server",
        "/actuator/health",
        ("--mcp.security.jwt-secret=" + JWT_SECRET, "--spring.flyway.enabled=false"),
    ),
    Application("auth-service", "/auth/health"),
    Application(
        "agent-server-java",
        "/health",
        ("--management.health.redis.enabled=false",),
    ),
    Application(
        "codeexec-server",
        "/actuator/health",
        (
            "--mcp.security.jwt-secret=" + JWT_SECRET,
            "--spring.profiles.active=dev",
            "--codeexec.sandbox.mode=process",
        ),
    ),
    Application(
        "completion-server",
        "/actuator/health",
        ("--mcp.security.jwt-secret=" + JWT_SECRET, "--mcp.security.jwks-url="),
    ),
    Application(
        "filesearch-server",
        "/actuator/health",
        ("--mcp.security.jwt-secret=" + JWT_SECRET,),
    ),
    Application(
        "model-router",
        "/actuator/health",
        (
            "--mcp.security.jwt-secret=" + JWT_SECRET,
            "--model-router.providers.ollama.enabled=false",
            "--model-router.default-provider=none",
        ),
    ),
    Application("review-agent", "/health"),
)


def pom_value(project: ET.Element, path: str) -> str:
    element = project.find(path, NS)
    return element.text.strip() if element is not None and element.text else ""


def application_jar(module: str) -> Path:
    project = ET.parse(ROOT / "platform" / module / "pom.xml").getroot()
    artifact = pom_value(project, "m:artifactId")
    version = pom_value(project, "m:version") or pom_value(project, "m:parent/m:version")
    jar = ROOT / "platform" / module / "target" / f"{artifact}-{version}.jar"
    if not jar.is_file() or jar.stat().st_size == 0:
        raise RuntimeError(f"{module}: packaged jar is missing or empty: {jar.name}")
    with ZipFile(jar) as archive:
        manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8")
    if "Main-Class: org.springframework.boot.loader.launch.JarLauncher" not in manifest:
        raise RuntimeError(f"{module}: jar is not Spring Boot executable")
    if "Start-Class:" not in manifest:
        raise RuntimeError(f"{module}: executable jar has no Start-Class")
    return jar


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def log_tail(log_file, limit: int = 4_000) -> str:
    log_file.flush()
    size = log_file.tell()
    log_file.seek(max(0, size - limit))
    return log_file.read().decode("utf-8", errors="replace")


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def await_health(application: Application, process: subprocess.Popen[bytes], port: int, log_file) -> None:
    url = f"http://127.0.0.1:{port}{application.health_path}"
    deadline = time.monotonic() + 45
    last_error = "no response"
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"{application.module}: exited before health ({return_code})\n{log_tail(log_file)}"
            )
        try:
            with urlopen(url, timeout=1) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
                if response.status == 200 and str(payload.get("status", "")).lower() in {"up", "ok"}:
                    return
                last_error = f"HTTP {response.status}: {body[:500]}"
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(
        f"{application.module}: health timed out ({last_error})\n{log_tail(log_file)}"
    )


with tempfile.TemporaryDirectory(prefix="java-entrypoint-smoke-") as temporary_root:
    for application in APPLICATIONS:
        jar = application_jar(application.module)
        port = free_port()
        arguments = [
            "java",
            "-jar",
            str(jar),
            f"--server.port={port}",
            "--logging.level.root=WARN",
            *application.arguments,
        ]
        if application.module == "memory-server":
            arguments.append(f"--memorystore.file.base-dir={temporary_root}/memory-store")
        if application.module == "filesearch-server":
            arguments.append(f"--filesearch.default-root={temporary_root}")
        with tempfile.TemporaryFile() as log_file:
            process = subprocess.Popen(
                arguments,
                cwd=ROOT / "platform" / application.module,
                env={**os.environ, "JWT_SECRET": JWT_SECRET},
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                await_health(application, process, port, log_file)
                print(f"ok {application.module} {application.health_path}")
            finally:
                stop(process)