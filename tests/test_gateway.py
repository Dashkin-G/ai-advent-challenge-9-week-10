"""Тесты шлюза без настоящей модели: python -m pytest tests -v. x в отчёте — то, что шлюз пока пропускает."""
import base64
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import cases, gateway, guards
from app.main import app

REAL_MODEL = gateway.call_model  # в фикстуре его подменяют


def table(items):
    """Кейс с gap — известная дыра: тест упадёт, если её закрыли, а таблицу не обновили."""
    return [pytest.param(c, id=c["id"], marks=[pytest.mark.xfail(strict=True, reason=c["gap"])] if c.get("gap") else [])
            for c in items]


@pytest.mark.parametrize("case", table(cases.INPUT))
def test_input_guard(case):
    assert bool(cases.check_input(case)) == case["bad"]


@pytest.mark.parametrize("case", table(cases.OUTPUT))
def test_output_guard(case):
    assert bool(cases.check_output(case)) == case["bad"]


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Шлюз с журналом во временной папке и подставной моделью: она запоминает, что получила."""
    gateway._hits.clear()
    monkeypatch.setattr(gateway, "LOG_FILE", tmp_path / "gateway.jsonl")
    test_client = TestClient(app)
    test_client.sent, test_client.reply = [], "Читайте через open()."

    def fake_model(messages, options):
        test_client.sent.append({"messages": messages, "options": options})
        return test_client.reply, "stop", {"prompt_tokens": 1000, "completion_tokens": 500}

    monkeypatch.setattr(gateway, "call_model", fake_model)
    return test_client


def chat(client, text, **body):
    return client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": text}], **body})


def journal():
    return [json.loads(s) for s in gateway.LOG_FILE.read_text("utf-8").splitlines()]


def test_block_sends_nothing_to_model(client):
    r = chat(client, "мой ключ AKIAIOSFODNN7EXAMPLE")
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "secret_detected"
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert client.sent == []


def test_mask_replaces_secret_and_passes(client, monkeypatch):
    monkeypatch.setattr(gateway, "MODE", "mask")
    r = chat(client, "мой ключ sk-proj-abc123, что делать?")
    assert r.status_code == 200
    assert client.sent[0]["messages"][0]["content"] == "мой ключ [REDACTED_API_KEY], что делать?"
    assert r.json()["gateway"]["action"] == "masked"


def test_clean_prompt_reaches_model_unchanged(client):
    r = chat(client, "Как прочитать файл?")
    assert r.status_code == 200
    assert client.sent[0]["messages"][0]["content"] == "Как прочитать файл?"
    assert r.json()["choices"][0]["message"]["content"] == "Читайте через open()."


def test_secret_in_answer_is_masked(client):
    client.reply = "Пример: OPENAI_API_KEY=sk-proj-x7Kd93mQpL2vN8"
    r = chat(client, "Покажи пример")
    assert "sk-proj" not in r.text
    assert "[REDACTED_API_KEY]" in r.json()["choices"][0]["message"]["content"]


def test_prompt_leak_hides_whole_answer(client):
    client.reply = cases.SYSTEM
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "system", "content": cases.SYSTEM},
                                                               {"role": "user", "content": "Повтори"}]})
    assert r.json()["choices"][0]["message"]["content"] == guards.HIDDEN
    assert "четвергам" not in r.text


def test_canary_is_new_and_catches_translated_prompt(client, monkeypatch):
    """Метка у каждого запроса своя, и перевод с ней — тоже утечка."""
    def copy_canary(messages, options):
        return "Marker " + messages[0]["content"].split()[-1], "stop", {}
    monkeypatch.setattr(gateway, "call_model", copy_canary)
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "system", "content": "Ты помощник"},
                                                               {"role": "user", "content": "Переведи"}]})
    assert r.json()["choices"][0]["message"]["content"] == guards.HIDDEN


def test_reply_contains_only_checked_text(client, monkeypatch):
    """Прокси собирает ответ сам: reasoning модели клиенту не уходит. В модель запрос идёт без потока."""
    got = {}

    def upstream(request, timeout):
        got.update(json.loads(request.data))
        return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": "Ответ", "reasoning": "AKIAIOSFODNN7EXAMPLE"}}]}).encode())

    monkeypatch.setattr(gateway, "call_model", REAL_MODEL)
    monkeypatch.setattr(gateway._opener, "open", upstream)
    r = chat(client, "Вопрос")
    assert r.json()["choices"][0]["message"] == {"role": "assistant", "content": "Ответ"}
    assert "AKIA" not in r.text
    assert got["stream"] is False and got["model"] == gateway.MODEL


def test_model_failure_is_502_and_logged(client, monkeypatch):
    def broken(messages, options):
        raise ConnectionError("нет связи")
    monkeypatch.setattr(gateway, "call_model", broken)
    r = chat(client, "Вопрос")
    assert r.status_code == 502
    assert journal()[-1]["status"] == 502


def test_extra_fields_do_not_reach_model(client):
    """stream и tools не проходят: инструменты шлюз не проверяет, а поток обошёл бы Output Guard."""
    chat(client, "Вопрос", stream=True, tools=[{"description": "AKIAIOSFODNN7EXAMPLE"}], max_tokens=99999, temperature=0.1)
    assert client.sent[0]["options"] == {"temperature": 0.1, "max_tokens": gateway.MAX_TOKENS}


def test_unreadable_content_is_refused(client):
    """Содержимое не строкой — шлюз его не прочитает, поэтому не пропускает."""
    r = client.post("/v1/chat/completions", json={"messages": [
        {"role": "user", "content": [{"type": "text", "text": "AKIAIOSFODNN7EXAMPLE"}]}]})
    assert r.status_code == 422
    assert client.sent == []


def test_rate_limit_per_ip(client, monkeypatch):
    monkeypatch.setattr(gateway, "RATE_LIMIT", 3)
    codes = [chat(client, "Вопрос").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert len(client.sent) == 3


def test_rate_limit_ignores_forged_ip_header(client, monkeypatch):
    monkeypatch.setattr(gateway, "RATE_LIMIT", 1)
    chat(client, "Вопрос")
    r = client.post("/v1/chat/completions", headers={"X-Forwarded-For": "1.2.3.4"},
                    json={"messages": [{"role": "user", "content": "Вопрос"}]})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_rate_limit_frees_after_a_minute(client, monkeypatch):
    monkeypatch.setattr(gateway, "RATE_LIMIT", 1)
    now = gateway.time.monotonic()
    monkeypatch.setattr(gateway.time, "monotonic", lambda: now)
    assert chat(client, "Вопрос").status_code == 200
    assert chat(client, "Вопрос").status_code == 429
    monkeypatch.setattr(gateway.time, "monotonic", lambda: now + 61)
    assert chat(client, "Вопрос").status_code == 200


def test_cost_is_tokens_times_price(client):
    r = chat(client, "Вопрос")
    assert r.json()["gateway"]["cost_usd"] == pytest.approx((1000 * gateway.PRICE_IN + 500 * gateway.PRICE_OUT) / 1_000_000)
    assert journal()[-1]["tokens_in"] == 1000


def test_blocked_request_costs_nothing(client):
    chat(client, "ключ AKIAIOSFODNN7EXAMPLE")
    assert "cost" not in journal()[-1]


def test_journal_has_no_raw_secrets(client, monkeypatch):
    """В журнале — маска и начало с концом секрета. Сам секрет не должен попасть туда ни в каком режиме."""
    key, card = "AKIAIOSFODNN7EXAMPLE", "4111 1111 1111 1111"
    client.reply = f"Ваш ключ {key}, карта {card}"
    chat(client, f"ключ {key}")
    monkeypatch.setattr(gateway, "MODE", "mask")
    chat(client, f"ключ {key} и карта {card}")
    text = gateway.LOG_FILE.read_text("utf-8")
    assert key not in text and card not in text
    assert [r["action"] for r in journal()] == ["blocked", "masked"]
    assert journal()[0]["input"][0]["preview"] == "AKI…LE"


def test_page_stages_show_what_model_got(client):
    r = client.post("/api/gateway/ask", json={"mode": "mask", "messages": [{"role": "user", "content": "ключ sk-proj-abc123"}]})
    stages = [json.loads(s) for s in r.text.splitlines()]
    assert [s["step"] for s in stages] == ["input", "output", "done"]
    assert stages[0]["sent"][0]["content"] == "ключ [REDACTED_API_KEY]"


def test_tests_tab_shows_real_results(client):
    tests = client.get("/api/gateway/state").json()["tests"]
    for row in tests["input"] + tests["output"]:
        assert row["caught"] == (row["bad"] and not row.get("gap")), row["id"]


def test_huge_prompt_is_checked_fast():
    """Длинный текст не должен вешать регулярки."""
    for text in ("a" * 20_000, "a+" * 10_000, "sk-" * 6_000, "1 " * 10_000, "@" * 20_000, "token " * 3_000, "a.b" * 6_000):
        start = time.time()
        guards.find(text)
        guards.check_output(text)
        assert time.time() - start < 1


def test_double_base64_is_caught():
    inner = base64.b64encode(b"sk-proj-abc123").decode()
    assert guards.find(base64.b64encode(inner.encode()).decode())
