"""Coach Web — FastAPI frontend for the Coach MCP server.

The package declares no version constant (#198): the reported version has a
single source of truth — :func:`coach_web.app.resolve_version` (build-time
env override, then installed distribution metadata, then the honest ``0.0.0``
fallback).
"""
