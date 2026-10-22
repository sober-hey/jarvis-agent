"""Offline HTTP-boundary tests; never use production credentials/network."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import wave
from pathlib import Path


def load_adapter():
    path = Path(__file__).parent.parent / "voice_dashscope.py"
    assert path.is_file(), "DashScope adapter is not implemented"
    spec = importlib.util.spec_from_file_location("voice_dashscope", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_asr_pcm_uses_wav_and_returns_emotion_language_usage(tmp_path, monkeypatch):
    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture-not-a-live-key\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    calls = []

    def send(request, timeout):
        calls.append(request)
        assert request.full_url == adapter.ASR_ENDPOINT
        assert request.get_header("Authorization") == "Bearer fixture-not-a-live-key"
        body = json.loads(request.data)
        assert body["model"] == "qwen3-asr-flash"
        audio = body["input"]["messages"][0]["content"][0]["audio"]
        with wave.open(io.BytesIO(base64.b64decode(audio.split(",", 1)[1]))) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
            assert wav.readframes(wav.getnframes()) == b"\x00\x00" * 1600
        return Response(
            json.dumps(
                {
                    "output": {
                        "choices": [
                            {
                                "message": {
                                    "content": [{"text": "你好。"}],
                                    "annotations": [
                                        {
                                            "type": "audio_info",
                                            "emotion": "neutral",
                                            "language": "zh",
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                    "usage": {"seconds": 1, "output_tokens": 3},
                }
            ).encode()
        )

    monkeypatch.setattr(adapter, "_open", send)
    assert adapter.asr_transcribe(b"\x00\x00" * 1600) == {
        "text": "你好。",
        "emotion": "neutral",
        "language": "zh",
        "usage": {"seconds": 1, "output_tokens": 3},
    }
    assert len(calls) == 1


def test_tts_downloads_audio_without_forwarding_authorization(tmp_path, monkeypatch):
    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture-not-a-live-key\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    monkeypatch.setattr(adapter, "VOICE_CONFIG_PATH", tmp_path / "absent-voice_config.json")
    calls = []

    def send(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            body = json.loads(request.data)
            assert body == {
                "model": "cosyvoice-v3.5-flash",
                "input": {
                    "text": "你好。",
                    "voice": adapter.VOICE_ID,
                    "format": "mp3",
                    "sample_rate": 24000,
                },
            }
            return Response(
                json.dumps(
                    {
                        "output": {
                            "audio": {
                                "url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/sample.mp3?Expires=1"
                            }
                        },
                        "usage": {"characters": 6},
                    }
                ).encode()
            )
        assert request.get_header("Authorization") is None
        assert request.full_url.startswith("https://")
        return Response(b"ID3fixture")

    monkeypatch.setattr(adapter, "_open", send)
    assert adapter.tts_synthesize("你好。") == {
        "audio_bytes": b"ID3fixture",
        "format": "mp3",
        "usage": {"characters": 6},
    }
    assert len(calls) == 2


def test_http_failures_are_explicit_and_sanitized(tmp_path, monkeypatch, caplog):
    import logging
    from urllib.error import HTTPError

    import pytest

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture-not-a-live-key\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    for status, provider_code, expected in [
        (401, "InvalidApiKey", "invalid_key"),
        (403, "AccessDenied", "permission_denied"),
        (400, "QuotaExhausted", "quota_exhausted"),
        (429, "Throttling", "rate_limited"),
        (402, "Arrearage", "quota_exhausted"),
        (500, "InternalError", "provider_error"),
    ]:
        calls = []

        def send(request, timeout, calls=calls, provider_code=provider_code, status=status):
            calls.append(request)
            body = json.dumps(
                {"code": provider_code, "message": "fixture-not-a-live-key " + "PRIVATE-TEXT"}
            ).encode()
            raise HTTPError(request.full_url, status, "sensitive-reason", {}, io.BytesIO(body))

        monkeypatch.setattr(adapter, "_open", send)
        with caplog.at_level(logging.INFO):
            with pytest.raises(adapter.DashScopeError) as error:
                adapter.tts_synthesize("PRIVATE-TEXT")
        assert error.value.code == expected
        assert "fixture-not-a-live-key" not in str(error.value)
        assert "PRIVATE-TEXT" not in caplog.text
        assert "fixture-not-a-live-key" not in caplog.text
        assert "elapsed_seconds=" in caplog.text
        assert "characters=" in caplog.text
        assert len(calls) == 1  # No automatic duplicate paid requests.


def test_timeout_network_and_payload_failures(tmp_path, monkeypatch):
    from urllib.error import URLError

    import pytest

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture-not-a-live-key\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    for failure, expected in [
        (TimeoutError(), "timeout"),
        (URLError(TimeoutError()), "timeout"),
        (URLError("private-network-url"), "network_error"),
    ]:

        def send(request, timeout, failure=failure):
            raise failure

        monkeypatch.setattr(adapter, "_open", send)
        with pytest.raises(adapter.DashScopeError) as error:
            adapter.asr_transcribe(b"\0\0" * 10)
        assert error.value.code == expected
    for body in [
        b"not json",
        b"[]",
        b"{}",
        b'{"output":{"choices":[]}}',
        b'{"output":{"choices":[{"message":{"content":[{}]}}]}}',
    ]:
        monkeypatch.setattr(adapter, "_open", lambda *args, body=body: Response(body))
        with pytest.raises(adapter.DashScopeError) as error:
            adapter.asr_transcribe(b"\0\0" * 10)
        assert error.value.code == "invalid_response"


def test_validates_input_timeout_and_key_before_network(tmp_path, monkeypatch):
    import pytest

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    monkeypatch.setattr(adapter, "KEY_PATH", key)

    def send(*args):
        pytest.fail("invalid inputs must not cause network requests")

    monkeypatch.setattr(adapter, "_open", send)
    for pcm in [b"", b"1", b"RIFF" + b"\0" * 40, b"\0" * (180 * 32000 + 2)]:
        with pytest.raises(ValueError):
            adapter.asr_transcribe(pcm)
    for timeout in [0, -1, float("inf"), float("nan")]:
        with pytest.raises(ValueError):
            adapter.asr_transcribe(b"\0\0", timeout=timeout)
        with pytest.raises(ValueError):
            adapter.tts_synthesize("test", timeout=timeout)
    for text in ["", "  ", "a" * 2001]:
        with pytest.raises(ValueError):
            adapter.tts_synthesize(text)
    for value, mode in [("fixture", 0o644), ("", 0o600), ("a\nb", 0o600)]:
        key.write_text(value)
        key.chmod(mode)
        with pytest.raises(adapter.DashScopeError) as error:
            adapter.tts_synthesize("test")
        assert error.value.code == "key_file"
    key.unlink()
    target = tmp_path / "target"
    target.write_text("fixture")
    target.chmod(0o600)
    key.symlink_to(target)
    with pytest.raises(adapter.DashScopeError) as error:
        adapter.tts_synthesize("test")
    assert error.value.code == "key_file"


def test_cost_logs_contain_provider_usage_not_text(tmp_path, monkeypatch, caplog):
    import logging

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)

    def send(request, timeout):
        if request.get_method() == "POST":
            return Response(
                json.dumps(
                    {
                        "output": {
                            "audio": {
                                "url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test.mp3"
                            }
                        },
                        "usage": {"characters": 16},
                    }
                ).encode()
            )
        return Response(b"ID3fixture")

    monkeypatch.setattr(adapter, "_open", send)
    with caplog.at_level(logging.INFO):
        adapter.tts_synthesize("PRIVATE-SENTENCE")
    assert '"characters":16' in caplog.text
    assert "status=ok" in caplog.text
    assert "PRIVATE-SENTENCE" not in caplog.text


def test_real_local_http_transport_rejects_redirects(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture-not-a-live-key\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/credential-leak-target")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(adapter, "TTS_ENDPOINT", f"http://127.0.0.1:{server.server_port}/tts")
    try:
        with pytest.raises(adapter.DashScopeError):
            adapter.tts_synthesize("fixture")
        assert received == [("/tts", "Bearer fixture-not-a-live-key")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_untrusted_audio_urls_are_rejected_before_get(tmp_path, monkeypatch):
    import pytest

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    for url in [
        None,
        "",
        "file:///etc/passwd",
        "https://127.0.0.1/a",
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com.evil.invalid/a",
        "https://user@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a",
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com:8443/a",
    ]:
        calls = []

        def send(request, timeout, url=url, calls=calls):
            calls.append(request)
            return Response(json.dumps({"output": {"audio": {"url": url}}}).encode())

        monkeypatch.setattr(adapter, "_open", send)
        with pytest.raises(adapter.DashScopeError) as error:
            adapter.tts_synthesize("test")
        assert error.value.code == "invalid_response"
        assert len(calls) == 1


def test_provider_error_in_200_response_and_download_failure_are_not_silent(
    tmp_path, monkeypatch, caplog
):
    import logging

    import pytest

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    monkeypatch.setattr(
        adapter,
        "_open",
        lambda *args: Response(b'{"code":"QuotaExhausted","message":"private-provider-response"}'),
    )
    with pytest.raises(adapter.DashScopeError) as error:
        adapter.tts_synthesize("test")
    assert error.value.code == "quota_exhausted"

    def send(request, timeout):
        if request.get_method() == "POST":
            return Response(
                json.dumps(
                    {
                        "output": {
                            "audio": {
                                "url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a"
                            }
                        },
                        "usage": {
                            "characters": 99,
                            "seconds": "PRIVATE-SENTENCE",
                            "arbitrary": "fixture-secret",
                        },
                    }
                ).encode()
            )
        raise TimeoutError("private-url")

    monkeypatch.setattr(adapter, "_open", send)
    with caplog.at_level(logging.INFO):
        with pytest.raises(adapter.DashScopeError) as error:
            adapter.tts_synthesize("PRIVATE-SENTENCE")
    assert error.value.code == "timeout"
    assert '"characters":99' in caplog.text
    assert "status=error" in caplog.text
    assert "PRIVATE-SENTENCE" not in caplog.text
    assert "fixture-secret" not in caplog.text
    assert "private-url" not in str(error.value)


def test_response_byte_limit_and_empty_audio_fail_explicitly(tmp_path, monkeypatch):
    import pytest

    adapter = load_adapter()
    request = __import__("urllib.request", fromlist=["Request"]).Request("https://example.invalid")
    monkeypatch.setattr(adapter, "_open", lambda *args: Response(b"12345"))
    with pytest.raises(adapter.DashScopeError) as error:
        adapter._fetch(request, 2, 4)
    assert error.value.code == "invalid_response"

    key = tmp_path / "dashscope.key"
    key.write_text("fixture\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)

    def send(request, timeout):
        if request.get_method() == "POST":
            return Response(
                json.dumps(
                    {
                        "output": {
                            "audio": {
                                "url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a"
                            }
                        }
                    }
                ).encode()
            )
        return Response(b"")

    monkeypatch.setattr(adapter, "_open", send)
    with pytest.raises(adapter.DashScopeError) as error:
        adapter.tts_synthesize("test")
    assert error.value.code == "invalid_response"


def test_missing_annotations_are_unknown_not_fabricated_neutral(tmp_path, monkeypatch):
    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    monkeypatch.setattr(
        adapter,
        "_open",
        lambda *args: Response(
            json.dumps({"output": {"choices": [{"message": {"content": [{"text": ""}]}}]}}).encode()
        ),
    )
    assert adapter.asr_transcribe(b"\0\0") == {
        "text": "",
        "emotion": None,
        "language": None,
        "usage": {},
    }


def test_real_local_asr_http_roundtrip(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    adapter = load_adapter()
    key = tmp_path / "dashscope.key"
    key.write_text("fixture\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            payload = json.dumps(
                {
                    "output": {
                        "choices": [
                            {
                                "message": {
                                    "content": [{"text": "本地协议测试"}],
                                    "annotations": [
                                        {
                                            "type": "audio_info",
                                            "emotion": "neutral",
                                            "language": "zh",
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                    "usage": {"seconds": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(adapter, "ASR_ENDPOINT", f"http://127.0.0.1:{server.server_port}/asr")
    try:
        result = adapter.asr_transcribe(b"\0\0" * 160)
        assert result == {
            "text": "本地协议测试",
            "emotion": "neutral",
            "language": "zh",
            "usage": {"seconds": 1},
        }
        assert len(received) == 1
        assert received[0]["model"] == "qwen3-asr-flash"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _tts_stub(adapter, monkeypatch, tmp_path, config, seen):
    key = tmp_path / "dashscope.key"
    key.write_text("fixture\n")
    key.chmod(0o600)
    monkeypatch.setattr(adapter, "KEY_PATH", key)
    path = tmp_path / "voice_config.json"
    if config is not None:
        path.write_text(json.dumps(config, ensure_ascii=False))
    monkeypatch.setattr(adapter, "VOICE_CONFIG_PATH", path)

    calls = {"n": 0}

    def send(request, timeout):
        calls["n"] += 1
        if request.data is not None:
            seen.append(json.loads(request.data))
        if calls["n"] == 1:
            return Response(
                json.dumps(
                    {
                        "output": {
                            "audio": {
                                "url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/s.mp3?E=1"
                            }
                        },
                        "usage": {"characters": 2},
                    }
                ).encode()
            )
        return Response(b"ID3fixture")

    monkeypatch.setattr(adapter, "_open", send)


def test_voice_parameters_from_config_reach_the_request(tmp_path, monkeypatch):
    adapter = load_adapter()
    seen: list[dict] = []
    _tts_stub(
        adapter,
        monkeypatch,
        tmp_path,
        {"rate": 0.9, "pitch": 1.05, "instruction": "沉稳克制", "model": "ignored", "voice": "x"},
        seen,
    )
    adapter.tts_synthesize("你好。")
    assert seen[0]["parameters"] == {"rate": 0.9, "pitch": 1.05, "instruction": "沉稳克制"}
    assert seen[0]["input"]["voice"] == adapter.VOICE_ID


def test_voice_parameters_override_and_reject_out_of_range(tmp_path, monkeypatch):
    adapter = load_adapter()
    seen: list[dict] = []
    _tts_stub(
        adapter, monkeypatch, tmp_path, {"rate": 5.0, "pitch": -1, "instruction": "x" * 500}, seen
    )
    adapter.tts_synthesize("你好。", instruction="像老朋友说话")
    assert seen[0]["parameters"] == {"instruction": "像老朋友说话"}


def test_absent_or_broken_voice_config_sends_no_parameters(tmp_path, monkeypatch):
    adapter = load_adapter()
    seen: list[dict] = []
    _tts_stub(adapter, monkeypatch, tmp_path, None, seen)
    adapter.tts_synthesize("你好。")
    assert "parameters" not in seen[0]

    second = tmp_path / "broken"
    second.mkdir()
    seen.clear()
    _tts_stub(adapter, monkeypatch, second, None, seen)
    (second / "voice_config.json").write_text("{not json")
    adapter.tts_synthesize("你好。")
    assert "parameters" not in seen[0]
