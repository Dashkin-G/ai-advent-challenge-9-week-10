"""День 49: execution loop с security step. Задача → генерация → тесты → security review → коммит.

Страница — /loop. С моделью цикл говорит только через шлюз дня 48: своего выхода к ней у него нет.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import gateway, guards
from app.llm import MODEL
from app.loop_tasks import BAITS, CODER_PROMPT, REVIEW_FORMAT, RULES, SECURITY_INTRO, TASKS

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "project"
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
REPO = DATA / "loop_repo"  # копия project/ со своим git: коммиты цикла остаются в ней
LOG_FILE = DATA / "loop.jsonl"
MAX_TRIES = 5  # попыток на задачу: дальше цикл останавливается без коммита
REVIEW_TRIES = 3  # ответ review скрыт шлюзом или не разобран: проверки не было, просим ещё раз
CODE_TOKENS, REVIEW_TOKENS = 1000, 300  # длина ответа: код длиннее ответов на чек-лист
RETURN_ON = ("Critical", "High")
ANSWER = re.compile(r"^\s*(\d+)\b[^\n]*?\b(да|нет)\b([^\n]*)", re.M | re.I)  # «4: да, строка 7»
# Вывод pytest для фидбека: без рамок, полоски прогресса, чужих файлов и абсолютных путей
NOISE = re.compile(r"^(?:[=_]{3,}.*|[.FEsx]+ +\[ *\d+%\]|E +\+ +where .*)\n?", re.M)
FOREIGN_FRAME = re.compile(r"^(?:[A-Za-z]:\\|<frozen ).*\n(?:[ \t]+.*\n)*", re.M)
PATH = re.compile(r"[A-Za-z]:\\+[^\s'\"]*")
# Код модели запускаем на этой машине: отдельный процесс, таймаут, в окружении только нужное Python
ENV = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if name in os.environ}
ENV |= {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
TIMEOUT = 60

router = APIRouter()
_running = threading.Event()
_events = []


def _run(*command):
    """(код возврата, вывод). Зависшая программа модели — тоже ошибка, а не остановка цикла."""
    try:
        done = subprocess.run(command, cwd=REPO, env=ENV, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 1, f"Не уложилось в {TIMEOUT} с"
    return done.returncode, done.stdout + done.stderr


def _git(*args):
    """Только в копии: --git-dir не даст git подняться выше и задеть настоящий репозиторий."""
    return _run("git", f"--git-dir={REPO / '.git'}", f"--work-tree={REPO}", "-c", "user.name=loop",
                "-c", "user.email=loop@localhost", *args)


def _prepare_repo():
    if (REPO / ".git").exists():
        return
    shutil.rmtree(REPO, ignore_errors=True)
    shutil.copytree(PROJECT, REPO, ignore=shutil.ignore_patterns("__pycache__"))
    _run("git", "init", "-q", str(REPO))
    _git("add", "-A")
    _git("commit", "-qm", "Кодовая база")


def _reset():
    """Чистая копия перед запуском: код модели может испортить файлы проекта — и портил."""
    _git("reset", "--hard", "-q")
    _git("clean", "-fdxq")


def ask(system, user, mode, who, max_tokens, temperature):
    """Единственный путь к модели — шлюз: лимит, проверка запроса, проверка ответа, журнал."""
    chat = gateway.Chat(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                        temperature=temperature, max_tokens=max_tokens)
    while True:
        stages = {stage["step"]: stage for stage in gateway.process(chat, "loop", mode, who)}
        if stages["done"]["status"] != 429:
            return stages
        wait = int(stages["done"]["headers"]["Retry-After"])
        _events.append({"step": "wait", "seconds": wait})
        time.sleep(wait)


def _llm_step(step, attempt, stages):
    """Шаг с моделью: что поймал шлюз, что ушло в модель, что вернулось. slipped — приманки, которых шлюз не узнал."""
    done, entry, out = stages["done"], stages["input"], stages.get("output", {})
    hits = [h for m in entry.get("messages", []) for h in m["hits"]]
    sent = entry.get("sent") or []
    text = "\n".join(m["content"] for m in sent)
    request = "\n".join(m["masked"] for m in entry.get("messages", []) if m["role"] == "user")
    return {"step": step, "try": attempt, "status": done["status"], "action": entry.get("action"), "request": request,
            "error": done["body"]["error"]["message"] if done["status"] != 200 else None,
            "input": [{"name": h["name"], "preview": h["preview"]} for h in hits],
            "output": [{"name": h["name"], "preview": h["preview"]} for h in out.get("hits", [])],
            "slipped": [name for name, value in BAITS.items() if value in text],
            "seconds": out.get("seconds"), "sent": sent, "raw": out.get("raw"), "shown": out.get("shown")}


def _coder_request(task, context, code, failure, notes):
    """Первая попытка — задача и файл проекта. Дальше — ещё прошлая версия и что в ней исправить."""
    text = f"Задача: {task['text']}\n{task['spec']}\n\n--- {task['context']} ---\n{context}"
    if code:
        text += f"\n\nПрошлая версия:\n```python\n{code}```"
    if notes:
        text += "\n\n" + "\n".join(notes)
    if failure:
        text += f"\n\nИсправь: проверка не прошла.\n{failure}"
    return text


def extract_code(answer):
    found = re.search(r"```(?:python|py)?\n(.*?)(?:```|\Z)", answer, re.S)
    return (found.group(1) if found else answer).strip() + "\n"


def run_checks(task, code):
    """Линтер и тесты: (прошло, что показать)."""
    if stubs := [f"строка {i}: {line.strip()}" for i, line in enumerate(code.splitlines(), 1) if "[REDACTED_" in line]:
        return False, "\n".join(["Убери из кода заглушки шлюза.", "Значений секретов ты не знаешь: импортируй или читай из os.environ.", *stubs])
    _reset()
    file = f"{task['module']}.py"
    (REPO / file).write_text(code, "utf-8")
    failed, lint = _run(sys.executable, "-m", "ruff", "check", "--no-cache", "--select", "E9,F", "--ignore", "F401,F841",
                        "--output-format", "concise", file)
    if failed:
        return False, lint.replace("F821 Undefined name", "нет import или переменной:").strip()
    failed, tests = _run(sys.executable, "-m", "pytest", "-q", "--tb=short", "--no-header", "-p", "no:cacheprovider",
                         f"tests/test_{task['module']}.py")
    tests = tests.replace(f"{REPO}{os.sep}", "")
    tests = FOREIGN_FRAME.sub("", NOISE.sub("", tests))
    tests = PATH.sub("<путь>", tests).strip()  # фидбек уходит в модель: имя пользователя в пути ей ни к чему
    if failed:
        return False, tests[-1000:]
    _, status = _git("status", "--porcelain")
    if spoiled := [row[3:] for row in status.splitlines() if not row.startswith("??") and row[3:] != file]:
        return False, f"Код изменил файлы проекта: {', '.join(spoiled)}. Их не трогай."
    return True, tests


def parse_review(text, asked):
    """Ответы модели на чек-лист: к каждому заданному пункту — да, нет или None, если она промолчала."""
    answers = {int(number): (word.lower(), rest) for number, word, rest in ANSWER.findall(text)}
    rows = []
    for number, rule in enumerate(asked, 1):
        word, rest = answers.get(number, (None, ""))
        line = re.search(r"\d+", rest) if word == "да" else None
        rows.append({**rule, "answer": word, "line": int(line.group()) if line else None})
    return rows


def findings_of(rows):
    """Находки. None — проверки не было. Вернуть код хватит одного серьёзного «да», для коммита нужны все ответы."""
    found = [{key: row[key] for key in ("level", "problem", "line")} for row in rows if row["answer"] == "да"]
    complete = all(row["answer"] for row in rows)
    return found if complete or any(f["level"] in RETURN_ON for f in found) else None


def review(code, mode, attempt):
    """Второй вызов модели, со своим промптом. Отдаёт попытки; в последней — находки или None, если проверки не было."""
    asked = [rule for rule in RULES if re.search(rule.get("when", ""), code)]
    checklist = "\n".join(f"{i}. {rule['ask']}" for i, rule in enumerate(asked, 1))
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(code.splitlines(), 1))
    system, user = f"{SECURITY_INTRO}\n\n{checklist}", f"Код:\n{numbered}\n\n{REVIEW_FORMAT.format(n=len(asked))}"
    for _ in range(REVIEW_TRIES):
        step = _llm_step("review", attempt, ask(system, user, mode, "review", REVIEW_TOKENS, 0.1))
        rows = parse_review(step["shown"] or "", asked)
        step |= {"code": code, "checklist": rows, "findings": findings_of(rows) if step["status"] == 200 else None}
        yield step
        if step["findings"] is not None or step["status"] != 200:
            return


def _note(finding):
    """Фидбек для генерации: «Исправь: SQL injection в строке 42»."""
    place = f" в строке {finding['line']}" if finding["line"] else ""
    fix = next(rule["fix"] for rule in RULES if rule["problem"] == finding["problem"])
    return f"Исправь: {finding['problem']}{place}. Как: {fix}."


def audit(task, warnings):
    """Контроль после коммита: ищет дыры в готовом коде. Ни цикл, ни модель его не видят.

    Дыра, о которой review предупредил как о Medium или Low, — не «мимо обоих»: её пропустили осознанно.
    """
    _reset()
    _, out = _run(sys.executable, "-m", "pytest", "-q", "--tb=no", "-rA", "-p", "no:cacheprovider",
                  f"audit/audit_{task['module']}.py")
    rows = re.findall(r"^(PASSED|FAILED|ERROR) \S*?::test_(\S+)", out, re.M)
    found = [name.replace("_", " ") for status, name in rows if status != "PASSED"]
    said = {f["problem"] for f in warnings}
    return {"ran": len(rows), "warned": [hole for hole in found if hole in said], "holes": [hole for hole in found if hole not in said]}


def run_task(task, mode):
    """Цикл одной задачи. Отдаёт шаги по мере готовности; последний — summary, итог для таблицы."""
    _prepare_repo()
    context = (PROJECT / task["context"]).read_text("utf-8")
    code, failure, notes = "", "", []
    steps, result = [], {"result": "no_fix"}

    for attempt in range(1, MAX_TRIES + 1):
        request = _coder_request(task, context, code, failure, notes)
        # С каждой попыткой смелее: на тот же фидбек при низкой температуре модель пишет тот же код
        step = _llm_step("generate", attempt, ask(CODER_PROMPT, request, mode, "генерация", CODE_TOKENS, 0.2 * attempt))
        step["code"] = extract_code(step["shown"]) if step["status"] == 200 else ""
        steps.append(step)
        yield step
        if step["status"] != 200:
            result = {"result": "blocked" if step["status"] == 400 else "error", "reason": step["error"]}
            break
        if step["shown"] == guards.HIDDEN:
            continue  # шлюз скрыл ответ: кода нет, просим заново
        code = step["code"]

        ok, failure = run_checks(task, code)
        steps.append({"step": "tests", "try": attempt, "ok": ok, "details": failure})
        yield steps[-1]
        if not ok:
            continue
        failure = ""

        for step in review(code, mode, attempt):
            steps.append(step)
            yield step
        if step["findings"] is None:
            reason = step["error"] or "Шлюз скрыл ответ review или его не разобрать: проверки не было, коммита нет."
            result = {"result": "no_review", "reason": reason}
            break
        if blocking := [f for f in step["findings"] if f["level"] in RETURN_ON]:
            notes += [note for note in map(_note, blocking) if note not in notes]
            continue

        warnings = step["findings"]
        _reset()
        (REPO / f"{task['module']}.py").write_text(code, "utf-8")
        _git("add", f"{task['module']}.py")
        message = [f"{f['level']}: {f['problem']}" for f in warnings]
        _git("commit", "--allow-empty", "-qm", task["text"], *(["-m", "\n".join(message)] if message else []))
        commit = _git("rev-parse", "--short", "HEAD")[1].strip()
        steps.append({"step": "commit", "try": attempt, "hash": commit, "warnings": warnings, "code": code})
        yield steps[-1]
        checked = audit(task, warnings)
        steps.append({"step": "audit", "try": attempt, **checked})
        yield steps[-1]
        result = {"result": "commit", "hash": commit, **checked}
        break

    yield {"step": "summary", "task": task["id"], "mode": mode, **_summary(steps), **result}


def _summary(steps):
    """Что поймал шлюз, что поймал review, какие приманки дошли до модели. Дыры в коде добавляет контроль."""
    caught, findings, slipped = {}, {}, {}
    for step in steps:
        for hit in step.get("input", []) + step.get("output", []):
            caught[hit["name"], hit["preview"]] = hit
        for finding in step.get("findings") or []:
            findings[finding["level"], finding["problem"]] = finding
        slipped |= dict.fromkeys(step.get("slipped", []))
    return {"tries": max((step["try"] for step in steps), default=0), "gateway": list(caught.values()),
            "reviewed": any(step["step"] == "review" for step in steps), "review": list(findings.values()),
            "slipped": list(slipped)}


def _log(task_id, step):
    """Журнал цикла. Текстов запросов в нём нет: что шлюз не узнал, то из них попало бы и в журнал."""
    DATA.mkdir(parents=True, exist_ok=True)
    row = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "task": task_id,
           **{k: v for k, v in step.items() if k not in ("sent", "raw", "shown", "code", "task", "request", "checklist")}}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _worker(tasks, mode):
    try:
        for task in tasks:
            _events.append({"step": "start", "task": task["id"], "mode": mode})
            for step in run_task(task, mode):
                _log(task["id"], step)
                _events.append({**step, "task": task["id"]})
    except Exception as e:  # noqa: BLE001 — показать на странице, почему цикл остановился
        _events.append({"step": "crash", "error": str(e)})
    finally:
        _running.clear()


class Run(BaseModel):
    task: str = "all"
    mode: Literal["block", "mask"] = "mask"


@router.post("/api/loop/run")
def start(req: Run):
    tasks = [t for t in TASKS if req.task in ("all", t["id"])]
    if not tasks:
        raise HTTPException(400, "Нет такой задачи")
    if _running.is_set():
        raise HTTPException(409, "Цикл уже идёт")
    _running.set()
    _events.clear()
    threading.Thread(target=_worker, args=(tasks, req.mode), daemon=True).start()
    return {"running": True}


@router.get("/api/loop/events")
def events():
    return {"running": _running.is_set(), "events": _events}


def summaries():
    """Последний итог по каждой задаче: журнал хранит все прогоны."""
    rows = [json.loads(s) for s in LOG_FILE.read_text("utf-8").splitlines()] if LOG_FILE.exists() else []
    return {row["task"]: row for row in rows if row["step"] == "summary"}


@router.get("/api/loop/state")
def state():
    commits = _git("log", "--oneline", "-5")[1].splitlines() if (REPO / ".git").exists() else []
    tasks = [{**task, "file": (PROJECT / task["context"]).read_text("utf-8")} for task in TASKS]
    return {"model": MODEL, "tasks": tasks, "baits": BAITS, "max_tries": MAX_TRIES, "summaries": summaries(), "commits": commits}


@router.get("/loop")
def page():
    return FileResponse(ROOT / "static" / "loop.html")
