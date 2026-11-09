from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field


SERVICE_NAME = "vocu-openai-tts-bridge"
DEFAULT_BASE_URLS = "https://v1.vocu.studio,https://v1.wusound.cn"
# Sites that share the same API but issue their own keys. A key rejected with
# 401 by one site is retried on the next. VOCU_BASE_URL (single site) is still
# honored when VOCU_BASE_URLS is unset.
VOCU_BASE_URLS = [
    url.strip().rstrip("/")
    for url in (
        os.getenv("VOCU_BASE_URLS")
        or os.getenv("VOCU_BASE_URL")
        or DEFAULT_BASE_URLS
    ).split(",")
    if url.strip()
] or DEFAULT_BASE_URLS.split(",")
VOCU_MODEL = os.getenv("VOCU_MODEL", "v4.0").strip() or "v4.0"
VOCU_TIMEOUT_SECONDS = float(os.getenv("VOCU_TIMEOUT_SECONDS", "360"))
AUDIO_DOWNLOAD_TIMEOUT_SECONDS = float(
    os.getenv("AUDIO_DOWNLOAD_TIMEOUT_SECONDS", "90")
)
FFMPEG_TIMEOUT_SECONDS = float(os.getenv("FFMPEG_TIMEOUT_SECONDS", "90"))
MAX_CONCURRENCY = max(1, int(os.getenv("VOCU_MAX_CONCURRENCY", "1")))
MAX_AUDIO_BYTES = max(1, int(os.getenv("MAX_AUDIO_BYTES", str(64 * 1024 * 1024))))
VOCU_MAX_TEXT_CHARACTERS = max(
    1, int(os.getenv("VOCU_MAX_TEXT_CHARACTERS", "1200"))
)
VOCU_PROMPT_ID = os.getenv("VOCU_PROMPT_ID", "default")
# V4 only: balanced / expressive / similarity. "balance" is silently dropped.
VOCU_REFERENCE_MODE = os.getenv("VOCU_REFERENCE_MODE", "balanced")
VOCU_LANGUAGE = os.getenv("VOCU_LANGUAGE", "auto")
VOCU_FLASH = os.getenv("VOCU_FLASH", "false").lower() in {"1", "true", "yes"}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(SERVICE_NAME)
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# Remembers which site last accepted a key (by SHA-256, never the key itself)
# so later requests go straight there instead of paying for a 401 first.
_KEY_ROUTE_LIMIT = 1024
_key_routes: OrderedDict[str, str] = OrderedDict()


def _key_digest(authorization: str) -> str:
    return hashlib.sha256(authorization.encode("utf-8")).hexdigest()


def candidate_base_urls(authorization: str) -> list[str]:
    preferred = _key_routes.get(_key_digest(authorization))
    if preferred not in VOCU_BASE_URLS:
        return list(VOCU_BASE_URLS)
    return [preferred] + [url for url in VOCU_BASE_URLS if url != preferred]


def remember_base_url(authorization: str, base_url: str) -> None:
    digest = _key_digest(authorization)
    _key_routes[digest] = base_url
    _key_routes.move_to_end(digest)
    while len(_key_routes) > _KEY_ROUTE_LIMIT:
        _key_routes.popitem(last=False)


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "vocu"
    input: str = Field(min_length=1, max_length=20_000)
    voice: str = Field(min_length=1, max_length=256)
    response_format: str = "mp3"
    speed: float = Field(default=1.0, gt=0)


# V4 inline markers: {{开心}}, {{pause 1.5s}}. The LLM may also write the
# full-width ｛｛开心｝｝ form; it is normalized before cleanup.
_MARKER = re.compile(r"\{\{\s*([^{}\n]{1,16}?)\s*\}\}")
_FULLWIDTH_MARKER = re.compile(r"｛｛\s*([^｛｝\n]{1,16}?)\s*｝｝")
# Stage directions: （微笑）(waves) 【叹气】 *歪头*. Kept short so real
# parenthesized sentences are not eaten.
_STAGE = re.compile(
    r"（[^（）]{0,30}）|\([^()]{0,30}\)|【[^【】]{0,30}】|(?<!\*)\*(?!\*)[^*\n]{1,30}\*(?!\*)"
)
_MD_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_SYMBOLS = re.compile(r"^\s*(#{1,6}|>|[-*+]|\d+\.)\s+|[`|]+", re.MULTILINE)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U0000FE0F\U0000200D\U000020E3]+"
)
_KAOMOJI = re.compile(r"[（(][^（）()\n]{0,12}[ω▽д´`°ﾟ∀◕‿≧≦●・][^（）()\n]{0,12}[）)]")


