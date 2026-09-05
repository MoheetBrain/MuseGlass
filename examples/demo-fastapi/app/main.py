from fastapi import FastAPI

from app import __version__

app = FastAPI(title="MuseGlass demo service", version=__version__)

GREETINGS = {"en": "hello", "fr": "bonjour", "de": "hallo"}


@app.get("/")
def root() -> dict:
    return {"service": "demo", "message": GREETINGS["en"]}


@app.get("/greet/{lang}")
def greet(lang: str) -> dict:
    return {"message": GREETINGS.get(lang, GREETINGS["en"]), "lang": lang if lang in GREETINGS else "en"}


@app.get("/items/{item_id}")
def read_item(item_id: int) -> dict:
    return {"item_id": item_id, "name": f"item-{item_id}"}
