def test_токен_переживает_перезапуск(load):
    load("auth_store").save_token("tok-12345")
    assert load("auth_store").load_token() == "tok-12345"
