"""Privacy-bounded operation tracing for Brain Hub."""

from __future__ import annotations

import atexit
import os
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


P = ParamSpec("P")
R = TypeVar("R")
SERVICE_NAME = "brainhub"
SERVICE_VERSION = "0.1.0"

_tracer = trace.get_tracer(__name__, SERVICE_VERSION)
_provider = None


def configure_telemetry() -> None:
    """Enable OTLP/HTTP export only when a standard OTLP endpoint is configured."""

    global _provider, _tracer
    if _provider is not None or not (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": SERVICE_NAME, "service.version": SERVICE_VERSION}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    _provider = provider
    _tracer = provider.get_tracer(__name__, SERVICE_VERSION)
    atexit.register(provider.shutdown)


def traced(operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Trace an operation without inspecting its arguments or result."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attributes = {
                "brainhub.operation": operation,
                "brainhub.service": SERVICE_NAME,
                "brainhub.version": SERVICE_VERSION,
            }
            with _tracer.start_as_current_span(
                f"brainhub.{operation}",
                attributes=attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    result = function(*args, **kwargs)
                except BaseException:
                    span.set_attribute("brainhub.success", False)
                    span.set_status(Status(StatusCode.ERROR))
                    raise
                span.set_attribute("brainhub.success", True)
                span.set_status(Status(StatusCode.OK))
                return result

        return wrapper

    return decorate
