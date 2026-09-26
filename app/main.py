from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import router as auth_router
from app.jobs import router as jobs_router
from app.worker_callback import router as worker_callback_router


class RawBodyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        body = await request.body()
        request.state.raw_body = body
        return await call_next(request)


app = FastAPI(title="SARGuardian Backend", version="0.1.0")
app.add_middleware(RawBodyMiddleware)
app.include_router(auth_router)
app.include_router(jobs_router)
app.include_router(worker_callback_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sarguardian-backend"}