def clean_tts_text(text: str) -> str:
    """Remove what should not be read aloud; keep V4 {{...}} markers."""
    text = _FULLWIDTH_MARKER.sub(r"{{\1}}", text)
    # Park markers so the cleanup rules below cannot touch them.
    markers: list[str] = []

    def park(match: re.Match[str]) -> str:
        markers.append("{{" + match.group(1).strip() + "}}")
        return f"\x00{len(markers) - 1}\x00"

    text = _MARKER.sub(park, text)
    text = _MD_EMPHASIS.sub(r"\2", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _KAOMOJI.sub("", text)
    text = _STAGE.sub("", text)
    text = _EMOJI.sub("", text)
    text = _MD_SYMBOLS.sub("", text)
    text = re.sub(r"[ \t]*\n+[ \t]*", "，", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: markers[int(m.group(1))], text)
    # A message that was only markers and stage directions has nothing to say.
    spoken = _MARKER.sub("", text)
    if not re.search(r"\w", spoken):
        return ""
    return text.strip(" ，,")


def build_vocu_payload(payload: SpeechRequest, text: str) -> dict[str, Any]:
    # V4.0 is selected by the VOCU request-body model field. Pinning the
    # upstream model here keeps the OpenAI-facing model name from accidentally
    # selecting a different VOCU generation path. V3-only options (preset,
    # vivid, break_clone) are left out; V4 reads emotion from {{...}} markers.
    speech_rate = min(2.0, max(0.5, 1.0 / payload.speed))
    return {
        "model": VOCU_MODEL,
        "voiceId": payload.voice,
        "text": text,
        "promptId": VOCU_PROMPT_ID,
        "reference_mode": VOCU_REFERENCE_MODE,
        "language": VOCU_LANGUAGE,
        "speechRate": speech_rate,
        "flash": VOCU_FLASH,
        "stream": False,
        "seed": -1,
        "srt": False,
    }


def error_response(
    status_code: int,
    message: str,
    *,
    code: str,
    request_id: str | None = None,
) -> JSONResponse:
    headers = {
        "Cache-Control": "no-store",
        # OpenAI SDKs otherwise retry 408/409/429/5xx responses automatically.
        # A failed post-synthesis download must never trigger duplicate billing.
        "x-should-retry": "false",
    }
    if request_id:
        headers["X-Vocu-App-Request-Id"] = request_id
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "message": message,
                "type": "vocu_upstream_error",
                "code": code,
            }
        },
    )


def vocu_request_id(response: aiohttp.ClientResponse, payload: Any = None) -> str:
    header_id = response.headers.get("X-Vocu-App-Request-Id", "").strip()
    if header_id:
        return header_id
    if isinstance(payload, dict):
        body_id = payload.get("requestId") or payload.get("request_id")
        if isinstance(body_id, str):
            return body_id.strip()
    return ""


async def read_json_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
    if response.content_length and response.content_length > 2 * 1024 * 1024:
        raise ValueError("VOCU returned an oversized JSON response")
    raw = await response.read()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("VOCU returned an oversized JSON response")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("VOCU returned a non-object JSON response")
    return payload


