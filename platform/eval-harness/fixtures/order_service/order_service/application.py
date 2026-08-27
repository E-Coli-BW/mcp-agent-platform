from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .api import OrderAPI
from .events import InMemoryEventPublisher
from .repository import InMemoryOrderRepository
from .service import OrderService


@dataclass(frozen=True, slots=True)
class Application:
    api: OrderAPI
    service: OrderService
    repository: InMemoryOrderRepository
    publisher: InMemoryEventPublisher


def load_config(path: str | Path) -> dict[str, object]:
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)


def build_application(config_path: str | Path = "config/app.toml") -> Application:
    config = load_config(config_path)
    repository = InMemoryOrderRepository()
    publisher = InMemoryEventPublisher()
    service = OrderService(
        repository,
        publisher,
        max_items_per_order=int(config["max_items_per_order"]),
    )
    return Application(
        api=OrderAPI(service),
        service=service,
        repository=repository,
        publisher=publisher,
    )