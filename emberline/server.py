"""EMBERLINE — Ashes of the Ninth Sun. Save server + static host.

Run:  .venv/bin/python emberline/server.py   (from the repo root)
Then open http://127.0.0.1:7777
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SAVES = Path(os.environ.get("EMBERLINE_SAVE_DIR", ROOT / "saves"))
SAVES.mkdir(parents=True, exist_ok=True)

SLOT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_SAVE_BYTES = 2_000_000

app = FastAPI(title="Emberline", docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_stale_static(request: Request, call_next):
    response = await call_next(request)
    # Revalidate on every load so local edits show up on refresh (ETag keeps it cheap).
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


def _slot_path(slot: str) -> Path:
    if not SLOT_RE.match(slot):
        raise HTTPException(status_code=400, detail="bad slot id")
    return SAVES / f"{slot}.json"


@app.get("/api/health")
async def health():
    return {"ok": True, "game": "emberline"}


@app.get("/api/saves")
async def list_saves():
    out = []
    for p in sorted(SAVES.glob("*.json")):
        try:
            data = json.loads(p.read_text("utf-8"))
            s = data.get("summary", {})
            out.append(
                {
                    "slot": p.stem,
                    "name": s.get("name", "Unknown"),
                    "generation": s.get("generation", 1),
                    "realm": s.get("realm", "Mortal"),
                    "year": s.get("year", 1),
                    "updatedAt": data.get("updatedAt", 0),
                }
            )
        except Exception:
            continue
    out.sort(key=lambda x: -x["updatedAt"])
    return out


@app.get("/api/saves/{slot}")
async def load_save(slot: str):
    p = _slot_path(slot)
    if not p.exists():
        raise HTTPException(status_code=404, detail="no such save")
    return JSONResponse(json.loads(p.read_text("utf-8")))


@app.put("/api/saves/{slot}")
async def write_save(slot: str, request: Request):
    p = _slot_path(slot)
    body = await request.body()
    if len(body) > MAX_SAVE_BYTES:
        raise HTTPException(status_code=413, detail="save too large")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    data["updatedAt"] = int(time.time())
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), "utf-8")
    tmp.replace(p)
    return {"ok": True, "slot": slot, "updatedAt": data["updatedAt"]}


@app.delete("/api/saves/{slot}")
async def delete_save(slot: str):
    p = _slot_path(slot)
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.get("/api/ghosts/{slot}")
async def ghosts(slot: str):
    """Other hearthlines on this server, seen as distant names on the Roll."""
    out = []
    for p in sorted(SAVES.glob("*.json")):
        if p.stem == slot:
            continue
        try:
            data = json.loads(p.read_text("utf-8"))
            st = data.get("state", {})
            chr_ = st.get("chr", {})
            out.append(
                {
                    "name": chr_.get("name", "A stranger"),
                    "realm": int(chr_.get("realm", 0)),
                    "generation": st.get("meta", {}).get("generation", 1),
                }
            )
        except Exception:
            continue
    return out[:6]


@app.get("/api/content")
async def content():
    """Optional content packs: emberline/content/*.json arrays of extra events."""
    merged = {"events": []}
    cdir = ROOT / "content"
    if cdir.is_dir():
        for p in sorted(cdir.glob("*.json")):
            try:
                pack = json.loads(p.read_text("utf-8"))
                merged["events"].extend(pack.get("events", []))
            except Exception:
                continue
    return merged


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("EMBERLINE_PORT", 7777)))
