#!/usr/bin/env python3
"""Diagnose an OpenAI-compatible provider without exposing its API key."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = Path(".codenova/config.yaml")
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.6-flash"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Result:
    name: str
    url: str
    status: int | None
    data: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe models, Chat Completions, and Responses endpoints."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--provider", default="qwen")
    parser.add_argument("--base-url", help="Override the provider base_url")
    parser.add_argument("--model", help="Override the provider model")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--sdk-only",
        action="store_true",
        help="Only run the exact CodeNova streaming client probe",
    )
    parser.add_argument(
        "--api-key-env",
        default="DASHSCOPE_API_KEY",
        help="Environment variable containing the API key",
    )
    return parser.parse_args()


def load_provider(path: Path, name: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for provider in raw.get("providers", []):
        if provider.get("name") == name:
            return provider
    raise ValueError(f"provider {name!r} not found in {path}")


def resolve_key(args: argparse.Namespace, provider: dict[str, Any]) -> tuple[str, str]:
    key = os.environ.get(args.api_key_env, "")
    if key:
        return key, args.api_key_env
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        return key, "OPENAI_API_KEY"

    configured = str(provider.get("api_key", ""))
    if configured.startswith("${") and configured.endswith("}"):
        env_name = configured[2:-1]
        return os.environ.get(env_name, ""), env_name
    if configured:
        return configured, f"{args.config} (不建议明文保存)"
    return "", ""


def compact_error(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            parts = [error.get("code"), error.get("type"), error.get("message")]
            text = ": ".join(str(part) for part in parts if part)
            if text:
                return text[:800]
        elif error:
            return str(error)[:800]
    return fallback[:800]


def request_json(
    name: str,
    method: str,
    url: str,
    key: str,
    timeout: float,
    body: dict[str, Any] | None = None,
) -> Result:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "codenova-provider-diagnostic/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return Result(name, url, response.status, data=data)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = None
        return Result(name, url, exc.code, data=data, error=compact_error(data, raw))
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", exc)
        return Result(name, url, None, error=f"{type(reason).__name__}: {reason}")
    except Exception as exc:  # Keep diagnostics useful for unusual proxy/TLS failures.
        return Result(name, url, None, error=f"{type(exc).__name__}: {exc}")


def show_result(result: Result) -> None:
    status = str(result.status) if result.status is not None else "NETWORK_ERROR"
    print(f"[{result.name}] {status}  {result.url}")
    if result.error:
        print(f"  {result.error}")
        return
    if result.name == "models":
        items = result.data.get("data", []) if isinstance(result.data, dict) else []
        print(f"  返回 {len(items)} 个模型")
        return
    if isinstance(result.data, dict):
        request_id = result.data.get("id") or result.data.get("request_id")
        print(f"  请求成功{f'，id={request_id}' if request_id else ''}")


def print_diagnosis(
    protocol: str, model: str, models: Result, chat: Result, responses: Result
) -> None:
    print("\n诊断结论:")
    if models.ok and isinstance(models.data, dict):
        ids = {
            item.get("id")
            for item in models.data.get("data", [])
            if isinstance(item, dict)
        }
        if ids and model not in ids:
            print(f"- /models 未列出 {model!r}；请检查模型权限、地域或模型名。")

    if chat.ok and not responses.ok:
        print("- Chat Completions 可用，但 Responses 不可用。")
        print("- 在 CodeNova 中把 protocol 改为 openai-compat。")
    elif responses.ok and not chat.ok:
        print("- Responses 可用；CodeNova 的 protocol: openai 路径可以使用。")
    elif chat.ok and responses.ok:
        print("- API Key、网络、模型名以及两个生成端点都正常。")
        print(f"- 当前 protocol={protocol!r}；若应用仍失败，请检查应用请求参数和日志。")
    else:
        results = (chat, responses)
        statuses = {item.status for item in results}
        if statuses == {401}:
            print("- 两个生成端点均返回 401：Key 无效，或 Key 与服务地域/套餐域名不匹配。")
        elif 403 in statuses:
            print("- 服务返回 403：Key 已识别，但工作空间或模型调用权限不足。")
        elif 429 in statuses:
            print("- 服务返回 429：额度、余额或速率限制问题。")
        elif all(item.status is None for item in results):
            print("- 两个请求都是网络错误：优先检查 DNS、代理、防火墙和 TLS 证书。")
        else:
            print("- 两个生成端点均失败；以上响应正文包含服务端给出的具体原因。")


def exception_chain(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(parts) < 5:
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


async def probe_codenova(
    provider: dict[str, Any], base_url: str, model: str, key: str, timeout: float
) -> tuple[bool, str]:
    """Run the same streaming client path used by CodeNova."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from codenova.client import create_client
    from codenova.config import ProviderConfig
    from codenova.conversation import ConversationManager
    from codenova.tools.base import StreamEnd, TextDelta

    config = ProviderConfig(
        name=str(provider.get("name", "diagnostic")),
        protocol=str(provider.get("protocol", "openai")),
        base_url=base_url,
        model=model,
        api_key=key,
        thinking=bool(provider.get("thinking", False)),
        context_window=int(provider.get("context_window", 0)),
        max_output_tokens=int(provider.get("max_output_tokens", 0)),
    )
    conversation = ConversationManager()
    conversation.add_user_message("只回复 OK")
    text_chars = 0
    saw_end = False
    try:
        async with asyncio.timeout(timeout):
            async for event in create_client(config).stream(conversation):
                if isinstance(event, TextDelta):
                    text_chars += len(event.text)
                elif isinstance(event, StreamEnd):
                    saw_end = True
        return True, f"流式调用完成，文本字符数={text_chars}, StreamEnd={saw_end}"
    except Exception as exc:
        return False, exception_chain(exc)


