"""Контроль: ни модель, ни review его не видят. Тест назван по дыре: упал — значит, дыра есть."""
import logging

import pytest

HEADERS = {"Authorization": "Bearer tok-AUDIT-1"}
BODY = {"email": "anna@mail.example", "phone": "+7 999 765-43-21", "passport": "0000 654321", "password": "pw-AUDIT-2"}


@pytest.fixture
def logged(load, caplog, capsys, tmp_path):
    """Всё, что код вывел: в логгер, на экран и в файлы."""
    caplog.set_level(logging.DEBUG)
    logging.getLogger("requests").addHandler(caplog.handler)
    load("request_log").log_request("POST", "/v1/login", dict(HEADERS), dict(BODY))
    out = capsys.readouterr()
    files = [f.read_text("utf-8", "ignore") for f in tmp_path.rglob("*") if f.is_file()]
    return "\n".join([caplog.text, out.out, out.err, *files])


def test_токен_в_логе(logged):
    assert "tok-AUDIT-1" not in logged


def test_пароль_в_логе(logged):
    assert BODY["password"] not in logged


def test_почта_в_логе(logged):
    assert BODY["email"] not in logged


def test_телефон_в_логе(logged):
    assert BODY["phone"] not in logged


def test_паспорт_в_логе(logged):
    assert BODY["passport"] not in logged
