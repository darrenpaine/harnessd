"""Manage FastAPI and endpoints."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
