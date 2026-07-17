"""Stable, privacy-preserving ingestion boundary for Brain Hub."""

from .model import CloudEvent, make_event
from .manifest import AdapterManifest, load_manifest
from .normalize import normalize_capture
from .spool import BoundedSpool, SpoolResult

__all__ = [
    "BoundedSpool",
    "CloudEvent",
    "AdapterManifest",
    "SpoolResult",
    "load_manifest",
    "make_event",
    "normalize_capture",
]

__version__ = "0.1.0"
