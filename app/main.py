from fastapi import FastAPI

from app.auth import router as auth_router
from app.jobs import router as jobs_router

app = FastAPI(title="SARGuardian Backend", version="0.1.0")
app.include_router(auth_router)
app.include_router(jobs_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sarguardian-backend"}
