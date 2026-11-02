"""Модель в Ollama и строка, которую она получает."""
import json
import os
import urllib.request

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("MODEL", "qwen3.5:4b")
# Настройки Qwen для ответа без размышления; штраф за повторы выключен — он мешает отвечать числами из промпта
OPTIONS = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 0, "num_predict": 300,
           "num_ctx": 4096, "stop": ["<|im_end|>"]}
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # к Ollama — мимо прокси


def context(system, user):
    """Что получает модель — одна строка текста, здесь по кусочкам: метки ролей, правила, сообщение.

    Это шаблон чата Qwen: «system» и «user» — просто слова-метки внутри того же текста.
    """
    user = user.replace("<|", "< |")  # метки ролей пользователь подделать не сможет
    return [["mark", "<|im_start|>system\n"], ["system", system], ["mark", "<|im_end|>\n<|im_start|>user\n"],
            ["user", user], ["mark", "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"]]


def answer(parts):
    """Ответ модели по кусочкам. raw — строку отправляем как есть: шаблон чата уже в ней."""
    body = {"model": MODEL, "prompt": "".join(text for _, text in parts), "raw": True, "stream": True,
            "keep_alive": "30m", "options": OPTIONS}
    request = urllib.request.Request(f"{OLLAMA_URL}/api/generate", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    with _direct.open(request, timeout=300) as response:
        for line in response:
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            if chunk.get("response"):
                yield chunk["response"]
