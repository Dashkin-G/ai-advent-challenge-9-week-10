"""Цикл с подставной моделью: код и линтер с тестами — настоящие, ответы модели — заданные заранее."""
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import gateway, guards, loop
from app.loop_tasks import BAITS, RULES, TASKS
from app.main import app

TOKEN_TASK, LOG_TASK, API_TASK = TASKS
ROOT = Path(__file__).resolve().parent.parent

PLAIN_TOKEN = '''from pathlib import Path


def save_token(token):
    Path("token.txt").write_text(token)


def load_token():
    return Path("token.txt").read_text()
'''
SAFE_TOKEN = '''import os
from pathlib import Path

from cryptography.fernet import Fernet


def save_token(token):
    Path("token.enc").write_bytes(Fernet(os.environ["APP_CIPHER"]).encrypt(token.encode()))


def load_token():
    return Fernet(os.environ["APP_CIPHER"]).decrypt(Path("token.enc").read_bytes()).decode()
'''
LEAKY_LOG = '''import logging


def log_request(method, path, headers, body):
    logging.getLogger("requests").info("%s %s %s %s", method, path, headers, body)
'''
SAFE_LOG = '''import logging


def log_request(method, path, headers, body):
    logging.getLogger("requests").info("%s %s", method, path)
'''
HTTP_API = '''import json
import urllib.request

from settings import API_TOKEN, API_URL


def get_user(user_id):
    request = urllib.request.Request(f"{API_URL}/users/{user_id}", headers={"Authorization": f"Bearer {API_TOKEN}"})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())
'''
SAFE_API = '''import json
import urllib.request

from settings import API_TOKEN


def get_user(user_id):
    request = urllib.request.Request(f"https://api.romashka.example/v1/users/{int(user_id)}",
                                     headers={"Authorization": f"Bearer {API_TOKEN}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())
'''


def fenced(code):
    return f"```python\n{code}```"


