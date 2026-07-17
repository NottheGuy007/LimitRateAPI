# LimitRateAPI

A lightweight **OpenAI-compatible LLM gateway** with per-provider **outgoing rate shaping**. Over-limit requests are *buffered and sent at the configured cadence* instead of being rejected with a 429.

> Think "a tiny self-hosted proxy" that splits your RPM budget into "send once every *N* seconds" and queues everything else.

---

## Features

- **OpenAI-compatible API** — `/v1/chat/completions`, `/v1/completions`, `/v1/models`. Works with any OpenAI SDK.
- **Any OpenAI-compatible upstream** — OpenAI, Nvidia NIM, DeepSeek, Moonshot, Ollama, vLLM, LocalAI, etc. No per-provider adapters.
- **Two rate-shaping modes** (configurable per provider):
  - `spaced` — strict, uniform interval. `rpm: 60` → one send every 1s.
  - `burst` — token bucket with capacity. `rpm: 120, capacity: 10` → burst of 10, then 2/s.
- **Queue, don't reject** — over-limit requests wait by default. Set `max_wait_seconds` to return HTTP 503 + `Retry-After` instead.
- **SSE streaming** forwarded transparently.
- **Live monitoring** — `/admin/stats` shows queue depth, configured interval, last send time per provider.
- **No auth required locally** — can run without a gateway token for local/LAN use.
- **Model name routing** — models from all providers are merged into a single list; `<provider>/<model>` syntax disambiguates duplicates.

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/YOUR_USERNAME/LimitRateAPI.git
cd LimitRateAPI
make venv install

# 2. Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your provider API keys and rate limits

# 3. Run
make run
```

The server starts on the host:port configured in `config.yaml` (default `0.0.0.0:8080`).

### Using with OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="any-value"  # gateway doesn't validate unless auth_token is set
)

response = client.chat.completions.create(
    model="z-ai/glm-5.1",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

### Using with curl

```bash
# List available models
curl http://localhost:8080/v1/models

# Send a chat request
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-ai/glm-5.1",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Check rate limiter status
curl http://localhost:8080/admin/stats
```

---

## Configuration

```yaml
server:
  host: 0.0.0.0
  port: 8080
  # auth_token: ${KEEPALIVE_TOKEN}   # Comment out or remove to disable

providers:
  - name: my-provider
    base_url: https://api.example.com/v1
    api_key: ${MY_API_KEY}            # Or hardcode: "sk-xxxx"
    models: [model-a, model-b]        # Model names exposed by this provider
    rate_limit:
      mode: spaced                    # spaced | burst
      rpm: 60                         # → one send every 1 second
      # interval_seconds: 2           # optional; stricter of rpm/interval wins
      max_wait_seconds: null          # null = wait forever; or 60 → 503 after 60s
```

### Where to put API keys

In your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export NVIDIA_API_KEY="nvapi-xxxxxxxxxxxxxxxxxxxx"
export AGNES_API_KEY="sk-xxxxxxxxxxxxxxxxxxxx"
export OLLAMACLOUD_API_KEY="xxxxxxxxxxxxxxxxxxxxxxx"
```

Then in `config.yaml` reference them as `${NVIDIA_API_KEY}`. This way your real keys never appear in the config file.

### Model routing

The gateway merges all `models` lists from all providers into a single index. When a request arrives:
1. The `model` field is matched against the index.
2. If found, the request is proxied to the owning provider.
3. If multiple providers expose the same model name, the first one wins.
4. Use `<provider>/<model>` (e.g. `openai/gpt-4o`) to force-route to a specific provider.

---

## Verifying Rate Limiting

### 1. Live dashboard

```bash
curl http://localhost:8080/admin/stats
```

Response example:
```json
{
  "providers": [
    {
      "provider": "Nvidia-NIM",
      "mode": "spaced",
      "interval_seconds": 1.5,
      "capacity": 1,
      "tokens": -2.0,
      "queued_ahead": 2,
      "waiting_in_acquire": 1,
      "last_send_time": 1234567.89
    }
  ]
}
```

Key fields:
| Field | Meaning |
|---|---|
| `tokens` | `> 0` = available burst budget; `< 0` = requests queued ahead |
| `queued_ahead` | Number of requests waiting in line |
| `waiting_in_acquire` | Currently waiting to send |
| `last_send_time` | Timestamp of the last upstream request |

### 2. Manual stress test

Open three terminals and send requests simultaneously:

