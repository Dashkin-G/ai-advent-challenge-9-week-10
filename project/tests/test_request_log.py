import logging


def test_запрос_попадает_в_лог(load, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("requests").addHandler(caplog.handler)
    load("request_log").log_request("POST", "/v1/login", {"Accept": "*/*"}, {"item": 7})
    assert "POST" in caplog.text and "/v1/login" in caplog.text
