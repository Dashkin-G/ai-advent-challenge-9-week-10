"""День 48: LLM-шлюз между пользователем и моделью. Клиентам — /v1/chat/completions, показ механизма — /gateway."""
import json
import math
import os
import secrets
import threading
import time
import urllib.request
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import cases, guards
from app.llm import MODEL

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = Path(os.getenv("DATA_DIR", ROOT / "data")) / "gateway.jsonl"
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://127.0.0.1:11434/v1")  # Ollama говорит на протоколе OpenAI
UPSTREAM_KEY = os.getenv("UPSTREAM_KEY", "")  # ключ облака знает только шлюз, клиентам он не нужен
EXTRA = {} if UPSTREAM_KEY else {"reasoning_effort": "none"}  # своя Ollama: qwen отвечает без размышлений
MODE = os.getenv("GUARD_MODE", "block")  # нашли секрет: block — не пускать, mask — замаскировать и пропустить
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))  # запросов в минуту с одного IP
DEFAULT_TOKENS = 300  # длина ответа, если клиент не просил другой: бережёт и время, и деньги
MAX_TOKENS = 1000  # потолок: длиннее не дадим, сколько ни проси. Код длиннее ответа в чате
# Модель своя и бесплатна; считаем, сколько стоило бы в облаке: Qwen3.5-Flash, $ за 1 млн токенов
PRICE_IN, PRICE_OUT = float(os.getenv("PRICE_IN", "0.1")), float(os.getenv("PRICE_OUT", "0.4"))

router = APIRouter()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler(None if UPSTREAM_KEY else {}))
_lock = threading.Lock()
_hits = defaultdict(deque)


class Message(BaseModel):
    role: str
    content: str = Field(max_length=20_000)  # только текст: что шлюз не может прочитать, то не пропускает


class Chat(BaseModel):
    """Лишние поля запроса (tools, stream, model…) отбрасываются: в модель уходит только проверенное."""
    messages: list[Message] = Field(min_length=1)
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(None, ge=1)


class Ask(Chat):
    mode: Literal["block", "mask"] = MODE


def line(**data):
    return json.dumps(data, ensure_ascii=False) + "\n"


def _window(ip):
    """Запросы этого IP за последнюю минуту. Вызывать под _lock."""
    window = _hits[ip]
    while window and time.monotonic() - window[0] >= 60:
        window.popleft()
    return window


def _wait(window):
    return math.ceil(60 - (time.monotonic() - window[0])) if len(window) >= RATE_LIMIT else 0


def _take(ip):
    """Rate limit: (можно ли, сколько осталось, через сколько секунд освободится место)."""
    with _lock:
        window = _window(ip)
        if wait := _wait(window):
            return False, 0, wait
        window.append(time.monotonic())
        return True, RATE_LIMIT - len(window), 0


