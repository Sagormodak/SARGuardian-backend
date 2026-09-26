from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import router as auth_router
from app.jobs import router as jobs_router
from app.worker_callback import CallbackError, router as worker_callback_router


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


@app.exception_handler(CallbackError)
async def handle_callback_error(_request: Request, exc: CallbackError) -> JSONResponse:
    """Return only stable, safe callback errors to the worker."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.safe_message}},
    )


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sarguardian-backend"}
