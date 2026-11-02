import pytest

from provcat import Catalog
from provcat import hooks


@pytest.fixture(autouse=True)
def no_hooks(monkeypatch, tmp_path_factory):
    """No test reads the hooks file or the app of the machine it runs on."""
    monkeypatch.delenv("PROVCAT_APP", raising=False)
    monkeypatch.delenv("PROVCAT_HOOKS", raising=False)
    monkeypatch.setattr(hooks, "DEFAULT_FILE", tmp_path_factory.mktemp("hooks") / "none.yaml")
    hooks._forget_process_app()
    yield
    hooks._forget_process_app()


@pytest.fixture
def catalog(tmp_path):
    return Catalog(cache_root=tmp_path / "cache")


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "runs"


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "report.txt").write_text("Revenue in 2025 was 42 million.\nCosts were 30 million.\n")
    (d / "notes").mkdir()
    (d / "notes" / "a.md").write_text("# A\nalpha\n")
    return d


@pytest.fixture
def spans(monkeypatch):
    """provrun's tracer, recording to memory. Returns the exporter."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from provrun import otel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel, "_tracer", provider.get_tracer("provrun"))
    return exporter
