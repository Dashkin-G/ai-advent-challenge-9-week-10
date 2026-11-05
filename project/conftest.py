"""Код модели запускается на этой машине, поэтому в тестах нет сети, а домашняя папка и рабочая — временные."""
import importlib
import io
import json
import socket
import sys
import urllib.request

import pytest
from cryptography.fernet import Fernet

from settings import API_TOKEN, API_URL


def _no_network(*args, **kwargs):
    raise OSError("В тестах сети нет")


socket.socket.connect = _no_network
socket.create_connection = _no_network


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("APP_CIPHER", Fernet.generate_key().decode())
    monkeypatch.setenv("API_URL", API_URL)
    monkeypatch.setenv("API_TOKEN", API_TOKEN)


@pytest.fixture
def load():
    """Импорт заново — как новый запуск программы."""
    def fresh(name):
        sys.modules.pop(name, None)
        return importlib.import_module(name)
    return fresh


@pytest.fixture
def server(monkeypatch):
    """Подставной API: запоминает запросы и отвечает JSON."""
    calls = []

    def urlopen(url, data=None, timeout=None, context=None, **kwargs):
        is_request = isinstance(url, urllib.request.Request)
        calls.append({"url": url.full_url if is_request else url, "timeout": timeout, "context": context,
                      "auth": (url.get_header("Authorization") or "") if is_request else ""})
        response = io.BytesIO(json.dumps({"id": 42, "name": "Анна"}).encode())
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls
