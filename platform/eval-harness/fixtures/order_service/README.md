# Order Service

A small in-process order service used by the coding-agent E2E suite. It has no
third-party dependencies and can be tested offline.

## Architecture

The application follows a layered design:

1. `OrderAPI` translates HTTP-like requests into service calls and maps domain
   errors to status codes.
2. `OrderService` owns order validation and state transitions.
3. `OrderRepository` is the persistence boundary. The fixture uses
   `InMemoryOrderRepository`.
4. `EventPublisher` is the integration boundary. The fixture uses
   `InMemoryEventPublisher`.

`application.py` is the composition root that wires these layers together.

## API

| Method | Path | Result |
|---|---|---|
| `GET` | `/health` | Service health |
| `POST` | `/orders` | Create an order (`201`) |
| `GET` | `/orders/{id}` | Read an order (`200`) |
| `POST` | `/orders/{id}/confirm` | Confirm a pending order (`200`) |
| `POST` | `/orders/{id}/fulfill` | Fulfill a confirmed order (`200`) |

Unknown orders return `404`. Invalid state transitions return `409`. Invalid
request bodies return `400`.

## Business Flow

An order starts in `PENDING`, moves to `CONFIRMED`, and then to `FULFILLED`.
Each successful transition is persisted before its domain event is published.

## Configuration

`config/app.toml` controls the service name and maximum items per order.

## Tests

```bash
python -m unittest discover -s tests -v
```