"""server.server

Forge ADK server: FastAPI WebSocket endpoint + Gemini Live (BIDI audio). Reuse Wand app/server.py. Built in plan.md Step 5.

Right now this is only a **Step 0b smoke placeholder**: a minimal FastAPI app with a
`/health` route so `uvicorn server.server:app` boots and the environment can be verified
end to end. The real WebSocket + Gemini Live wiring replaces this in Step 5.
"""

from fastapi import FastAPI

app = FastAPI(title="Forge (scaffold)", version="0.1.0")


@app.get("/health")
def health() -> dict:
    # Placeholder until Step 5 wires the /ws endpoint + Gemini Live runner.
    return {"status": "ok", "stage": "scaffold", "note": "Step 5 adds /ws + Gemini Live"}