def _audit(**row):
    """Журнал аудита — строка JSON на запрос. Текст в нём уже замаскирован: иначе журнал сам стал бы утечкой."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **row}
    with _lock, LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _brief(hits):
    return [{k: h[k] for k in ("kind", "name", "via", "preview")} for h in hits]


def _names(hits):
    return ", ".join(dict.fromkeys(h["name"] for h in hits))


def _error(kind, message, **extra):
    return {"error": {"type": kind, "message": message, **extra}}


def call_model(messages, options):
    """Запрос к модели по протоколу OpenAI: единственное место, откуда текст уходит наружу."""
    body = {"model": MODEL, "messages": messages, "stream": False, **EXTRA, **options}
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else {})
    request = urllib.request.Request(f"{UPSTREAM_URL}/chat/completions", json.dumps(body).encode(), headers)
    with _opener.open(request, timeout=300) as response:
        data = json.load(response)
    choice = data["choices"][0]
    return choice["message"].get("content") or "", choice.get("finish_reason", "stop"), data.get("usage") or {}


def process(chat, ip, mode, who="client"):
    """Путь запроса: лимит → Input Guard → модель → Output Guard → журнал.

    Отдаёт этапы по мере готовности — их показывает страница. Последний этап — то, что получает клиент.
    """
    free, left, wait = _take(ip)
    headers = {"X-RateLimit-Limit": str(RATE_LIMIT), "X-RateLimit-Remaining": str(left)}
    row = {"ip": ip, "who": who, "mode": mode}
    if not free:
        _audit(**row, action="limited", status=429)
        yield {"step": "done", "status": 429, "headers": headers | {"Retry-After": str(wait)},
               "body": _error("rate_limited", f"Не больше {RATE_LIMIT} запросов в минуту. Подождите {wait} с.")}
        return

    # Input Guard: нашли секрет — запрос не уходит или уходит замаскированным
    checked = []
    for m in chat.messages:
        text = guards.clean(m.content)
        found = guards.find(text)
        checked.append({"role": m.role, "text": text, "hits": found, "masked": guards.mask(text, found)})
    hits = [h for c in checked for h in c["hits"]]
    action = ("blocked" if mode == "block" else "masked") if hits else "passed"
    row |= {"action": action, "request": "\n".join(c["masked"] for c in checked if c["role"] != "system"),
            "input": _brief(hits)}
    if action == "blocked":
        message = f"Запрос заблокирован: {_names(hits)}. В модель ничего не ушло."
        _audit(**row, status=400)
        yield {"step": "input", "action": action, "messages": checked, "sent": None}
        yield {"step": "done", "status": 400, "headers": headers,
               "body": _error("secret_detected", message, findings=_brief(hits))}
        return

    # Метка в системном промпте: появилась в ответе — промпт утёк, даже если он переведён
    canary = f"CANARY-{secrets.token_hex(3)}"
    sent = [{"role": c["role"], "content": c["masked"] + (f"\nМетка: {canary}" if c["role"] == "system" else "")}
            for c in checked]
    yield {"step": "input", "action": action, "messages": checked, "sent": sent, "canary": canary}

    start = time.time()
    options = chat.model_dump(include={"temperature", "top_p"}, exclude_none=True)
    try:
        answer, finish, usage = call_model(sent, options | {"max_tokens": min(chat.max_tokens or DEFAULT_TOKENS, MAX_TOKENS)})
    except Exception as e:  # noqa: BLE001 — клиент должен узнать, что модель не ответила; секрета в тексте ошибки нет
        message = f"Модель не ответила: {e}"
        _audit(**row, status=502, error=message)
        yield {"step": "done", "status": 502, "headers": headers, "body": _error("upstream_error", message)}
        return

    # Output Guard: пока ответ не проверен, пользователь его не видит
    answer = guards.clean(answer)
    system = "\n".join(c["masked"] for c in checked if c["role"] == "system")
    shown, found = guards.check_output(answer, system, canary)
    tokens_in, tokens_out = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    cost = round((tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1_000_000, 8)
    seconds = round(time.time() - start, 1)
    _audit(**row, status=200, answer=shown, output=_brief(found), tokens_in=tokens_in, tokens_out=tokens_out,
           cost=cost, seconds=seconds)
    yield {"step": "output", "raw": answer, "hits": found, "shown": shown, "tokens_in": tokens_in,
           "tokens_out": tokens_out, "cost": cost, "seconds": seconds}
    yield {"step": "done", "status": 200, "headers": headers, "body": {
        "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()), "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": shown}, "finish_reason": finish}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out, "total_tokens": tokens_in + tokens_out},
        "gateway": {"action": action, "input": _brief(hits), "output": _brief(found), "cost_usd": cost}}}


@router.post("/v1/chat/completions")
def proxy(chat: Chat, request: Request):
    """Настоящий прокси по протоколу OpenAI: клиент получает только ответ после проверки."""
    *_, last = process(chat, request.client.host, MODE)
    return JSONResponse(last["body"], last["status"], last["headers"])


@router.post("/api/gateway/ask")
def ask(chat: Ask, request: Request):
    """То же для страницы: она показывает и этапы — что получила модель и что ответила до проверки."""
    stages = (line(**stage) for stage in process(chat, request.client.host, chat.mode))
    return StreamingResponse(stages, media_type="application/x-ndjson")


@router.get("/gateway")
def page():
    return FileResponse(ROOT / "static" / "gateway.html")


@router.get("/api/gateway/state")
def state():
    return {"model": MODEL, "mode": MODE, "limit": RATE_LIMIT, "price": [PRICE_IN, PRICE_OUT], "system": cases.SYSTEM,
            "asks": cases.ASKS, "tests": cases.results(), "log_file": LOG_FILE.name}


@router.get("/api/gateway/log")
def log(request: Request):
    """Журнал и итоги: сколько запросов, сколько перехвачено, сколько потрачено; сколько лимита занято сейчас."""
    with _lock:
        rows = [json.loads(s) for s in LOG_FILE.read_text("utf-8").splitlines()] if LOG_FILE.exists() else []
        window = _window(request.client.host)
        rate = {"used": len(window), "wait": _wait(window)}
    return {"rows": rows[-50:], "requests": len(rows), "caught": sum(bool(r.get("input") or r.get("output")) for r in rows),
            "tokens": sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in rows),
            "cost": round(sum(r.get("cost", 0) for r in rows), 8), "rate": rate}
