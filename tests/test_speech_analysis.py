import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from atlas_backend import analysis
from atlas_backend.analysis import AnalysisFailure, AzureAnalysisClient, grounded_tag, speech_segments


JOB = "https://speech.test/speechtotext/transcriptions/job-1?api-version=2025-10-15"
JOB_FILES = "https://speech.test/speechtotext/transcriptions/job-1/files?api-version=2025-10-15"
JOB_REGIONAL = "https://eastus.api.cognitive.microsoft.com/speechtotext/transcriptions/job-1?api-version=2025-10-15"


def test_speech_batch_submit_and_poll(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["locale"] == "en-US"
            assert body["contentUrls"] == ["https://storage.blob.core.windows.net/audio/NS-1.flac"]
            return httpx.Response(201, headers={"Location": JOB_REGIONAL})
        if request.url.path.endswith("/job-1"):
            return httpx.Response(200, json={"status": "Succeeded", "links": {"files": JOB_FILES}})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"values": [{"kind": "Transcription", "links": {
                "contentUrl": "https://results.blob.core.windows.net/output/job.json?sig=test"}}]})
        return httpx.Response(200, json={"recognizedPhrases": [{"recognitionStatus": "Success",
            "offsetInTicks": 12_340_000, "durationInTicks": 20_000_000,
            "nBest": [{"display": "Hello there"}]}]})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.speech_endpoint = "https://speech.test"
    azure.speech_key = "test"
    azure.speech_locale = "en-US"
    azure.audio_storage = lambda: type("Audio", (), {"blob": lambda self, name: type("Blob", (), {
        "url": f"https://storage.blob.core.windows.net/audio/{name}"})()})()
    assert azure.submit_video(type("Asset", (), {"id": "NS-1"})(), "NS-1.flac") == JOB
    status, payload = azure.poll_video(JOB)
    assert status == "complete"
    assert speech_segments(payload) == [{"id": 1, "start": "0:00:01.234",
                                         "end": "0:00:03.234", "text": "Hello there"}]
    assert len(requests) == 4


def test_speech_submit_accepts_relative_location_with_absolute_self(monkeypatch):
    def handler(request):
        return httpx.Response(201, headers={"Location": "/speechtotext/transcriptions/job-1?api-version=2025-10-15"},
                              json={"self": JOB_REGIONAL})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.speech_endpoint = "https://eastus.api.cognitive.microsoft.com"
    azure.speech_key = "test"
    azure.audio_storage = lambda: type("Audio", (), {"blob": lambda self, name: type("Blob", (), {
        "url": f"https://storage.blob.core.windows.net/audio/{name}"})()})()

    assert azure.submit_video(type("Asset", (), {"id": "NS-1"})(), "NS-1.flac") == JOB_REGIONAL


def test_speech_submit_uses_valid_self_when_location_is_invalid(monkeypatch, caplog):
    def handler(request):
        return httpx.Response(201, headers={"Location": "https://evil.test/speechtotext/transcriptions/job-1"},
                              json={"self": JOB_REGIONAL})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.speech_endpoint = "https://eastus.api.cognitive.microsoft.com"
    azure.speech_key = "test"
    azure.audio_storage = lambda: type("Audio", (), {"blob": lambda self, name: type("Blob", (), {
        "url": f"https://storage.blob.core.windows.net/audio/{name}"})()})()

    with caplog.at_level("WARNING", logger="atlas_backend.analysis"):
        assert azure.submit_video(type("Asset", (), {"id": "NS-1"})(), "NS-1.flac") == JOB_REGIONAL
    assert "host_allowed=False" in caplog.text
    assert "evil.test" not in caplog.text


def test_speech_submit_accepts_resource_domain_job_url(monkeypatch):
    resource_job = "https://speech-resource.cognitiveservices.azure.com/speechtotext/transcriptions/job-1?api-version=2025-10-15"

    def handler(request):
        return httpx.Response(201, headers={"Location": resource_job})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.speech_endpoint = "https://eastus.api.cognitive.microsoft.com"
    azure.speech_key = "test"
    azure.audio_storage = lambda: type("Audio", (), {"blob": lambda self, name: type("Blob", (), {
        "url": f"https://storage.blob.core.windows.net/audio/{name}"})()})()

    assert azure.submit_video(type("Asset", (), {"id": "NS-1"})(), "NS-1.flac") == JOB_REGIONAL


