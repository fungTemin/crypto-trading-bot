from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Awaitable
from typing import Any

from src.core.constants import EventType
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

Handler = Callable[..., Awaitable[None]]


class EventBus:
    """Simple async pub/sub event bus.

    Events are typed by EventType. Handlers receive **kwargs payloads.
    All handlers are called concurrently via asyncio.gather on each publish.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)
        self._error_handlers: list[Handler] = []

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        """Register a handler for a specific event type."""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: Handler) -> None:
        """Remove a handler registration."""
        try:
            self._handlers[event_type].remove(handler)
        except ValueError:
            pass

    def on_error(self, handler: Handler) -> None:
        """Register a handler for errors during event processing."""
        self._error_handlers.append(handler)

    async def publish(self, event_type: EventType, **kwargs: Any) -> None:
        """Publish an event to all registered handlers concurrently.

        Each handler receives the event keyword arguments directly.
        Errors in handlers are logged and forwarded to error handlers.
        """
        handlers = self._handlers.get(event_type, [])
        if not handlers:
            return

        results = await asyncio.gather(
            *(self._invoke_handler(h, event_type, **kwargs) for h in handlers),
            return_exceptions=True,
        )

        for h, result in zip(handlers, results):
            if isinstance(result, Exception):
                logger.error(
                    "Event handler error",
                    extra={"event": event_type.value, "handler": h.__name__, "error": str(result)},
                )
                for err_handler in self._error_handlers:
                    try:
                        await err_handler(event=event_type, error=result)
                    except Exception:
                        pass

    async def _invoke_handler(self, handler: Handler, event_type: EventType, **kwargs: Any) -> None:
        try:
            await handler(**kwargs)
        except Exception as e:
            raise e
