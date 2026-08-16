"""
Resumable File Upload Service  v5.0 — with Azure Blob Storage + Admin dashboard
"""
import asyncio, os, uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy import text

import logger as _log_setup
from database import Base, SessionLocal, engine
from limiter_config import limiter
from logger import get_logger
from routes.admin import router as admin_router
from routes.auth import router as auth_router
from routes.uploads import router as upload_router
from workers.cleanup import start_cleanup_worker

log = get_logger(__name__)

ALLOWED_ORIGINS = [
    o.strip() for o in
    os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")
    if o.strip()
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", version=app.version,
             storage="azure" if os.getenv("AZURE_STORAGE_CONNECTION_STRING") else "local")
    Base.metadata.create_all(bind=engine)
    cleanup_task = asyncio.create_task(start_cleanup_worker())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    log.info("shutdown")

app = FastAPI(
    title="Resumable File Upload Service",
    description="Chunk-based uploads · Azure Blob · JWT auth · Rate limiting · Admin dashboard",
    version="5.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    import time
    rid = uuid.uuid4().hex[:8]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=rid)
    start    = time.perf_counter()
    response = await call_next(request)
    ms       = round((time.perf_counter() - start) * 1000, 1)
    log.info("request", method=request.method, path=request.url.path,
             status=response.status_code, duration_ms=ms, ip=get_remote_address(request))
    response.headers["X-Request-ID"] = rid
    return response

@app.get("/health", tags=["ops"])
async def health_check():
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        storage_backend = "azure" if os.getenv("AZURE_STORAGE_CONNECTION_STRING") else "local"
        return {"status": "healthy", "version": app.version, "storage": storage_backend}
    except Exception as exc:
        log.error("health_check_failed", error=str(exc))
        return JSONResponse(503, {"status": "unhealthy", "detail": str(exc)})

app.include_router(auth_router,   prefix="/api", tags=["auth"])
app.include_router(upload_router, prefix="/api", tags=["uploads"])
app.include_router(admin_router,  prefix="/api", tags=["admin"])

if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
