from settings import API_TOKEN


def test_запрос_уходит_с_токеном_на_нужный_адрес(load, server):
    assert load("api_client").get_user(42) == {"id": 42, "name": "Анна"}
    assert server[0]["url"].endswith("/users/42")
    assert server[0]["auth"] == f"Bearer {API_TOKEN}"
