"""Minimal diagnostic endpoint — no vLLM, no model, no nginx, no GPU.

Purpose: bisect the Nebius endpoint failure. If THIS reaches RUNNING, the
platform / create-flow / networking work, and the fault is in the heavy
components of the real image. If this also hangs in STARTING, the fault is
the create operation itself (params/env/quota/platform).
"""
from fastapi import FastAPI

app = FastAPI(title="min-test", version="0.0.1")


@app.get("/")
def root():
    return {"ok": True, "service": "min-test"}


@app.get("/health")
def health():
    return {"status": "healthy"}