async def validate_audio_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("VOCU returned an audio URL with an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise ValueError("VOCU returned an invalid HTTPS audio URL")

    blocked_names = {
        "localhost",
        "host.docker.internal",
        "gateway.docker.internal",
        "metadata.google.internal",
    }
    if (
        host in blocked_names
        or host.endswith((".localhost", ".local", ".internal"))
        or ("." not in host and ":" not in host)
    ):
        raise ValueError("VOCU returned a non-public audio hostname")

    try:
        literal_address = ipaddress.ip_address(host.split("%", 1)[0])
        addresses = {literal_address}
    except ValueError:
        try:
            address_info = await asyncio.get_running_loop().getaddrinfo(
                host,
                443,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError("VOCU audio hostname could not be resolved") from exc
        addresses = {
            ipaddress.ip_address(item[4][0].split("%", 1)[0])
            for item in address_info
        }

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("VOCU returned an audio URL resolving to a non-public IP")


async def download_audio(session: aiohttp.ClientSession, audio_url: str) -> bytes:
    current_url = audio_url
    timeout = aiohttp.ClientTimeout(
        total=AUDIO_DOWNLOAD_TIMEOUT_SECONDS,
        connect=min(15, AUDIO_DOWNLOAD_TIMEOUT_SECONDS),
    )

    for _ in range(4):
        await validate_audio_url(current_url)

        async with session.get(
            current_url,
            allow_redirects=False,
            timeout=timeout,
            headers={"Accept": "audio/mpeg, application/octet-stream"},
        ) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "").strip()
                if not location:
                    raise ValueError("VOCU audio redirect has no Location header")
                current_url = urljoin(current_url, location)
                continue

            if response.status != 200:
                raise ValueError(
                    f"VOCU audio download failed with HTTP {response.status}"
                )
            if response.content_length and response.content_length > MAX_AUDIO_BYTES:
                raise ValueError("VOCU audio exceeds the configured size limit")

            chunks = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                chunks.extend(chunk)
                if len(chunks) > MAX_AUDIO_BYTES:
                    raise ValueError("VOCU audio exceeds the configured size limit")
            if not chunks:
                raise ValueError("VOCU returned an empty audio file")
            return bytes(chunks)

    raise ValueError("VOCU audio URL redirected too many times")


def finalize_streamed_wav(data: bytes) -> bytes:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("ffmpeg did not return a valid RIFF/WAVE stream")
    output = bytearray(data)
    struct.pack_into("<I", output, 4, min(len(output) - 8, 0xFFFFFFFF))
    data_offset = output.find(b"data", 12)
    if data_offset < 0 or data_offset + 8 > len(output):
        raise ValueError("ffmpeg WAV stream has no data chunk")
    struct.pack_into(
        "<I",
        output,
        data_offset + 4,
        min(len(output) - data_offset - 8, 0xFFFFFFFF),
    )
    return bytes(output)


async def mp3_to_wav(audio: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(audio), timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError("ffmpeg conversion timed out") from None
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise ValueError(f"ffmpeg conversion failed: {detail or 'unknown error'}")
    return finalize_streamed_wav(stdout)


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = aiohttp.ClientTimeout(
        total=VOCU_TIMEOUT_SECONDS,
        connect=min(20, VOCU_TIMEOUT_SECONDS),
        sock_read=VOCU_TIMEOUT_SECONDS,
    )
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENCY + 2, ttl_dns_cache=300)
    app.state.http = aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        trust_env=True,
        headers={"User-Agent": f"{SERVICE_NAME}/1.0"},
    )
    logger.info(
        "bridge_ready sites=%s vocu_model=%s max_text_characters=%s "
        "max_concurrency=%s upstream_timeout_seconds=%s reference_mode=%s",
        ",".join(VOCU_BASE_URLS),
        VOCU_MODEL,
        VOCU_MAX_TEXT_CHARACTERS,
        MAX_CONCURRENCY,
        VOCU_TIMEOUT_SECONDS,
        VOCU_REFERENCE_MODE,
    )
    try:
        yield
    finally:
        await app.state.http.close()


app = FastAPI(
    title=SERVICE_NAME,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    del request
    first_error = exc.errors()[0] if exc.errors() else {}
    message = str(first_error.get("msg", "Invalid request body"))
    return error_response(400, message, code="invalid_request")


@app.get("/healthz")
async def healthz() -> dict[str, str | int]:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "vocu_model": VOCU_MODEL,
        "max_text_characters": VOCU_MAX_TEXT_CHARACTERS,
    }


