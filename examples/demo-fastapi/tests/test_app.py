from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_says_hello():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"service": "demo", "message": "hello"}


def test_greet_known_language():
    assert client.get("/greet/fr").json() == {"message": "bonjour", "lang": "fr"}


def test_greet_unknown_language_falls_back_to_english():
    assert client.get("/greet/xx").json() == {"message": "hello", "lang": "en"}


def test_read_item():
    assert client.get("/items/7").json() == {"item_id": 7, "name": "item-7"}


def test_read_item_rejects_non_integer():
    assert client.get("/items/seven").status_code == 422
