"""LLM provider — local Ollama over stdlib HTTP. No package, no API key (constraint-safe).

Version pinning: model + prompt_version are passed in and logged by the caller into
ai_telemetry.jsonl — every AI prediction in this pipeline is attributable.
Temperature 0 for reproducibility; schema-constrained output (`format` = JSON schema)
makes malformed responses a near-impossible event (P-AI05). One automatic retry,
downgrading schema -> plain JSON mode, for provider quirks.

Memory lifecycle (directive): a local model must not sit in RAM when the pipeline is
idle. `KEEP_ALIVE` keeps it resident only during the batch; `unload()` frees it the
moment computation stops (keep_alive=0). `loaded_models()` reports what is resident.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_MODEL = "llama3.2:3b"
TEMPERATURE = 0.0
REQUEST_TIMEOUT_S = 300  # local CPU inference can be slow; generous by design
KEEP_ALIVE = "5m"  # resident during the batch; run.py unloads it at the end
DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class LLMUnavailable(RuntimeError):
    """Ollama not reachable — caller may fall back to the regex path (named fallback)."""


class LLMBadOutput(RuntimeError):
    """Model answered but not with parseable JSON within retry budget."""


@dataclass(frozen=True)
class LLMResult:
    """Enriched container holding structured model response, metadata, and token telemetry."""
    data: dict
    model: str
    prompt_version: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None


def is_reachable(base_url: str, timeout: float = 2.0) -> bool:
    """Checks if the local Ollama daemon is running and reachable via HTTP GET /api/version.

    Args:
        base_url: Ollama base endpoint URL.
        timeout: Network probe timeout in seconds.

    Returns:
        True if reachable and status 200, False otherwise.

    Where called:
        Called by run.py before starting batch loop to select AI vs regex mode,
        and inside chat_json() before issuing model prompts.

    Where it sends information:
        Returned to run.py to set ai_available flag and output terminal log mode.
    """
    try:
        with urllib.request.urlopen(f"{base_url}/api/version", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def loaded_models(base_url: str = DEFAULT_BASE_URL) -> list[str]:
    """Inspects currently resident models in Ollama GPU/system memory via GET /api/ps.

    Args:
        base_url: Ollama base endpoint URL.

    Returns:
        List of resident model name strings.

    Where called:
        Called during memory management checks and integration test assertions.

    Where it sends information:
        Returns list to caller for audit logging and memory health verification.
    """
    try:
        with urllib.request.urlopen(f"{base_url}/api/ps", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "?") for m in data.get("models", [])]
    except Exception:
        return []


def unload(model: str, base_url: str = DEFAULT_BASE_URL) -> bool:
    """Ejects model weights from RAM/VRAM immediately by sending keep_alive=0 to /api/generate.

    Args:
        model: Model identifier string (e.g. 'llama3.2:3b').
        base_url: Ollama base endpoint URL.

    Returns:
        True on successful unload, False on network or request failure.

    Where called:
        Called at the very end of run.py (Step S11 cleanup) unless --keep-loaded is passed.

    Where it sends information:
        Sends POST request directly to local Ollama server, freeing ~2.0 GB system RAM.
    """
    try:
        req = urllib.request.Request(
            f"{base_url}/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _post_chat(payload: dict, base_url: str) -> dict:
    """Internal helper to dispatch JSON POST payload to Ollama /api/chat endpoint."""
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat_json(
    system: str,
    user: str,
    schema: dict | None,
    model: str,
    prompt_version: str,
    temperature: float = TEMPERATURE,
    base_url: str = DEFAULT_BASE_URL,
    keep_alive: str | int | None = KEEP_ALIVE,
    max_attempts: int = 2,
) -> LLMResult:
    """Executes a single schema-constrained chat completion and returns validated parsed JSON.

    Args:
        system: System instructions setting the role and schema contract.
        user: Input prompt containing document text, line items, or pair evidence.
        schema: Optional JSON Schema dict passed to Ollama format option for strict adherence.
        model: Name of model to invoke (e.g., 'llama3.2:3b').
        prompt_version: Version tag (e.g. 'ap-extract-v1', 'ap-forensic-v1') for audit tracking.
        temperature: Sampling temperature (pinned at 0.0 for determinism).
        base_url: Ollama API URL.
        keep_alive: Time to retain weights in memory (e.g. '5m').
        max_attempts: Retry budget (falls back from schema to generic JSON format on attempt 2).

    Returns:
        LLMResult containing parsed dict data, token counts, and request latency.

    Raises:
        LLMUnavailable: If Ollama server is offline.
        LLMBadOutput: If model fails to return parseable JSON within retry budget.

    Where called:
        Called by extract_invoice_ai() (S3), forensic_verdict() (S8),
        enrich_record() (S9), and decide_all() (S10).

    Where it sends information:
        Returns structured LLMResult to calling modules to populate intermediate records
        and feed ai_telemetry.jsonl.
    """
    if not is_reachable(base_url):
        raise LLMUnavailable(f"Ollama not reachable at {base_url}")

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    # attempt 0: strict JSON schema; attempt 1: plain JSON mode (provider fallback)
    formats: list = [schema if schema else "json", "json"]
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        payload = {
            "model": model,
            "stream": False,
            "messages": messages,
            "format": formats[min(attempt, len(formats) - 1)],
            "options": {"temperature": temperature},
        }
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive

        started = time.perf_counter()
        try:
            response = _post_chat(payload, base_url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            continue
        latency_ms = int((time.perf_counter() - started) * 1000)

        content = response.get("message", {}).get("content", "")
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                raise LLMBadOutput(f"model returned {type(data).__name__}, expected object")
        except (json.JSONDecodeError, LLMBadOutput) as exc:
            last_error = exc
            continue

        return LLMResult(
            data=data,
            model=model,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            prompt_tokens=response.get("prompt_eval_count"),
            completion_tokens=response.get("eval_count"),
        )

    raise LLMBadOutput(f"no valid JSON after {max_attempts} attempts: {last_error!r}")
