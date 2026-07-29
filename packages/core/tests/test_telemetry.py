from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from brainhub import telemetry


def test_service_spans_are_bounded_and_hide_inputs(service, monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))

    secret_query = "secret prompt and credential"
    service.search(secret_query, global_scope=True)
    with pytest.raises(ValueError):
        service.search(secret_query)

    success, failure = exporter.get_finished_spans()
    expected_keys = {
        "brainhub.operation",
        "brainhub.service",
        "brainhub.version",
        "brainhub.success",
    }
    assert set(success.attributes) == expected_keys
    assert success.attributes["brainhub.operation"] == "search"
    assert success.attributes["brainhub.success"] is True
    assert success.status.status_code is StatusCode.OK
    assert failure.attributes["brainhub.success"] is False
    assert failure.status.status_code is StatusCode.ERROR
    assert failure.events == ()
    assert secret_query not in repr((success, failure))
