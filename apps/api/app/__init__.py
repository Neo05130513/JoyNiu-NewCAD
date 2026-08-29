"""Backend services used by the JoyNiu FastAPI application.

The domain services deliberately have no FastAPI or Pydantic dependency.  This
keeps PDM, authorisation, OCR evidence and CAM release rules usable from tests,
workers and command-line tools.  ``platform_api.create_platform_router`` is the
thin HTTP adapter.
"""

# Keep the API/package version in one place.  ``app.main`` exposes this value
# in both the health response and OpenAPI metadata, while packaging tools can
# read it without importing FastAPI or CadQuery.
__version__ = "0.2.0"

from .platform import (
    AuthService,
    AuthorizationError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PDMRepository,
    Permission,
    Role,
    ValidationError,
)
from .cam import CAMService, CAMReleaseGate
from .ocr import OCRService

__all__ = [
    "__version__",
    "AuthService",
    "AuthenticationError",
    "AuthorizationError",
    "ConflictError",
    "NotFoundError",
    "PDMRepository",
    "Permission",
    "Role",
    "ValidationError",
    "CAMReleaseGate",
    "CAMService",
    "OCRService",
]
