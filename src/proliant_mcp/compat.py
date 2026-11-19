"""Accept the 2025 ``initialize`` handshake after a client probed with a 2026-07-28 request.

The MCP library picks a connection's protocol era from the client's first request and then
refuses the other era. Some clients (Copilot in VS Code, Nov 2026) open with a 2026-07-28
``server/discover`` probe and then fall back to ``initialize`` on the same stdio connection,
which the library answers with -32022 "connection is serving the 2026-07-28 protocol", so the
server never starts for them. This wraps the library's stdio loop: a connection that opened in
the 2026-07-28 era switches to the 2025 handshake era when ``initialize`` arrives.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio

logger = logging.getLogger(__name__)


class _KeepOpen:
    """Write stream proxy whose close is a no-op, so the first era's loop cannot close the shared output."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def send(self, item: Any) -> None:
        await self._inner.send(item)

    async def aclose(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def __aenter__(self) -> _KeepOpen:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install() -> bool:
    """Patch the MCP library's dual-era stdio loop; returns False if its internals changed."""
    try:
        from mcp.server import runner
        from mcp.server.lowlevel import server as lowlevel
        from mcp.shared._context_streams import ContextReceiveStream
        from mcp.shared.message import SessionMessage
        from mcp_types import JSONRPCRequest

        has_modern = runner._has_modern_envelope
        replay = runner._replay_from_opening_request
        sender_context = runner._sender_context
        serve_legacy, serve_modern = runner._serve_legacy_stream, runner._serve_modern_stream
        original = lowlevel.serve_dual_era_loop
    except (ImportError, AttributeError):
        logger.debug("MCP library internals changed; initialize-after-discover fallback not installed")
        return False
    if getattr(original, "_proliant_mcp", False):
        return True

    def is_initialize(item: Any) -> bool:
        return isinstance(item, SessionMessage) and isinstance(item.message, JSONRPCRequest) and item.message.method == "initialize"

    async def serve_dual_era_loop(server: Any, read_stream: Any, write_stream: Any, *, lifespan_state: Any,
                                  session_id: str | None = None, init_options: Any = None, raise_exceptions: bool = False) -> None:
        try:
            async with replay(read_stream) as (opening, replayed):
                opens_modern = opening is not None and opening.method != "initialize" and has_modern(opening.params)
                if not opens_modern:
                    await serve_legacy(server, replayed, write_stream, lifespan_state=lifespan_state, session_id=session_id,
                                       init_options=init_options, raise_exceptions=raise_exceptions)
                    return
                out = _KeepOpen(write_stream)
                modern_send, modern_recv = anyio.create_memory_object_stream[Any](100)
                legacy_send, legacy_recv = anyio.create_memory_object_stream[Any](100)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(lambda: serve_modern(server, ContextReceiveStream(modern_recv), out,
                                                       lifespan_state=lifespan_state, raise_exceptions=raise_exceptions))
                    target = modern_send
                    try:
                        async for item in replayed:
                            if target is modern_send and is_initialize(item):
                                logger.info("client sent initialize after a 2026-07-28 request; switching to the handshake era")
                                modern_send.close()
                                target = legacy_send
                                tg.start_soon(lambda: serve_legacy(server, ContextReceiveStream(legacy_recv), out,
                                                                   lifespan_state=lifespan_state, session_id=session_id,
                                                                   init_options=init_options, raise_exceptions=raise_exceptions))
                            await target.send((sender_context(replayed), item))
                    finally:
                        modern_send.close()
                        legacy_send.close()
                        if target is modern_send:
                            legacy_recv.close()
        finally:
            await write_stream.aclose()

    serve_dual_era_loop._proliant_mcp = True  # type: ignore[attr-defined]
    lowlevel.serve_dual_era_loop = serve_dual_era_loop
    return True