def test_speech_submit_logs_redacted_azure_error(monkeypatch, caplog):
    secret = "abcdefghijklmnopqrstuvwxyz1234567890"
    audio_url = "https://storage.blob.core.windows.net/audio/private.flac?sig=private-signature"

    def handler(request):
        return httpx.Response(400, headers={"apim-request-id": "request-123"}, json={
            "code": "InvalidRequest",
            "message": f"Subscription key '{secret}' is invalid for {audio_url}",
            "innerError": {"code": "InvalidSubscription", "message":
                           'Only "Standard" subscriptions for the region of the called service are valid.'},
        })

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.speech_endpoint = "https://eastus.api.cognitive.microsoft.com"
    azure.speech_key = secret
    azure.audio_storage = lambda: type("Audio", (), {"blob": lambda self, name: type("Blob", (), {
        "url": audio_url})()})()

    with caplog.at_level("WARNING", logger="atlas_backend.analysis"):
        with pytest.raises(httpx.HTTPStatusError):
            azure.submit_video(type("Asset", (), {"id": "NS-1"})(), "private.flac")

    log = caplog.text
    assert "InvalidRequest" in log and "InvalidSubscription" in log
    assert "request-123" in log and "eastus.api.cognitive.microsoft.com" in log
    assert "Subscription key" in log
    assert 'Only "Standard" subscriptions' in log
    assert secret not in log
    assert audio_url not in log
    assert "private-signature" not in log


def test_speech_phrases_are_sorted_and_skip_failed_recognition():
    phrases = [{"recognitionStatus": "Success", "offsetInTicks": 20_000_000,
                "durationInTicks": 5_000_000, "nBest": [{"display": "Second"}]},
               {"recognitionStatus": "NoMatch", "offsetInTicks": 0, "nBest": []},
               {"recognitionStatus": "Success", "offsetInTicks": 1_000_000,
                "durationInTicks": 10_000_000, "nBest": [{"display": "First"}]}]
    assert [item["text"] for item in speech_segments({"recognizedPhrases": phrases})] == ["First", "Second"]


def test_fallback_rejects_remote_job_urls():
    azure = AzureAnalysisClient()
    azure.speech_endpoint = "https://speech.test"
    with pytest.raises(AnalysisFailure, match="invalid job URL"):
        azure.poll_video("https://evil.test/speechtotext/transcriptions/job")


def test_tag_grounding_requires_every_word_in_contiguous_spoken_evidence():
    source = "The beach was crowded, and AI helped plan the trip."
    assert grounded_tag({"name": "Crowded beach", "evidence": "beach was crowded"}, source)
    assert grounded_tag({"name": "AI", "evidence": "AI helped plan"}, source)
    assert not grounded_tag({"name": "Beach sunset", "evidence": "The beach was crowded"}, source)
    assert not grounded_tag({"name": "Beach", "evidence": "beach was empty"}, source)


def test_foundry_tags_accept_speech_quote_with_punctuation_difference(monkeypatch):
    def handler(_request):
        return httpx.Response(200, json={"output_text": json.dumps({"tags": [
            {"name": "Blue ocean", "evidence": "blue ocean"},
            {"name": "Sunset", "evidence": "blue ocean"},
        ]})})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.foundry_endpoint = "https://foundry.test"
    azure.foundry_key = "test"
    assert azure.tag_transcript([{"text": "A bright blue, ocean appears."}]) == ["Blue ocean"]


def test_foundry_empty_grounded_tags_are_reported_as_failure(monkeypatch):
    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"output_text": json.dumps({"tags": []})})), **kwargs))
    azure = AzureAnalysisClient()
    azure.foundry_endpoint = "https://foundry.test"
    azure.foundry_key = "test"
    with pytest.raises(AnalysisFailure, match="no grounded transcript tags"):
        azure.tag_transcript([{"text": "A bright blue ocean"}])


