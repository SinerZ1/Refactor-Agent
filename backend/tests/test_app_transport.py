from app import is_websocket_origin_allowed


def test_websocket_accepts_dynamic_same_origin_loopback_port():
    assert is_websocket_origin_allowed("http://127.0.0.1:54321", "127.0.0.1:54321")
    assert is_websocket_origin_allowed("http://localhost:49152", "localhost:49152")


def test_websocket_rejects_cross_site_origin_even_with_same_port():
    assert not is_websocket_origin_allowed(
        "https://attacker.example:54321", "127.0.0.1:54321"
    )
    assert not is_websocket_origin_allowed("http://127.0.0.1:54322", "127.0.0.1:54321")
