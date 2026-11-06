import logging
from contextlib import asynccontextmanager
from threading import Event, Thread

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.context import Context
from common.errors import DomainError
from common.http import HTTPGateway
from common.messaging import BrokerWorker
from common.settings import Settings
from common.store import SQLStore

log = logging.getLogger(__name__)


def make_app(name, Service, make_router, context=None, background=True):
    # Production adapters are initialized at startup, not import/OpenAPI generation.
    state = {"ctx": context, "service": Service(context) if context is not None else None, "worker": None}
    stop = Event()

    class LazyService:
        def __getattr__(self, attr):
            if state["service"] is None:
                raise RuntimeError("Application lifespan has not started")
            return getattr(state["service"], attr)

    def timers():
        while not stop.wait(state["ctx"].settings.timer_interval):
            try:
                tick = getattr(state["service"], "tick", None)
                if tick:
                    tick()
            except Exception:
                log.exception("Timer failed in %s", name)

    @asynccontextmanager
    async def lifespan(app):
        stop.clear()
        if state["ctx"] is None:
            settings = Settings()
            database_url = settings.database_url.replace("{service}", name)
            ctx = Context(name, SQLStore(name, database_url), HTTPGateway(settings), settings=settings)
            state.update(ctx=ctx, service=Service(ctx))
        ctx = state["ctx"]
        if ctx.settings is None:
            ctx.settings = Settings()
        app.state.context = ctx
        app.state.service = state["service"]
        timer = None
        if background:
            state["worker"] = BrokerWorker(ctx, state["service"])
            state["worker"].start()
            timer = Thread(target=timers, name=f"timer-{name}", daemon=True)
            timer.start()
        try:
            yield
        finally:
            stop.set()
            if timer:
                timer.join(timeout=10)
            if state["worker"]:
                state["worker"].close()
            if context is None:
                ctx.http.close()
                ctx.store.close()

    app = FastAPI(title=f"Billiards · {name}", version="1.0.0", lifespan=lifespan)
    app.state.context = context
    app.state.service = state["service"]

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.get("/health", tags=["technical"], include_in_schema=False)
    def health():
        return {"service": name, "status": "ok", "broker_connected": bool(state["worker"] and state["worker"].connected)}

    app.include_router(make_router(LazyService()))
    return app
