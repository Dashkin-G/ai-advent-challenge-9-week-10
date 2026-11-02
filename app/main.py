"""Взлом промпта: чат с ботом банка, атаки на него и итог «атака × промпт»."""
import json
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import attacks, llm, prompts

ROOT = Path(__file__).resolve().parent.parent
BOARD_FILE = Path(os.getenv("DATA_DIR", ROOT / "data")) / "board.json"
TRIES = 5  # попыток на клетку: ответ модели случаен, а атакующему хватит одной удачной

app = FastAPI()
_running = threading.Event()


class Ask(BaseModel):
    text: str
    prompt: str = "plain"


def line(**data):
    return json.dumps(data, ensure_ascii=False) + "\n"


@app.get("/")
def page():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def state():
    return {"model": llm.MODEL, "prompts": prompts.PROMPTS, "secret": prompts.SECRET, "attacks": attacks.ATTACKS,
            "control": attacks.CONTROL, "kinds": attacks.KINDS, "collection": attacks.COLLECTION, "board": board()}


@app.post("/api/ask")
def ask(req: Ask):
    """Ответ бота потоком строк JSON: что получила модель → кусочки ответа → вердикт."""
    text = req.text.strip()
    if req.prompt not in prompts.PROMPTS:
        raise HTTPException(400, "Нет такого промпта")
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    parts = llm.context(prompts.PROMPTS[req.prompt], text)
    attack_id = attacks.CONTROL["id"] if text == attacks.CONTROL["text"] else None

    def stream():
        yield line(parts=parts)
        answer, start = "", time.time()
        try:
            for piece in llm.answer(parts):
                answer += piece
                yield line(text=piece)
        except Exception as e:  # noqa: BLE001 — показать на странице, почему модель не ответила
            yield line(error=f"Модель не ответила: {e}")
            return
        yield line(done=True, verdict=attacks.verdict(answer, attack_id), seconds=round(time.time() - start, 1))

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/board")
def board():
    """Ответы прогона; взломан ли бот, считается при чтении — поправили улики, итог пересчитан без модели."""
    data = json.loads(BOARD_FILE.read_text("utf-8")) if BOARD_FILE.exists() else {}
    for key, tries in data.get("cells", {}).items():
        for t in tries:
            t["verdict"] = attacks.verdict(t["text"], key.split("/")[0])
    return {**data, "running": _running.is_set()}


@app.post("/api/board")
def run_board():
    if _running.is_set():
        raise HTTPException(409, "Прогон уже идёт")
    _running.set()
    threading.Thread(target=_run, daemon=True).start()
    return board()


def _run():
    """Каждая атака и обычный вопрос × оба промпта × TRIES попыток; клетки пишутся на диск по мере ответов."""
    data = {"cells": {}, "tries": TRIES, "model": llm.MODEL, "when": time.strftime("%d.%m.%Y %H:%M")}
    start = time.time()
    try:
        for attack in attacks.ATTACKS + [attacks.CONTROL]:
            for name, system in prompts.PROMPTS.items():
                for _ in range(TRIES):
                    text = "".join(llm.answer(llm.context(system, attack["text"])))
                    data["cells"].setdefault(f"{attack['id']}/{name}", []).append({"text": text})
                    data["seconds"] = round(time.time() - start)
                    _save(data)
    except Exception as e:  # noqa: BLE001 — ошибка видна на вкладке «Итог»
        data["error"] = f"Модель не ответила: {e}"
        _save(data)
    finally:
        _running.clear()


def _save(data):
    BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOARD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(BOARD_FILE)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