def main() -> int:
    args = parse_args()
    try:
        provider = load_provider(args.config, args.provider)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"配置读取失败: {exc}", file=sys.stderr)
        return 2

    base_url = (args.base_url or provider.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    model = args.model or provider.get("model") or DEFAULT_MODEL
    protocol = provider.get("protocol", "<未配置>")
    key, key_source = resolve_key(args, provider)
    if not key:
        print(
            f"没有找到 API Key。请先设置 {args.api_key_env} 环境变量。",
            file=sys.stderr,
        )
        return 2

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        print(f"base_url 必须是有效的 HTTPS URL: {base_url}", file=sys.stderr)
        return 2

    print(f"配置: provider={args.provider}, protocol={protocol}, model={model}")
    print(f"地址: {base_url}")
    print(f"Key 来源: {key_source}（值不会输出）")

    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443)})
        print(f"[DNS] OK  {parsed.hostname} -> {', '.join(addresses[:4])}")
    except socket.gaierror as exc:
        print(f"[DNS] FAILED  {parsed.hostname}: {exc}")

    if args.sdk_only:
        print("\n[CodeNova 实际调用链]", flush=True)
        sdk_ok, sdk_detail = asyncio.run(
            probe_codenova(provider, base_url, model, key, max(args.timeout, 30.0))
        )
        print(f"{'OK' if sdk_ok else 'FAILED'}  {sdk_detail}")
        return 0 if sdk_ok else 1

    models = request_json("models", "GET", f"{base_url}/models", key, args.timeout)
    chat = request_json(
        "chat.completions",
        "POST",
        f"{base_url}/chat/completions",
        key,
        args.timeout,
        {
            "model": model,
            "messages": [{"role": "user", "content": "只回复 OK"}],
            "max_tokens": 32,
            "stream": False,
            "enable_thinking": False,
        },
    )
    responses = request_json(
        "responses",
        "POST",
        f"{base_url}/responses",
        key,
        args.timeout,
        {"model": model, "input": "只回复 OK", "stream": False},
    )

    for result in (models, chat, responses):
        show_result(result)
    print_diagnosis(protocol, model, models, chat, responses)

    print("\n[CodeNova 实际调用链]", flush=True)
    sdk_ok, sdk_detail = asyncio.run(
        probe_codenova(provider, base_url, model, key, max(args.timeout, 30.0))
    )
    print(f"{'OK' if sdk_ok else 'FAILED'}  {sdk_detail}")
    if not sdk_ok and (chat.ok or responses.ok):
        print("- HTTP 基础调用成功而 CodeNova 调用失败，问题位于 SDK 流式调用或事件解析。")
    return 0 if sdk_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
