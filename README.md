# vocu-openai-tts-bridge

把 [VOCU](https://www.vocu.ai/) / WuSound 语音合成接口包装成 OpenAI 兼容的 `POST /v1/audio/speech`，让只支持 OpenAI TTS 格式的客户端（AstrBot、OpenAI SDK 等）直接使用 VOCU 的音色。

A tiny FastAPI service that exposes VOCU text-to-speech as an OpenAI-compatible `/v1/audio/speech` endpoint.

## 特性

- **OpenAI 兼容**：请求体与 OpenAI TTS 一致（`model` / `input` / `voice` / `response_format` / `speed`），直接返回音频字节。
- **不保存密钥**：客户端的 `Authorization: Bearer <VOCU API Key>` 原样透传给 VOCU，服务本身不存任何凭据。
- **多站点自动路由**：同时支持 VOCU（`v1.vocu.studio`）和 WuSound（`v1.wusound.cn`）。两个站点接口相同但 Key 不通用，某个站点返回 401 时自动换下一个站点重试，全部拒绝才返回 401；成功过的 Key 会记住对应站点（内存中只存 Key 的 SHA-256），之后直接命中。
- **文本清洗**：朗读前去掉 Markdown 符号、emoji、颜文字和 `（微笑）` `*歪头*` 这类舞台提示，保留 VOCU V4 的 `{{开心}}`、`{{pause 1.5s}}` 情绪/停顿标记（全角 `｛｛｝｝` 会自动转换）。
- **wav / mp3 输出**：VOCU 返回 mp3，需要 wav 时用 ffmpeg 转码。
- **避免重复计费**：错误响应带 `x-should-retry: false`，防止 OpenAI SDK 自动重试导致重复合成。
- **下载防护**：只从公网 HTTPS 地址下载音频，逐跳校验重定向，限制音频大小。

## 快速开始

```bash
git clone <this-repo> && cd vocu-openai-tts-bridge
docker compose up -d --build
```

服务监听 `127.0.0.1:8000`。不使用 Docker 时需要 Python 3.11+ 和 ffmpeg：

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

## 调用

```bash
curl http://127.0.0.1:8000/v1/audio/speech \
  -H "Authorization: Bearer $VOCU_OR_WUSOUND_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"vocu","voice":"<VOCU voiceId>","input":"{{开心}}你好呀！","response_format":"mp3"}' \
  -o speech.mp3
```

| 字段 | 说明 |
| --- | --- |
| `voice` | VOCU 的音色 ID（`voiceId`），必填 |
| `input` | 要朗读的文本，清洗后不得超过 `VOCU_MAX_TEXT_CHARACTERS` |
| `response_format` | `mp3`（默认）或 `wav` |
| `speed` | 与 OpenAI 语义一致，大于 1 更快；内部换算为 VOCU 的 `speechRate`（范围 0.5–2.0） |
| `model` | 被忽略，上游模型由环境变量 `VOCU_MODEL` 决定 |

健康检查：`GET /healthz`。

### 在 AstrBot 中使用

添加一个 OpenAI TTS 服务提供商：API Base 填 `http://vocu-bridge:8000/v1`（同一 Docker 网络）或 `http://127.0.0.1:8000/v1`，API Key 填 VOCU 的 API Key，音色填 VOCU 的 voiceId。当文本清洗后没有可朗读内容时接口返回 400，AstrBot 会回退为发送文字。

## 配置

全部通过环境变量设置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VOCU_BASE_URLS` | `https://v1.vocu.studio,https://v1.wusound.cn` | 上游站点列表，逗号分隔，按顺序尝试；遇到 401 换下一个 |
| `VOCU_BASE_URL` | 无 | 旧的单站点写法，仅在未设置 `VOCU_BASE_URLS` 时生效 |
| `VOCU_MODEL` | `v4.0` | 上游模型 |
| `VOCU_PROMPT_ID` | `default` | 音色风格 ID |
| `VOCU_REFERENCE_MODE` | `balanced` | V4 参考模式：`balanced` / `expressive` / `similarity` |
| `VOCU_LANGUAGE` | `auto` | 语言 |
| `VOCU_FLASH` | `false` | 是否启用低延迟模式 |
| `VOCU_MAX_TEXT_CHARACTERS` | `1200` | 单次请求的最大字符数 |
| `VOCU_MAX_CONCURRENCY` | `1` | 同时向 VOCU 发起的请求数 |
| `VOCU_TIMEOUT_SECONDS` | `360` | 合成请求超时 |
| `AUDIO_DOWNLOAD_TIMEOUT_SECONDS` | `90` | 音频下载超时 |
| `FFMPEG_TIMEOUT_SECONDS` | `90` | wav 转码超时 |
| `MAX_AUDIO_BYTES` | `67108864` | 音频大小上限（字节） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 注意

- 服务自身不做鉴权，只是透传 Bearer Key。建议只在内网或本机回环上监听，不要直接暴露到公网。
- 不支持流式输出，也不支持 `opus` / `aac` / `flac` 等其他格式。
- 本项目与 VOCU、OpenAI 官方无关。
