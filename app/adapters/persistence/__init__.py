"""SQL persistence adapter used by the application modules."""

from .database import Base, platform_session

__all__ = ["Base", "platform_session"]
