from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .service import InvalidOrder, InvalidTransition, OrderNotFound, OrderService


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: dict[str, Any]


class OrderAPI:
    def __init__(self, service: OrderService) -> None:
        self._service = service

    def handle(self, method: str, path: str, body: dict[str, Any] | None = None) -> Response:
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            if method == "POST" and path == "/orders":
                payload = body or {}
                order = self._service.create_order(
                    str(payload.get("customer_id", "")),
                    list(payload.get("items") or []),
                )
                return Response(201, order.to_dict())
            order_id, action = self._parse_order_path(path)
            if method == "GET" and action is None:
                return Response(200, self._service.get_order(order_id).to_dict())
            if method == "POST" and action == "confirm":
                return Response(200, self._service.confirm_order(order_id).to_dict())
            if method == "POST" and action == "fulfill":
                return Response(200, self._service.fulfill_order(order_id).to_dict())
            if method == "POST" and action == "cancel":
                return Response(200, self._service.cancel_order(order_id).to_dict())
            return Response(404, {"error": "route not found"})
        except OrderNotFound:
            return Response(404, {"error": "order not found"})
        except InvalidOrder as error:
            return Response(400, {"error": str(error)})
        except InvalidTransition as error:
            return Response(409, {"error": str(error)})

    @staticmethod
    def _parse_order_path(path: str) -> tuple[str, str | None]:
        parts = [part for part in path.split("/") if part]
        if len(parts) not in {2, 3} or parts[0] != "orders":
            return "", "invalid"
        return parts[1], parts[2] if len(parts) == 3 else None