```bash
# Terminal 1-3: send at the same time
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "z-ai/glm-5.1", "messages": [{"role":"user","content":"hi"}]}' &
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "z-ai/glm-5.1", "messages": [{"role":"user","content":"hi"}]}' &
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "z-ai/glm-5.1", "messages": [{"role":"user","content":"hi"}]}' &

# Watch the queue depth
watch -n 0.5 'curl -s http://localhost:8080/admin/stats | python3 -m json.tool'
```

With a 40 RPM (1.5s interval) provider, you should see:
- Request 1: fires immediately (tokens: 0.0, queued_ahead: 0)
- Request 2: waits ~1.5s (tokens: -1.0, queued_ahead: 1)
- Request 3: waits ~3.0s (tokens: -2.0, queued_ahead: 2)

### 3. Timeout verification

If `max_wait_seconds: 30` is set, sending 50 requests at once will cause some to get HTTP 503 with a `Retry-After` header.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI chat completions (supports `stream: true`) |
| POST | `/v1/completions` | Legacy text completions |
| GET | `/v1/models` | Aggregated model list from all providers |
| GET | `/admin/health` | Health check |
| GET | `/admin/stats` | Per-provider rate limiter snapshot |

---

## Project Layout

```
LimitRateAPI/
├── app/
│   ├── main.py            # FastAPI app + lifespan
│   ├── config.py          # YAML loader with ${ENV} expansion
│   ├── models.py          # Pydantic data models
│   ├── rate_limiter.py    # Token bucket with negative balance (core)
│   ├── registry.py        # Model-name → provider routing
│   ├── proxy.py           # HTTPX upstream proxying + SSE
│   └── routes/
│       ├── chat.py        # Chat/completion endpoints
│       ├── models_list.py # /v1/models
│       └── admin.py       # Health + stats
├── tests/                 # Pytest suite (rate limiter, config, integration)
├── config.example.yaml    # Configuration template
├── requirements.txt
└── Makefile
```

---

## Running Tests

```bash
make dev      # Install dev dependencies
make test     # Run all 27 tests
```

---

## License

MIT

---

---

# LimitRateAPI

轻量级 **OpenAI 兼容的 LLM 网关**，支持**按提供商分别限速**。超出限制的请求会被**缓冲并按配置节奏发送**，而不是直接返回 429。

> 简单说：一个自托管的小型代理，把你的 RPM 预算拆成"每 *N* 秒发一次"，多出来的排队。

---

## 功能特性

- **OpenAI 兼容 API** — `/v1/chat/completions`、`/v1/completions`、`/v1/models`，兼容任何 OpenAI SDK。
- **任意 OpenAI 兼容上游** — OpenAI、Nvidia NIM、DeepSeek、月之暗面、Ollama、vLLM、LocalAI 等，无需适配器。
- **两种限速模式**（每个提供商可独立配置）：
  - `spaced`（均匀间隔）— `rpm: 60` → 每 1 秒发一次。
  - `burst`（脉冲模式）— `rpm: 120, capacity: 10` → 初始爆发 10 个，之后每秒 2 个。
- **排队不拒绝** — 默认无限等待。设置 `max_wait_seconds` 后会返回 HTTP 503 + `Retry-After`。
- **SSE 流式转发** — 透明透传。
- **实时监控** — `/admin/stats` 查看队列深度、配置间隔、上次发送时间。
- **本地运行无需认证** — 可关掉网关 token，适合局域网使用。
- **模型名路由** — 所有提供商的模型合并到一个列表；同名时用 `<提供商>/<模型>` 语法区分。

---

## 快速开始

```bash
# 1. 克隆并安装依赖
git clone https://github.com/YOUR_USERNAME/LimitRateAPI.git
cd LimitRateAPI
make venv install

# 2. 配置
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入你的 API Key 和限速参数

# 3. 运行
make run
```

服务器会在 `config.yaml` 配置的地址上启动（默认 `0.0.0.0:8080`）。

### 用 OpenAI SDK 调用

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="任意值"  # 网关默认不校验
)

response = client.chat.completions.create(
    model="z-ai/glm-5.1",
    messages=[{"role": "user", "content": "你好！"}],
)
print(response.choices[0].message.content)
```

### 用 curl 调用

```bash
# 查看可用模型
curl http://localhost:8080/v1/models

# 发送聊天请求
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-ai/glm-5.1",
    "messages": [{"role": "user", "content": "你好！"}]
  }'

# 查看限速器状态
curl http://localhost:8080/admin/stats
```

---

## 配置说明

```yaml
server:
  host: 0.0.0.0
  port: 8080
  # auth_token: ${KEEPALIVE_TOKEN}   # 注释掉或删除即不认证

