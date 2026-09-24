"""Create or update the Atlas image analyzer in an existing Foundry resource."""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


load_dotenv()


def check_azure_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        response.raise_for_status()
        return

    parts: list[str] = []

    def collect(item: dict) -> None:
        if code := item.get("code"):
            parts.append(str(code))
        if message := item.get("message"):
            parts.append(str(message))
        if isinstance(item.get("innererror"), dict):
            collect(item["innererror"])
        for detail in item.get("details", [])[:5]:
            if isinstance(detail, dict):
                collect(detail)

    collect(error)
    detail = ": ".join(parts)[:1200] or "No error details returned"
    key = os.getenv("ATLAS_CU_KEY", "")
    if key:
        detail = detail.replace(key, "<REDACTED>")
    raise RuntimeError(f"Azure Content Understanding returned HTTP {response.status_code}: {detail}")


def main() -> None:
    endpoint = os.environ["ATLAS_CU_ENDPOINT"].rstrip("/")
    key = os.environ["ATLAS_CU_KEY"]
    analyzer_id = os.getenv("ATLAS_CU_ANALYZER_ID", "atlas_image_tags")
    definition = json.loads((Path(__file__).with_name("image_analyzer.json")).read_text())
    definition["analyzerId"] = analyzer_id
    url = f"{endpoint}/contentunderstanding/analyzers/{analyzer_id}"
    headers = {"Ocp-Apim-Subscription-Key": key}
    with httpx.Client(timeout=60, trust_env=True) as client:
        response = client.put(url, params={"api-version": "2025-11-01"}, headers=headers, json=definition)
        check_azure_response(response)
        operation_url = response.headers.get("Operation-Location")
        if operation_url:
            if urlparse(operation_url).netloc != urlparse(endpoint).netloc:
                raise RuntimeError("Unexpected analyzer operation host")
            for _ in range(60):
                result = client.get(operation_url, headers=headers)
                check_azure_response(result)
                state = str(result.json().get("status", "")).lower()
                if state == "succeeded":
                    break
                if state in {"failed", "canceled"}:
                    raise RuntimeError("Image analyzer creation failed")
                time.sleep(2)
            else:
                raise TimeoutError("Image analyzer creation did not finish in two minutes")
    print(f"Image analyzer {analyzer_id} is ready")


if __name__ == "__main__":
    main()
