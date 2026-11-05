"""Контроль: ни модель, ни review его не видят. Тест назван по дыре: упал — значит, дыра есть."""
import ssl


def test_запрос_по_http(load, server):
    load("api_client").get_user(42)
    assert server[0]["url"].startswith("https://")


def test_проверка_сертификата_выключена(load, server):
    load("api_client").get_user(42)
    context = server[0]["context"]
    assert context is None or context.verify_mode != ssl.CERT_NONE


def test_нет_таймаута(load, server):
    load("api_client").get_user(42)
    assert server[0]["timeout"]


def test_нет_проверки_ввода(load, server):
    """Чужой id не должен менять адрес запроса."""
    try:
        load("api_client").get_user("../admin")
    except Exception:  # noqa: BLE001 — отказ и есть проверка
        return
    assert "../" not in server[0]["url"]