providers:
  - name: my-provider
    base_url: https://api.example.com/v1
    api_key: ${MY_API_KEY}            # 或直接写 "sk-xxxx"
    models: [model-a, model-b]        # 该提供商提供的模型
    rate_limit:
      mode: spaced                    # spaced | burst
      rpm: 60                         # → 每 1 秒发一次
      # interval_seconds: 2           # 可选；取 rpm/interval 中更严格那个
      max_wait_seconds: null          # null = 无限等待；或 60 → 等60秒后返回503
```

### API Key 放哪里

在 shell 配置文件（`~/.zshrc` 或 `~/.bashrc`）中设置环境变量：

```bash
export NVIDIA_API_KEY="nvapi-xxxxxxxxxxxxxxxxxxxx"
export AGNES_API_KEY="sk-xxxxxxxxxxxxxxxxxxxx"
export OLLAMACLOUD_API_KEY="xxxxxxxxxxxxxxxxxxxxxxx"
```

然后在 `config.yaml` 中用 `${NVIDIA_API_KEY}` 引用。这样真实密钥不会出现在配置文件中。

### 模型路由

网关将所有提供商的 `models` 列表合并为一个索引。请求到达时：
1. 用 `model` 字段匹配索引。
2. 匹配成功则转发给对应提供商。
3. 同名模型，最先声明的提供商胜出。
4. 用 `<提供商>/<模型>` 语法（如 `openai/gpt-4o`）强制路由到指定提供商。

---

## 验证限速效果

### 1. 实时仪表盘

```bash
curl http://localhost:8080/admin/stats
```

返回示例：
```json
{
  "providers": [
    {
      "provider": "Nvidia-NIM",
      "mode": "spaced",
      "interval_seconds": 1.5,
      "capacity": 1,
      "tokens": -2.0,
      "queued_ahead": 2,
      "waiting_in_acquire": 1,
      "last_send_time": 1234567.89
    }
  ]
}
```

关键字段含义：
| 字段 | 含义 |
|---|---|
| `tokens` | `> 0` = 剩余爆发额度；`< 0` = 有请求在排队 |
| `queued_ahead` | 排队的请求数 |
| `waiting_in_acquire` | 正在等待发送的请求 |
| `last_send_time` | 上次发送时间戳 |

### 2. 手动压力测试

开三个终端同时发请求：

```bash
# 终端 1-3：同时发送
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "z-ai/glm-5.1", "messages": [{"role":"user","content":"hi"}]}' &
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "z-ai/glm-5.1", "messages": [{"role":"user","content":"hi"}]}' &
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "z-ai/glm-5.1", "messages": [{"role":"user","content":"hi"}]}' &

# 观察队列变化
watch -n 0.5 'curl -s http://localhost:8080/admin/stats | python3 -m json.tool'
```

对于 40 RPM（间隔 1.5s）的提供商，你会看到：
- 请求 1：立即发送（tokens: 0.0, queued_ahead: 0）
- 请求 2：等待约 1.5 秒（tokens: -1.0, queued_ahead: 1）
- 请求 3：等待约 3.0 秒（tokens: -2.0, queued_ahead: 2）

### 3. 超时验证

如果设置了 `max_wait_seconds: 30`，一次性发 50 个请求，超过 30 秒的请求会收到 HTTP 503 + `Retry-After` 头。

---

## API 参考

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 聊天补全（支持 `stream: true`） |
| POST | `/v1/completions` | 传统文本补全 |
| GET | `/v1/models` | 所有提供商的模型列表 |
| GET | `/admin/health` | 健康检查 |
| GET | `/admin/stats` | 各提供商限速器状态 |

---

## 项目结构

```
LimitRateAPI/
├── app/
│   ├── main.py            # FastAPI 应用 + 生命周期
│   ├── config.py          # YAML 加载器，支持 ${ENV} 展开
│   ├── models.py          # Pydantic 数据模型
│   ├── rate_limiter.py    # 令牌桶（可负数，核心算法）
│   ├── registry.py        # 模型名 → 提供商路由
│   ├── proxy.py           # HTTPX 上游代理 + SSE
│   └── routes/
│       ├── chat.py        # 聊天/补全端点
│       ├── models_list.py # /v1/models
│       └── admin.py       # 健康检查 + 统计
├── tests/                 # 测试套件（限速器、配置、集成测试）
├── config.example.yaml    # 配置模板
├── requirements.txt
└── Makefile
```

---

## 运行测试

```bash
make dev      # 安装测试依赖
make test     # 运行全部 27 个测试
```

---

## License

MIT