@app.post("/v1/audio/speech")
async def create_speech(payload: SpeechRequest, request: Request) -> Response:
    local_request_id = str(uuid.uuid4())
    started = time.monotonic()
    authorization = request.headers.get("Authorization", "").strip()
    if not authorization.lower().startswith("bearer ") or len(
        authorization.split(None, 1)
    ) != 2:
        return error_response(
            401,
            "Missing Bearer API key",
            code="missing_api_key",
            request_id=local_request_id,
        )

    output_format = payload.response_format.lower()
    if output_format not in {"wav", "mp3", "mpeg"}:
        return error_response(
            400,
            "Only wav and mp3 response formats are supported",
            code="unsupported_response_format",
            request_id=local_request_id,
        )

    text = clean_tts_text(payload.input)
    if not text:
        # Nothing speakable (only emoji or stage directions). AstrBot then
        # falls back to sending the text message.
        return error_response(
            400,
            "Input has no speakable text after cleanup",
            code="empty_after_cleanup",
            request_id=local_request_id,
        )

    if len(text) > VOCU_MAX_TEXT_CHARACTERS:
        return error_response(
            400,
            f"V4.0 input exceeds {VOCU_MAX_TEXT_CHARACTERS} characters; "
            "split long text before synthesis",
            code="text_too_long",
            request_id=local_request_id,
        )

    # OpenAI speed > 1 means faster. VOCU speechRate > 1 means longer/slower,
    # so use the reciprocal to preserve OpenAI-compatible semantics.
    vocu_payload = build_vocu_payload(payload, text)

    upstream_request_id = ""
    synthesis_completed = False
    try:
        candidates = candidate_base_urls(authorization)
        async with semaphore:
            for attempt, base_url in enumerate(candidates):
                is_last = attempt == len(candidates) - 1
                site = urlsplit(base_url).hostname or base_url
                async with request.app.state.http.post(
                    f"{base_url}/api/tts/simple-generate",
                    json=vocu_payload,
                    headers={
                        "Authorization": authorization,
                        "Accept": "application/json",
                    },
                ) as upstream:
                    try:
                        upstream_payload = await read_json_response(upstream)
                    except (
                        json.JSONDecodeError,
                        UnicodeDecodeError,
                        ValueError,
                    ) as exc:
                        if upstream.status == 401 and not is_last:
                            logger.info(
                                "auth_fallback id=%s rejected_by=%s",
                                local_request_id,
                                site,
                            )
                            continue
                        upstream_request_id = vocu_request_id(upstream)
                        logger.warning(
                            "request_failed id=%s stage=upstream_json site=%s "
                            "status=%s elapsed_ms=%d",
                            local_request_id,
                            site,
                            upstream.status,
                            int((time.monotonic() - started) * 1000),
                        )
                        return error_response(
                            401 if upstream.status == 401 else 502,
                            str(exc),
                            code="invalid_upstream_response",
                            request_id=upstream_request_id or local_request_id,
                        )

                    upstream_request_id = vocu_request_id(upstream, upstream_payload)
                    body_status = upstream_payload.get("status")
                    auth_failed = upstream.status == 401 or body_status in {
                        401,
                        "401",
                    }
                    if auth_failed and not is_last:
                        # The key belongs to another site; nothing was
                        # synthesized or billed here, so trying the next is safe.
                        logger.info(
                            "auth_fallback id=%s rejected_by=%s",
                            local_request_id,
                            site,
                        )
                        continue
                    if not auth_failed:
                        remember_base_url(authorization, base_url)

                    if upstream.status != 200 or body_status not in {200, "200"}:
                        status_code = 401 if auth_failed else upstream.status
                        if status_code < 400 or status_code > 599:
                            status_code = 502
                        message = str(
                            upstream_payload.get("message")
                            or f"VOCU request failed with HTTP {upstream.status}"
                        )
                        if auth_failed and len(candidates) > 1:
                            message = (
                                f"API key rejected by all {len(candidates)} "
                                f"sites; last error: {message}"
                            )
                        code = str(
                            upstream_payload.get("code") or "vocu_request_failed"
                        )
                        logger.warning(
                            "request_failed id=%s stage=vocu site=%s status=%s "
                            "code=%s elapsed_ms=%d",
                            local_request_id,
                            site,
                            status_code,
                            code,
                            int((time.monotonic() - started) * 1000),
                        )
                        return error_response(
                            status_code,
                            message,
                            code=code,
                            request_id=upstream_request_id or local_request_id,
                        )

                    data = upstream_payload.get("data")
                    audio_url = data.get("audio") if isinstance(data, dict) else None
                    if not isinstance(audio_url, str) or not audio_url.strip():
                        return error_response(
                            422,
                            "VOCU response did not contain data.audio",
                            code="missing_audio_url",
                            request_id=upstream_request_id or local_request_id,
                        )
                    synthesis_completed = True
                    audio = await download_audio(
                        request.app.state.http, audio_url.strip()
                    )
                break

        if output_format == "wav":
            audio = await mp3_to_wav(audio)
            media_type = "audio/wav"
            filename = "speech.wav"
        else:
            media_type = "audio/mpeg"
            filename = "speech.mp3"

        logger.info(
            "request_complete id=%s format=%s bytes=%d elapsed_ms=%d",
            local_request_id,
            output_format,
            len(audio),
            int((time.monotonic() - started) * 1000),
        )
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Request-Id": local_request_id,
        }
        if upstream_request_id:
            headers["X-Vocu-App-Request-Id"] = upstream_request_id
        return Response(content=audio, media_type=media_type, headers=headers)

    except asyncio.TimeoutError:
        logger.warning(
            "request_failed id=%s stage=timeout elapsed_ms=%d",
            local_request_id,
            int((time.monotonic() - started) * 1000),
        )
        return error_response(
            504,
            "VOCU request timed out",
            code="upstream_timeout",
            request_id=upstream_request_id or local_request_id,
        )
    except (aiohttp.ClientError, ValueError) as exc:
        status_code = 422 if synthesis_completed else 502
        error_code = (
            "audio_processing_failed"
            if synthesis_completed
            else "bridge_processing_failed"
        )
        logger.warning(
            "request_failed id=%s stage=bridge synthesis_completed=%s "
            "error=%s elapsed_ms=%d",
            local_request_id,
            synthesis_completed,
            type(exc).__name__,
            int((time.monotonic() - started) * 1000),
        )
        return error_response(
            status_code,
            str(exc),
            code=error_code,
            request_id=upstream_request_id or local_request_id,
        )