def test_foundry_tags_require_exact_evidence_and_consolidate(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        assert body["reasoning"] == {"effort": "none"}
        source = body["input"][1]["content"]
        seen.append(source)
        if " | " in source:
            tags = [{"name": "topic00", "evidence": "topic00"}]
        else:
            tags = [{"name": word, "evidence": word} for word in re.findall(r"topic\d\d", source)]
            tags += [{"name": "City skyline", "evidence": "topic00"},
                     {"name": "Unsupported", "evidence": "no such quote"}]
        return httpx.Response(200, json={"output": [{"content": [
            {"type": "output_text", "text": json.dumps({"tags": tags})}]}]})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(analysis, "FOUNDRY_CHUNK_CHARS", 50)
    azure = AzureAnalysisClient()
    azure.foundry_endpoint = "https://foundry.test"
    azure.foundry_key = "test"
    result = azure.tag_transcript([{"text": " ".join(f"topic{i:02} evidence{i:02}" for i in range(25))}])
    assert result == ["topic00"]
    assert len(seen) > 2


def test_foundry_tagging_accepts_v1_base_url(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path != "/openai/v1/responses":
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        return httpx.Response(200, json={"output_text": json.dumps({"tags": [
            {"name": "Azure", "evidence": "Azure"}]})})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.foundry_endpoint = "https://foundry.test/openai/v1"
    azure.foundry_key = "test"
    azure.foundry_deployment = "gpt-6-luna"

    assert azure.tag_transcript([{"text": "Azure ML overview"}]) == ["Azure"]
    assert requests[0].url.path == "/openai/v1/responses"
    assert json.loads(requests[0].content)["model"] == "gpt-6-luna"


def test_video_classification_uses_controlled_labels_and_transcript_segment(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["text"]["format"]["name"] == "video_classification"
        assert "only the spoken transcript" in body["input"][0]["content"]
        return httpx.Response(200, json={"output_text": json.dumps({
            "category": "training", "format": "tutorial", "evidence_segment_id": 7,
        })})

    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    azure = AzureAnalysisClient()
    azure.foundry_endpoint = "https://foundry.test"
    azure.foundry_key = "test"
    assert azure.classify_transcript([{"id": 7, "text": "First, connect the device to power."}]) == (
        "training", "tutorial", "First, connect the device to power.")
    assert azure.classify_transcript([]) is None


def test_video_classification_rejects_unknown_transcript_segment(monkeypatch):
    original = analysis.httpx.Client
    monkeypatch.setattr(analysis.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
            "output_text": json.dumps({"category": "event", "format": "presentation",
                                       "evidence_segment_id": 99})})), **kwargs))
    azure = AzureAnalysisClient()
    azure.foundry_endpoint = "https://foundry.test"
    azure.foundry_key = "test"
    with pytest.raises(AnalysisFailure, match="ungrounded video classification"):
        azure.classify_transcript([{"id": 7, "text": "Welcome to our company briefing."}])


def test_video_without_audio_skips_encoding(tmp_path: Path, monkeypatch):
    def fake_run(command, **kwargs):
        assert command[0] == "ffprobe"
        return analysis.subprocess.CompletedProcess(command, 0,
            stdout='{"streams":[{"codec_type":"video"}]}', stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)
    assert analysis.extract_audio(tmp_path / "clip.mp4", tmp_path / "clip.flac") is False


def test_delayed_audio_remains_aligned_with_video(tmp_path: Path):
    source, output = tmp_path / "delayed.mp4", tmp_path / "audio.flac"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=16x16:d=4", "-itsoffset", "1.25", "-f", "lavfi", "-i",
                    "sine=frequency=1000:duration=2", "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
                   check=True, capture_output=True, timeout=30)
    assert analysis.extract_audio(source, output)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(output)], check=True, capture_output=True,
                           text=True, timeout=30)
    assert float(json.loads(probe.stdout)["format"]["duration"]) >= 3.2
