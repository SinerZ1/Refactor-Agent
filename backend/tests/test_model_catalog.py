import asyncio
from pathlib import Path
from types import SimpleNamespace

import model_catalog
from model_catalog import ModelConnectionConfig


def test_openai_catalog_normalizes_standard_model_response(monkeypatch):
    captured = {}

    async def fake_request(url, *, headers=None, params=None):
        captured.update(url=url, headers=headers, params=params)
        return {
            "data": [
                {"id": "deepseek-v4-pro"},
                {"id": "deepseek-v4-flash"},
                {"invalid": "ignored"},
            ]
        }

    monkeypatch.setattr(model_catalog, "_request_json", fake_request)
    models = asyncio.run(
        model_catalog.list_available_models(
            ModelConnectionConfig(
                provider="openai",
                api_key="secret",
                base_url="https://api.deepseek.com/",
            )
        )
    )

    assert captured["url"] == "https://api.deepseek.com/models"
    assert captured["headers"] == {"Authorization": "Bearer secret"}
    assert models == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_gemini_catalog_keeps_generate_content_models(monkeypatch):
    async def fake_request(url, *, headers=None, params=None):
        assert url == "https://generativelanguage.googleapis.com/v1beta/models"
        assert params == {"key": "secret", "pageSize": 1000}
        return {
            "models": [
                {
                    "name": "models/gemini-3.5-flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-embedding-001",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }

    monkeypatch.setattr(model_catalog, "_request_json", fake_request)
    models = asyncio.run(
        model_catalog.list_available_models(
            ModelConnectionConfig(provider="gemini_studio", api_key="secret")
        )
    )

    assert models == ["gemini-3.5-flash"]


def test_adc_status_never_exposes_credential_path(monkeypatch, tmp_path: Path):
    adc_path = tmp_path / "application_default_credentials.json"
    adc_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        model_catalog, "_adc_candidates", lambda: [("gcloud", adc_path)]
    )
    monkeypatch.setattr(
        model_catalog,
        "load_credentials_from_file",
        lambda *_args, **_kwargs: (SimpleNamespace(), "demo-project"),
    )

    status = model_catalog.inspect_adc_file()
    public_status = status.public_dict()

    assert status.available is True
    assert public_status["project_id"] == "demo-project"
    assert "path" not in public_status
    assert str(adc_path) not in str(public_status)


def test_vertex_api_key_uses_express_mode_without_project_or_location(monkeypatch):
    captured = {}

    class FakeModels:
        @staticmethod
        def list(*, config):
            assert config == {"page_size": 1000, "query_base": True}
            return [SimpleNamespace(name="publishers/google/models/gemini-3.5-flash")]

    class FakeClient:
        models = FakeModels()

        def close(self):
            captured["closed"] = True

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(model_catalog.genai, "Client", fake_client)
    models = model_catalog._list_vertex_models_sync(
        ModelConnectionConfig(
            provider="google_vertex",
            auth_mode="api_key",
            api_key="secret",
            project_id="must-not-be-sent",
            location="global",
        )
    )

    assert models == ["gemini-3.5-flash"]
    assert captured == {"vertexai": True, "api_key": "secret", "closed": True}
