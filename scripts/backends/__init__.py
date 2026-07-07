"""
backends — Pluggable device control backends for android-dev-qa.

Every backend implements the same DeviceBackend protocol, allowing the MCP
server to drive Android devices through ADB, Scrcpy, or any future
controller without changing tool logic.
"""
from .base import DeviceBackend, BackendInfo
from .adb_backend import AdbBackend

__all__ = ["DeviceBackend", "BackendInfo", "AdbBackend"]

BACKEND_REGISTRY: dict[str, type[DeviceBackend]] = {}


def register_backend(name: str, cls: type[DeviceBackend]) -> None:
    BACKEND_REGISTRY[name] = cls


def get_backend(name: str = "adb", **kwargs) -> DeviceBackend:
    """Instantiate a backend by name. Default is ADB."""
    # Auto-register built-in backends
    if not BACKEND_REGISTRY:
        register_backend("adb", AdbBackend)
    cls = BACKEND_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown backend '{name}'. Available: {list(BACKEND_REGISTRY)}")
    return cls(**kwargs)