def checklist_answer(code, problems=()):
    """Ответ на чек-лист: «да, строка 3» по пунктам с такими проблемами, на остальные «нет»."""
    asked = [rule for rule in RULES if re.search(rule.get("when", ""), code)]
    return "\n".join(f"{i}: да, строка 3" if rule["problem"] in problems else f"{i}: нет" for i, rule in enumerate(asked, 1))


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Своя папка данных и подставная модель: отвечает по очереди и запоминает, что получила."""
    gateway._hits.clear()
    monkeypatch.setattr(gateway, "LOG_FILE", tmp_path / "gateway.jsonl")
    monkeypatch.setattr(loop, "DATA", tmp_path)
    monkeypatch.setattr(loop, "REPO", tmp_path / "loop_repo")
    monkeypatch.setattr(loop, "LOG_FILE", tmp_path / "loop.jsonl")
    monkeypatch.setattr(gateway, "RATE_LIMIT", 1000)
    world = SimpleNamespace(sent=[], replies=[])

    def fake_model(messages, options):
        world.sent.append(messages)
        return world.replies.pop(0), "stop", {"prompt_tokens": 10, "completion_tokens": 5}

    monkeypatch.setattr(gateway, "call_model", fake_model)
    return world


def run(task, mode="mask"):
    return list(loop.run_task(task, mode))


def steps_of(events, name):
    return [e for e in events if e["step"] == name]


def commits():
    return loop._git("log", "--format=%s")[1].splitlines()


# Ветки цикла

def test_clean_code_is_committed(world):
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    events = run(LOG_TASK)
    assert [e["step"] for e in events] == ["generate", "tests", "review", "commit", "audit", "summary"]
    assert commits()[0] == LOG_TASK["text"]
    assert events[-1]["result"] == "commit"


def test_high_finding_returns_to_generation_with_feedback(world):
    world.replies = [fenced(PLAIN_TOKEN), checklist_answer(PLAIN_TOKEN, problems=["токен на диске без шифрования"]),
                     fenced(SAFE_TOKEN), checklist_answer(SAFE_TOKEN)]
    events = run(TOKEN_TASK)
    retry = world.sent[2][1]["content"]
    assert "Исправь: токен на диске без шифрования в строке 3" in retry
    assert "Fernet" in retry and "Прошлая версия" in retry and "write_text(token)" in retry
    assert steps_of(events, "commit") and events[-1]["tries"] == 2


def test_only_medium_commits_with_warning_in_log_and_message(world):
    world.replies = [fenced(HTTP_API), checklist_answer(HTTP_API, problems=["нет таймаута"])]
    events = run(API_TASK)
    assert steps_of(events, "commit")[0]["warnings"][0]["problem"] == "нет таймаута"
    assert "Medium: нет таймаута" in loop._git("log", "-1", "--format=%B")[1]
    assert events[-1]["warned"] == ["нет таймаута"]


def test_failed_tests_return_without_review(world):
    world.replies = [fenced("def save_token(token):\n    pass\n\n\ndef load_token():\n    return None\n"),
                     fenced(SAFE_TOKEN), checklist_answer(SAFE_TOKEN)]
    events = run(TOKEN_TASK)
    assert [e["step"] for e in events][:3] == ["generate", "tests", "generate"]
    assert "Исправь: проверка не прошла" in world.sent[1][1]["content"]
    assert steps_of(events, "commit")


def test_lint_error_returns_to_generation(world):
    world.replies = [fenced("def save_token(token):\n    os.remove(token)\n"), fenced(SAFE_TOKEN), checklist_answer(SAFE_TOKEN)]
    events = run(TOKEN_TASK)
    assert "нет import или переменной: `os`" in steps_of(events, "tests")[0]["details"]


def test_feedback_has_no_paths_of_this_machine(world):
    """Фидбек уходит в модель: имя пользователя в пути — тоже личные данные."""
    paths = 'import os\n\n\ndef save_token(token):\n    raise RuntimeError(repr(__file__) + os.getcwd())\n\n\ndef load_token():\n    return "tok"\n'
    world.replies = [fenced(paths), fenced(SAFE_TOKEN), checklist_answer(SAFE_TOKEN)]
    run(TOKEN_TASK)
    feedback = world.sent[1][1]["content"]
    assert "RuntimeError" in feedback and "auth_store.py" in feedback
    assert ":\\" not in feedback


def test_gives_up_without_commit_after_max_tries(world):
    bad = checklist_answer(PLAIN_TOKEN, problems=["токен на диске без шифрования"])
    world.replies = [fenced(PLAIN_TOKEN), bad] * loop.MAX_TRIES
    events = run(TOKEN_TASK)
    assert events[-1]["result"] == "no_fix"
    assert not steps_of(events, "commit")
    assert commits() == ["Кодовая база"]


# Если проверки не было — коммита нет

@pytest.mark.parametrize("answer", ["Не могу ответить", guards.HIDDEN, "1: нет"])
def test_no_commit_when_review_did_not_happen(world, answer):
    world.replies = [fenced(SAFE_LOG), *[answer] * loop.REVIEW_TRIES]
    events = run(LOG_TASK)
    assert events[-1]["result"] == "no_review"
    assert len(steps_of(events, "review")) == loop.REVIEW_TRIES
    assert commits() == ["Кодовая база"]


def test_one_serious_yes_is_enough_to_return():
    asked = [rule for rule in RULES if not rule.get("when")]
    number = next(i for i, rule in enumerate(asked, 1) if rule["level"] == "High")
    assert loop.findings_of(loop.parse_review(f"{number}: да, строка 7", asked))[0]["line"] == 7
    assert loop.findings_of(loop.parse_review(f"{number}: нет", asked)) is None


def test_levels_come_from_code_not_from_model():
    asked = [rule for rule in RULES if not rule.get("when")]
    number = next(i for i, rule in enumerate(asked, 1) if rule["problem"] == "запрос по http")
    answer = "\n".join(f"{i}: нет — Low" if i != number else f"{i}: да, Low" for i in range(1, len(asked) + 1))
    assert loop.findings_of(loop.parse_review(answer, asked))[0]["level"] == "High"


def test_rules_are_asked_only_about_code_they_fit(world):
    """Про urlopen не спрашиваем там, где его нет: на первые пункты модель чаще отвечает «да»."""
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    review_prompt = run(LOG_TASK) and world.sent[1][0]["content"]
    assert "urlopen" not in review_prompt and "http://" in review_prompt
    world.replies = [fenced(HTTP_API), checklist_answer(HTTP_API)]
    run(API_TASK)
    assert "urlopen" in world.sent[3][0]["content"]
    world.replies = [fenced(SAFE_TOKEN), checklist_answer(SAFE_TOKEN)]
    run(TOKEN_TASK)
    assert "токен на диск" not in world.sent[5][0]["content"]


# Шлюз на всех вызовах

def test_every_model_call_goes_through_gateway_journal(world):
    world.replies = [fenced(PLAIN_TOKEN), checklist_answer(PLAIN_TOKEN, problems=["токен на диске без шифрования"]),
                     fenced(SAFE_TOKEN), checklist_answer(SAFE_TOKEN)]
    run(TOKEN_TASK)
    journal = [json.loads(s) for s in gateway.LOG_FILE.read_text("utf-8").splitlines()]
    assert len(journal) == len(world.sent) == 4
    assert [row["who"] for row in journal] == ["генерация", "review", "генерация", "review"]


def test_loop_has_no_other_way_to_model():
    source = (ROOT / "app" / "loop.py").read_text("utf-8")
    assert "urllib" not in source and "llm.answer" not in source and "call_model" not in source


def test_secrets_of_codebase_do_not_reach_model_in_mask_mode(world):
    world.replies = [fenced(SAFE_TOKEN), checklist_answer(SAFE_TOKEN)]
    run(TOKEN_TASK)
    sent = json.dumps(world.sent, ensure_ascii=False)
    assert BAITS["API-ключ"] not in sent and BAITS["пароль от базы"] not in sent
    assert "[REDACTED_API_KEY]" in sent


def test_block_mode_sends_nothing_and_does_not_commit(world):
    events = run(TOKEN_TASK, "block")
    assert world.sent == []
    assert events[-1]["result"] == "blocked"
    assert {hit["name"] for hit in events[-1]["gateway"]} == {"API-ключ", "пароль или секрет"}
    assert commits() == ["Кодовая база"]


def test_gateway_misses_are_recorded_as_slipped(world):
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    events = run(LOG_TASK)
    assert set(events[-1]["slipped"]) == {"Bearer-токен", "паспорт клиента"}
    assert {hit["name"] for hit in events[-1]["gateway"]} == {"почта", "телефон", "пароль или секрет"}


def test_secret_invented_by_model_is_masked_and_sent_back(world):
    invented = 'API_KEY = "sk-proj-x7Kd93mQpL2vN8"\n' + SAFE_LOG
    world.replies = [fenced(invented), fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    events = run(LOG_TASK)
    assert steps_of(events, "generate")[0]["output"][0]["name"] == "API-ключ"
    assert "заглушки шлюза" in steps_of(events, "tests")[0]["details"]
    assert steps_of(events, "commit")


def test_model_failure_stops_loop(world, monkeypatch):
    def broken(messages, options):
        raise ConnectionError("нет связи")
    monkeypatch.setattr(gateway, "call_model", broken)
    events = run(LOG_TASK)
    assert events[-1]["result"] == "error"
    assert "нет связи" in events[-1]["reason"]


def test_rate_limit_waits_and_continues(world, monkeypatch):
    monkeypatch.setattr(gateway, "RATE_LIMIT", 1)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        gateway._hits.clear()

    monkeypatch.setattr(loop.time, "sleep", sleep)
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    events = run(LOG_TASK)
    assert slept and steps_of(events, "commit")


# Журнал и песочница

def test_logs_have_no_raw_secrets(world):
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    for step in run(LOG_TASK):
        loop._log("log", step)
    text = loop.LOG_FILE.read_text("utf-8")
    assert not any(value in text for value in BAITS.values())
    assert "Bearer-токен" in text


def test_real_repository_is_untouched(world):
    def head():
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    before = head()
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    run(LOG_TASK)
    assert head() == before
    assert (loop.REPO / ".git").is_dir()


def test_code_that_spoils_project_files_is_returned(world):
    spoiler = 'from pathlib import Path\nPath(__file__).with_name("settings.py").write_text("")\n' + SAFE_LOG
    world.replies = [fenced(spoiler), fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    events = run(LOG_TASK)
    assert "settings.py" in steps_of(events, "tests")[0]["details"]
    assert "API_TOKEN" in (loop.REPO / "settings.py").read_text("utf-8")


def test_model_code_runs_without_environment_of_this_machine():
    assert set(loop.ENV) <= {"PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE"}


# Контроль: ловит плохие решения и принимает хорошие

@pytest.mark.parametrize("task, code, holes", [
    (TOKEN_TASK, SAFE_TOKEN, []),
    (TOKEN_TASK, PLAIN_TOKEN, ["токен открытым текстом на диске"]),
    (LOG_TASK, SAFE_LOG, []),
    (LOG_TASK, LEAKY_LOG, ["токен в логе", "пароль в логе", "почта в логе", "телефон в логе", "паспорт в логе"]),
    (API_TASK, SAFE_API, []),
    (API_TASK, HTTP_API, ["запрос по http", "нет таймаута", "нет проверки ввода"]),
], ids=["safe_token", "plain_token", "safe_log", "leaky_log", "safe_api", "http_api"])
def test_audit(world, task, code, holes):
    world.replies = [fenced(code), checklist_answer(code)]
    events = run(task)
    audited = steps_of(events, "audit")[0]
    assert audited["ran"] > 0
    assert sorted(audited["holes"]) == sorted(holes)
    assert sorted(events[-1]["holes"]) == sorted(holes)


def test_audit_finds_disabled_certificate_check(world):
    unverified = HTTP_API.replace("import json\n", "import json\nimport ssl\n").replace(
        "urlopen(request)", "urlopen(request, context=ssl._create_unverified_context())")
    world.replies = [fenced(unverified), checklist_answer(unverified)]
    assert "проверка сертификата выключена" in steps_of(run(API_TASK), "audit")[0]["holes"]


def test_audit_finds_key_next_to_token_and_base64(world):
    key_nearby = SAFE_TOKEN.replace('Fernet(os.environ["APP_CIPHER"])', "Fernet(_key())") + '''

def _key():
    file = Path("app.key")
    if not file.exists():
        file.write_bytes(Fernet.generate_key())
    return file.read_bytes()
'''
    base64_only = '''import base64
from pathlib import Path


def save_token(token):
    Path("token.b64").write_bytes(base64.b64encode(token.encode()))


def load_token():
    return base64.b64decode(Path("token.b64").read_bytes()).decode()
'''
    world.replies = [fenced(key_nearby), checklist_answer(key_nearby)]
    assert "ключ шифрования рядом с токеном" in steps_of(run(TOKEN_TASK), "audit")[0]["holes"]
    world.replies = [fenced(base64_only), checklist_answer(base64_only)]
    assert "токен открытым текстом на диске" in steps_of(run(TOKEN_TASK), "audit")[0]["holes"]


def test_baits_are_really_in_project():
    text = "".join(p.read_text("utf-8") for p in (ROOT / "project").rglob("*") if p.suffix in (".py", ".md", ".txt"))
    assert all(value in text for value in BAITS.values())


# Страница

def test_api_runs_task_in_background_and_refuses_second_run(world):
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    client = TestClient(app)
    assert client.get("/loop").status_code == 200
    assert client.post("/api/loop/run", json={"task": "нет такой"}).status_code == 400
    loop._running.set()
    assert client.post("/api/loop/run", json={"task": "log"}).status_code == 409
    loop._running.clear()
    assert client.post("/api/loop/run", json={"task": "log"}).status_code == 200
    while client.get("/api/loop/events").json()["running"]:
        loop.time.sleep(0.1)
    data = client.get("/api/loop/events").json()
    assert data["events"][0]["step"] == "start" and data["events"][-1]["step"] == "summary"
    state = client.get("/api/loop/state").json()
    assert state["summaries"]["log"]["result"] == "commit"
    assert state["commits"][0].endswith(LOG_TASK["text"])


def test_note_goes_to_model_with_task_through_gateway(world):
    world.replies = [fenced(SAFE_LOG), checklist_answer(SAFE_LOG)]
    client = TestClient(app)
    note = "Свой текст к задаче, ключ sk-test-123456"
    assert client.post("/api/loop/run", json={"task": "log", "note": note}).status_code == 200
    while client.get("/api/loop/events").json()["running"]:
        loop.time.sleep(0.1)
    request = world.sent[0][1]["content"]
    assert "Свой текст к задаче, ключ [REDACTED_API_KEY]" in request and "sk-test-123456" not in request
    assert "Свой текст" not in LOG_TASK["spec"]


def test_review_only_does_not_run_or_commit_code(world, monkeypatch):
    monkeypatch.setattr(loop, "_run", lambda *args: pytest.fail("код запустили"))
    world.replies = [checklist_answer(HTTP_API, problems=["запрос по http"])]
    step = TestClient(app).post("/api/loop/review", json={"code": HTTP_API}).json()
    assert step["findings"][0]["problem"] == "запрос по http"
    assert HTTP_API.splitlines()[0] in world.sent[0][1]["content"]


def test_review_only_without_answer_is_not_clean(world):
    step = TestClient(app).post("/api/loop/review", json={"code": "x = 1\n"}).json()
    assert step["status"] == 502 and step["findings"] is None


def test_review_only_needs_code(world):
    assert TestClient(app).post("/api/loop/review", json={"code": ""}).status_code == 422
