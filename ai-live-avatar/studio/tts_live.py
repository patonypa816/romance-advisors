"""Short live-dialogue TTS segments; importing this module never contacts a service."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import aiohttp

from action_schema import LONG_SPEECH_MAX_TEXT
from semantic_speech import text_without_whitespace
from speech_markup import SpeechMarkupError, parse_speech_markup

MODEL = "qwen-audio-3.1-tts-flash"
VOICE = "libai_v3.1"


def valid_voice_id(value: Any) -> bool:
    """音色只能是供应商标识，不能把 URL、空白或控制字符带进请求与证据。"""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is not None


MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_AUDIO_SECONDS = 30.0
MAX_SSE_BYTES = 6 * 1024 * 1024
MAX_SSE_EVENT_BYTES = 3 * 1024 * 1024
MAX_SSE_EVENTS = 512
MAX_EVENT_LOG_BYTES = 256 * 1024

_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "headers",
    "request_headers", "response_headers", "api_key", "apikey", "token",
    "access_token", "secret", "password", "data", "b64_json", "audio_data",
}


def _safe(value: Any, secret: str) -> Any:
    """Keep evidence while excluding credentials, signed URLs and audio payloads."""
    if isinstance(value, dict):
        return {
            str(_safe(str(key), secret)): (
                "[redacted]" if str(key).lower() in _SENSITIVE_KEYS
                else _safe(item, secret)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item, secret) for item in value]
    if isinstance(value, str):
        value = value.replace(secret, "[redacted]") if secret else value
        return _URL.sub("[redacted-url]", value)
    return value


def _save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _token(value: Any) -> int | None:
    # bool is an int subclass, but is not valid billing evidence.
    return value if type(value) is int and value >= 0 else None


def _unavailable(reason: str, pcm_sha256: str | None = None) -> dict:
    return {"status": "unavailable", "reason": reason, "words": [],
            "timebase": None, "granularity_ms": None, "pcm_sha256": pcm_sha256}


class _StreamError(Exception):
    """Only fixed local error codes may cross the transport boundary."""


async def _read_timestamp_stream(response: Any, out: Path, secret: str) -> tuple[dict, dict]:
    """Read bounded SSE without retaining signed URLs or base64 audio in evidence.

    Words are cumulative per-sentence snapshots, not deltas. Empty snapshots do
    not erase the last list. Multi-sentence output is accepted only when the
    provider supplies already global indices and times; no offset is inferred.
    """
    state = {"words": [], "sentence_indices": set(), "sentence_end": False,
             "reason": None, "audio_bytes": 0, "audio_hash": hashlib.sha256(),
             "events": 0, "sentences": {}}
    body: dict = {"output": {"audio": {}}, "usage": {}}
    pending = bytearray()
    data_lines: list[bytes] = []
    event_bytes = total = log_bytes = 0
    stopped = False
    with (out / "events-safe.jsonl").open("x", encoding="utf-8") as log:
        def log_entry(entry: dict) -> None:
            nonlocal log_bytes
            raw = json.dumps(_safe(entry, secret), ensure_ascii=False) + "\n"
            size = len(raw.encode("utf-8"))
            if log_bytes + size <= MAX_EVENT_LOG_BYTES:
                log.write(raw)
                log_bytes += size

        def consume() -> None:
            nonlocal stopped
            if not data_lines:
                return
            raw = b"\n".join(data_lines)
            if raw.strip() == b"[DONE]":
                return
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                raise _StreamError("invalid_response") from None
            state["events"] += 1
            if state["events"] > MAX_SSE_EVENTS or not isinstance(event, dict):
                raise _StreamError("invalid_response")
            output = event.get("output")
            if not isinstance(output, dict):
                raise _StreamError("invalid_response")
            # Additional data after the final stop has no verified interpretation.
            if stopped:
                raise _StreamError("invalid_response")
            if isinstance(event.get("request_id"), str):
                body["request_id"] = _safe(event["request_id"], secret)[:256]
            if isinstance(event.get("usage"), dict):
                body["usage"] = {key: _token(event["usage"].get(key)) for key in
                                 ("input_tokens", "output_tokens", "total_tokens")}
            audio = output.get("audio")
            if isinstance(audio, dict):
                chunk = audio.get("data")
                if chunk:
                    try:
                        decoded = base64.b64decode(chunk, validate=True)
                    except (ValueError, TypeError):
                        state["reason"] = "stream_audio_invalid"
                    else:
                        state["audio_bytes"] += len(decoded)
                        if state["audio_bytes"] > MAX_AUDIO_BYTES:
                            raise _StreamError("audio_too_large")
                        state["audio_hash"].update(decoded)
                if isinstance(audio.get("url"), str):
                    body["output"]["audio"]["url"] = audio["url"]
            sentence = output.get("sentence")
            if sentence is not None:
                if (not isinstance(sentence, dict) or type(sentence.get("index")) is not int
                        or not 0 <= sentence["index"] < MAX_SSE_EVENTS):
                    state["reason"] = "invalid_sentence"
                else:
                    index = sentence["index"]
                    state["sentence_indices"].add(index)
                    sentences = state["sentences"]
                    if index not in sentences:
                        if (index != len(sentences) or any(
                                not entry["ended"] for entry in sentences.values())):
                            state["reason"] = "invalid_sentence_sequence"
                        sentences[index] = {"words": [], "ended": False}
                    elif index != max(sentences) or sentences[index]["ended"]:
                        state["reason"] = "invalid_sentence_sequence"
                    current = sentences[index]
                    words = sentence.get("words")
                    if not isinstance(words, list) or len(words) > LONG_SPEECH_MAX_TEXT:
                        state["reason"] = "invalid_timestamps"
                    elif words:
                        # Restrict evidence to bounded fields; malformed values are
                        # remembered as unavailable rather than blocking usable audio.
                        valid = all(isinstance(w, dict) and isinstance(w.get("text"), str)
                                    and len(w["text"]) <= LONG_SPEECH_MAX_TEXT
                                    and all(type(w.get(k)) is int for k in
                                            ("begin_index", "end_index", "begin_time", "end_time"))
                                    for w in words)
                        if not valid:
                            state["reason"] = "invalid_timestamps"
                        else:
                            snapshot = [{k: w[k] for k in ("text", "begin_index", "end_index",
                                                          "begin_time", "end_time")} for w in words]
                            previous = current["words"]
                            if snapshot[:len(previous)] != previous:
                                state["reason"] = "inconsistent_snapshots"
                            current["words"] = snapshot
                    if output.get("type") == "sentence-end":
                        current["ended"] = True
                    state["words"] = [word for key in sorted(sentences)
                                      for word in sentences[key]["words"]]
                    if len(state["words"]) > LONG_SPEECH_MAX_TEXT:
                        state["reason"] = "invalid_timestamps"
                    state["sentence_end"] = all(entry["ended"] for entry in sentences.values())
            stopped = output.get("finish_reason") == "stop"
            if stopped:
                body["output"]["finish_reason"] = "stop"
            log_entry({"event_number": state["events"], "sentence_index":
                       sentence.get("index") if isinstance(sentence, dict) and
                       type(sentence.get("index")) is int and
                       0 <= sentence["index"] < MAX_SSE_EVENTS else None,
                       "audio_bytes_received": state["audio_bytes"], "finished": stopped})

        def line(raw: bytes) -> None:
            nonlocal event_bytes
            raw = raw.rstrip(b"\r")
            if not raw:
                consume()
                data_lines.clear()
                event_bytes = 0
            elif raw.startswith(b"data:"):
                value = raw[5:].lstrip(b" ")
                event_bytes += len(value)
                if event_bytes > MAX_SSE_EVENT_BYTES:
                    raise _StreamError("invalid_response")
                data_lines.append(value)

        try:
            async for chunk in response.content.iter_chunked(65536):
                total += len(chunk)
                if total > MAX_SSE_BYTES:
                    raise _StreamError("invalid_response")
                pending.extend(chunk)
                while b"\n" in pending:
                    raw, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    line(raw)
                if len(pending) > MAX_SSE_EVENT_BYTES:
                    raise _StreamError("invalid_response")
            # A disconnected partial event must never count as final completion.
            if pending or data_lines or not stopped:
                raise _StreamError("tts_not_finished")
        finally:
            log_entry({"summary": True, "events": state["events"],
                       "audio_bytes_received": state["audio_bytes"],
                       "sentence_indices": sorted(state["sentence_indices"]),
                       "sentence_end": state["sentence_end"], "reason": state["reason"],
                       "final_words": state["words"], "sentences": [
                           {"index": index, "sentence_end": entry["ended"],
                            "final_words": entry["words"]}
                           for index, entry in sorted(state["sentences"].items())]})
    return body, state


def _verified_alignment(state: dict, text: str | None, seconds: float,
                        wav_sha256: str, pcm_sha256: str) -> dict:
    def unavailable(reason: str) -> dict:
        return _unavailable(reason, pcm_sha256)

    if state["reason"]:
        return unavailable(state["reason"])
    if not state["sentence_indices"] or state["sentence_indices"] != set(range(len(state["sentence_indices"]))):
        return unavailable("invalid_sentence")
    if (not state["sentence_end"] or not state["words"]
            or any(not entry["words"] for entry in state["sentences"].values())):
        return unavailable("timestamps_missing")
    if not state["audio_bytes"] or state["audio_hash"].hexdigest() != wav_sha256:
        return unavailable("stream_download_mismatch")
    words = state["words"]
    cursor = last_end = grid = 0
    for word in words:
        # Indices describe the provider's ordered units, not a verified count
        # of characters (e.g. one unit can contain "黢黢"). Keep words intact.
        begin, end = word["begin_time"], word["end_time"]
        if (not word["text"].strip() or word["begin_index"] != cursor or
                word["end_index"] <= cursor or begin < last_end or
                end <= begin or end > seconds * 1000):
            return unavailable("invalid_timestamps")
        cursor = word["end_index"]
        last_end = end
        # This is the observed integer-ms grid, not a claim of sample-level
        # accuracy or of a finer provider resolution than the received points.
        grid = math.gcd(grid, math.gcd(begin, end))
    return {"status": "ok", "reason": None, "words": words,
            "text_matches_script": (None if text is None else
                text_without_whitespace("".join(w["text"] for w in words)) == text_without_whitespace(text)),
            "timebase": "audio_start", "granularity_ms": grid or None,
            "pcm_sha256": pcm_sha256}


async def _process(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        # ffmpeg output is deliberately not forwarded into exception messages.
        raise RuntimeError("audio_tool_failed")
    return stdout


async def _probe(path: Path, *, pcm: bool = False) -> dict:
    args = ["ffprobe", "-v", "error"]
    if pcm:
        args += ["-f", "s16le", "-ar", "24000", "-ch_layout", "mono"]
    raw = await _process(
        *args, "-show_streams", "-show_format", "-of", "json", str(path)
    )
    return json.loads(raw)


def _audio_metadata(probe: dict) -> tuple[dict, float]:
    stream = next(s for s in probe["streams"] if s.get("codec_type") == "audio")
    seconds = float(stream.get("duration") or probe["format"]["duration"])
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_AUDIO_SECONDS:
        raise ValueError("invalid_audio_duration")
    if (int(stream["sample_rate"]) != 24000 or stream["channels"] != 1
            or stream["sample_fmt"] != "s16" or stream["codec_name"] != "pcm_s16le"):
        raise ValueError("unexpected_audio_format")
    return stream, seconds


async def synthesize_segment(
    http: aiohttp.ClientSession, api_key: str, workspace: str, out: Path,
    text: str, instruction: str, *, long_speech_test: bool = False,
    word_timestamps: bool = False, voice: str = VOICE, ssml: str | None = None,
) -> dict:
    """Make exactly one paid synthesis request, without retries or credential reads.

    The caller provides authorization and credentials. Each segment uses an empty
    output directory; a reserved request.json also prevents concurrent duplicates.
    All returned errors are fixed codes; server/HTTP exceptions can contain secrets.
    With word_timestamps=True, unavailable alignment never invalidates otherwise
    verified audio. Consumers must require alignment.status == 'ok' and match its
    pcm_sha256 to the PCM they play before scheduling timestamp-based actions.
    """
    result = {
        "status": "not_started", "model": MODEL, "voice": voice if valid_voice_id(voice) else None,
        "text": None, "text_codepoints": None, "request_id": None,
        "audio_seconds": None, "input_tokens": None, "output_tokens": None,
        "total_tokens": None, "cost_cny": None, "hour_estimate_cny": None,
        "wav_path": None, "pcm_path": None, "wav_sha256": None,
        "pcm_sha256": None, "usage_verified": False,
    }
    if word_timestamps is True:
        result["alignment"] = _unavailable("audio_unavailable")
    if (not isinstance(api_key, str) or not api_key or not isinstance(workspace, str)
            or not re.fullmatch(r"[A-Za-z0-9-]+", workspace) or not valid_voice_id(voice)):
        result["status"] = "invalid_arguments"
        return result
    if not isinstance(text, str) or not isinstance(instruction, str):
        result["status"] = "invalid_input"
        return result
    # Keep the legacy whitespace normalization for ordinary text. Annotated text
    # must retain its exact body, so SSML cannot silently alter display offsets.
    text = text.strip() if ssml is None else text
    instruction = instruction.strip()
    # The limit measures display text; markup has its own independent byte cap.
    text_limit = LONG_SPEECH_MAX_TEXT if long_speech_test is True or word_timestamps is True else 30
    if (not 1 <= len(text) <= text_limit or not text.strip()
            or not 1 <= len(instruction) <= 160 or api_key in text or api_key in instruction):
        result["status"] = "invalid_input"
        return result
    try:
        markup = parse_speech_markup(text, ssml)
    except SpeechMarkupError as exc:
        result["status"] = "invalid_input"
        result["reason"] = str(exc)
        return result
    if api_key in markup["tts_input"] or api_key in (markup["spoken_text"] or ""):
        result["status"] = "invalid_input"
        return result
    # A short display abbreviation can expand to a much longer sub alias. Apply
    # the same segment budget before paying; say-as remains provider-dependent.
    if markup["spoken_text"] is not None and len(markup["spoken_text"]) > text_limit:
        result["status"] = "invalid_input"
        result["reason"] = "spoken_text_too_long"
        return result
    result["text"] = text
    result["text_codepoints"] = len(text)
    result["speech_markup"] = markup
    result["spoken_text"] = markup["spoken_text"]
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        result["status"] = "artifact_exists"
        return result
    request = {"model": MODEL, "input": {
        "text": markup["tts_input"], "voice": voice, "instruction": instruction,
        "format": "wav", "sample_rate": 24000,
    }}
    request["input"].update(markup["tts_parameters"])
    if ssml is not None:
        request["input"]["enable_ssml"] = True
    if word_timestamps is True:
        request["input"]["word_timestamp_enabled"] = True
    try:
        with (out / "request.json").open("x", encoding="utf-8") as handle:
            json.dump(_safe(request, api_key), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        result["status"] = "artifact_exists"
        return result

    audio_url = None
    timestamp_state = None
    try:
        endpoint = (f"https://{workspace}.cn-beijing.maas.aliyuncs.com"
                    "/api/v1/services/audio/tts/SpeechSynthesizer")
        try:
            # A single POST, with redirects disabled and no retry wrapper.
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            if word_timestamps is True:
                headers["X-DashScope-SSE"] = "enable"
            async with http.post(
                endpoint, json=request,
                headers=headers,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                http_status = response.status
                if word_timestamps is True and 200 <= http_status < 300:
                    if response.content_type != "text/event-stream":
                        raise _StreamError("invalid_response")
                    body, timestamp_state = await _read_timestamp_stream(response, out, api_key)
                else:
                    try:
                        body = await response.json(content_type=None)
                    except Exception:
                        body = {"error": "non_json_response"}
                _save(out / "response-safe.json", {
                    "http_status": http_status, "body": _safe(body, api_key),
                })
        except _StreamError as exc:
            result["status"] = str(exc)
            return result
        except Exception:
            result["status"] = "transport_error"
            return result
        if not isinstance(body, dict):
            result["status"] = "invalid_response"
            return result
        result["request_id"] = _safe(body.get("request_id"), api_key)
        if not 200 <= http_status < 300:
            result["status"] = "error_response"
            return result
        output = body.get("output")
        if not isinstance(output, dict) or output.get("finish_reason") != "stop":
            result["status"] = "tts_not_finished"
            return result
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        result["input_tokens"] = _token(usage.get("input_tokens"))
        result["output_tokens"] = _token(usage.get("output_tokens"))
        result["total_tokens"] = _token(usage.get("total_tokens"))
        result["usage_verified"] = all(
            result[key] is not None for key in ("input_tokens", "output_tokens")
        )
        if result["usage_verified"]:
            result["cost_cny"] = (
                result["input_tokens"] * 1.5 + result["output_tokens"] * 12
            ) / 1_000_000
        audio = output.get("audio")
        audio_url = audio.get("url") if isinstance(audio, dict) else None
        if not isinstance(audio_url, str) or not audio_url.startswith(("https://", "http://")):
            result["status"] = "audio_url_missing"
            return result
        wav = out / "sample.wav"
        partial = out / "sample.wav.part"
        evidence = {"status": "not_started", "http_status": None,
                    "expected_bytes": None, "received_bytes": 0,
                    "content_type": None, "complete": False,
                    "partial_sha256": None, "error_type": None}
        try:
            # Override any session default Authorization: signed media needs no Key.
            async with http.get(
                audio_url, headers={"Authorization": ""}, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                evidence["http_status"] = response.status
                evidence["expected_bytes"] = response.content_length
                evidence["content_type"] = response.content_type
                if response.status != 200:
                    result["status"] = "download_failed"
                    evidence["status"] = "http_error"
                    return result
                if response.content_length is not None and response.content_length > MAX_AUDIO_BYTES:
                    result["status"] = "audio_too_large"
                    evidence["status"] = "size_limit_from_header"
                    return result
                with partial.open("xb") as handle:
                    async for chunk in response.content.iter_chunked(65536):
                        if evidence["received_bytes"] + len(chunk) > MAX_AUDIO_BYTES:
                            result["status"] = "audio_too_large"
                            evidence["status"] = "size_limit_during_download"
                            return result
                        handle.write(chunk)
                        evidence["received_bytes"] += len(chunk)
            if partial.stat().st_size == 0:
                result["status"] = "download_failed"
                evidence["status"] = "empty_audio"
                return result
            if (evidence["expected_bytes"] is not None
                    and evidence["expected_bytes"] != evidence["received_bytes"]):
                result["status"] = "download_failed"
                evidence["status"] = "incomplete_audio"
                return result
            partial.replace(wav)
            evidence["status"] = "downloaded"
            evidence["complete"] = True
        except Exception as exc:
            result["status"] = "download_failed"
            evidence["status"] = "exception"
            evidence["error_type"] = type(exc).__name__
            return result
        finally:
            # Keep bounded partial audio and evidence for failures; never delete
            # files from an earlier attempt or silently retry a paid request.
            if partial.exists():
                evidence["partial_sha256"] = hashlib.sha256(partial.read_bytes()).hexdigest()
            _save(out / "download-evidence.json", _safe(evidence, api_key))
        result["wav_path"] = str(wav)
        result["wav_sha256"] = hashlib.sha256(wav.read_bytes()).hexdigest()
        try:
            wav_probe = await _probe(wav)
            _save(out / "audio-metadata.json", _safe({"wav": wav_probe, "pcm": None}, api_key))
            _, seconds = _audio_metadata(wav_probe)
            result["audio_seconds"] = seconds
            if result["cost_cny"] is not None:
                result["hour_estimate_cny"] = result["cost_cny"] * 3600 / seconds
            pcm = out / "sample.pcm"
            await _process(
                "ffmpeg", "-v", "error", "-nostdin", "-n", "-i", str(wav),
                "-vn", "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
                "-f", "s16le", str(pcm),
            )
            pcm_probe = await _probe(pcm, pcm=True)
            _, pcm_seconds = _audio_metadata(pcm_probe)
            if pcm.stat().st_size % 2 or abs(pcm_seconds - seconds) > 0.05:
                raise ValueError("pcm_duration_mismatch")
            result["pcm_path"] = str(pcm)
            result["pcm_sha256"] = hashlib.sha256(pcm.read_bytes()).hexdigest()
            _save(out / "audio-metadata.json", _safe({"wav": wav_probe, "pcm": pcm_probe}, api_key))
        except Exception:
            result["status"] = "audio_unverified"
            return result
        if word_timestamps is True:
            result["alignment"] = _verified_alignment(
                timestamp_state, markup["spoken_text"], pcm_seconds,
                result["wav_sha256"], result["pcm_sha256"]
            )
            _save(out / "timestamps.json", _safe(result["alignment"], api_key))
        # Audio validity and billing verification are independent facts.
        result["status"] = "ok"
        return result
    except asyncio.CancelledError:
        result["status"] = "cancelled"
        raise
    finally:
        audio_url = None
        # Do not persist raw response, URL, headers or exception messages anywhere.
        _save(out / "result.json", _safe(result, api_key))
