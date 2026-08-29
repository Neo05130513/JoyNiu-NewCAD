"""Backward-compatible import alias for the platform FastAPI adapter."""

from .platform_api import (
    PlatformServices,
    build_platform_services,
    create_platform_router,
    default_services,
    router,
)

__all__ = [
    "PlatformServices",
    "build_platform_services",
    "create_platform_router",
    "default_services",
    "router",
]
