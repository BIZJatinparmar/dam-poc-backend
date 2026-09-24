"""Contract checks for the Azure Content Understanding analyzer setup request."""

import json
import re

import httpx

from atlas_backend import configure_image_analyzer


def test_default_analyzer_id_is_accepted_by_content_understanding(monkeypatch):
    monkeypatch.setenv("ATLAS_CU_ENDPOINT", "https://example.services.ai.azure.com")
    monkeypatch.setenv("ATLAS_CU_KEY", "test-key")
    monkeypatch.delenv("ATLAS_CU_ANALYZER_ID", raising=False)

    def respond(request: httpx.Request) -> httpx.Response:
        analyzer_id = request.url.path.rsplit("/", 1)[-1]
        assert json.loads(request.content)["analyzerId"] == analyzer_id
        if not re.fullmatch(r"[A-Za-z0-9._]{1,64}", analyzer_id):
            return httpx.Response(400, json={"error": {
                "code": "InvalidRequest", "message": "Invalid Request.",
                "innererror": {"code": "InvalidFieldSchema", "details": [{
                    "code": "InvalidAnalyzerId", "message": "The 'analyzerId' cannot contain '-'",
                }]},
            }})
        return httpx.Response(201, json={"analyzerId": analyzer_id})

    original_client = httpx.Client
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(configure_image_analyzer.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))

    configure_image_analyzer.main()
