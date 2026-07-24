from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import COOKIE_NAME, Session, SessionSigner, credentials_match
from .client import KucoinAPIError, KucoinClient
from .config import AppConfig, PACKAGE_DIR, load_config
from .feishu import FeishuNotifier
from .repository import ExposureRepository
from .scheduler import run_refresh_loop
from .service import ExposureService


LOGGER = logging.getLogger("kucoin_exposure.web")
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def configure_logging():
    log_dir = PACKAGE_DIR / "runtime_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "kucoin_exposure.log"
    root = logging.getLogger("kucoin_exposure")
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    root.addHandler(file_handler)


def create_app(config: AppConfig | None = None) -> FastAPI:
    configure_logging()
    config = config or load_config()
    repository = ExposureRepository(config.database_path)
    client = KucoinClient(config.kucoin, logger=LOGGER)
    notifier = FeishuNotifier(config.feishu)
    service = ExposureService(config, client, repository, notifier)
    signer = SessionSigner(
        config.login.session_secret, config.login.session_days
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        scheduler_task = asyncio.create_task(
            run_refresh_loop(service, config.monitor.interval_seconds),
            name="kucoin-exposure-refresh",
        )
        app.state.service = service
        try:
            yield
        finally:
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
            await service.close()

    app = FastAPI(title="KuCoin Hedge Exposure", lifespan=lifespan)
    app.state.config = config
    app.state.service = service
    app.state.signer = signer

    def session_for(request: Request) -> Session | None:
        return signer.verify(request.cookies.get(COOKIE_NAME))

    def require_session(request: Request) -> Session:
        session = session_for(request)
        if session is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return session

    def require_csrf(request: Request, session: Session):
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or supplied != session.csrf_token:
            raise HTTPException(status_code=403, detail="CSRF校验失败，请刷新页面")

    @app.exception_handler(KucoinAPIError)
    async def kucoin_error_handler(_request: Request, exc: KucoinAPIError):
        return JSONResponse(
            status_code=502,
            content={
                "detail": str(exc),
                "code": exc.code,
                "uncertain": exc.uncertain,
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        LOGGER.exception(
            "Unhandled request error: %s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"内部错误：{type(exc).__name__}: {exc}",
                "code": "internal_error",
            },
        )

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        if session_for(request):
            return RedirectResponse("/kucoin/exposure", status_code=303)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request, error: str = ""):
        if session_for(request):
            return RedirectResponse("/kucoin/exposure", status_code=303)
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": error}
        )

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        if not credentials_match(
            username,
            password,
            config.login.username,
            config.login.password,
        ):
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "用户名或密码错误"},
                status_code=401,
            )
        response = RedirectResponse("/kucoin/exposure", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            signer.issue(username),
            max_age=signer.max_age,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get(
        "/kucoin/exposure",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def exposure_page(request: Request):
        session = session_for(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            "exposure.html",
            {
                "request": request,
                "username": session.username,
                "csrf_token": session.csrf_token,
            },
        )

    @app.get("/api/kucoin/exposure/latest")
    async def latest(request: Request):
        require_session(request)
        return await service.latest()

    @app.post("/api/kucoin/exposure/refresh")
    async def refresh(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        return await service.refresh()

    @app.post("/api/kucoin/exposure/actions/clear")
    async def clear_actions(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        deleted = await repository.clear_trade_actions()
        LOGGER.info(
            "Trade action history cleared by %s; deleted=%s",
            session.username,
            deleted,
        )
        return {"status": "complete", "deleted": deleted}

    @app.get("/api/kucoin/exposure/{asset}/close-preview")
    async def close_preview(asset: str, request: Request):
        require_session(request)
        try:
            return await service.preview_close(asset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/kucoin/exposure/{asset}/close-both")
    async def close_both(asset: str, request: Request):
        session = require_session(request)
        require_csrf(request, session)
        try:
            return await service.close_both_sides(asset, session.username)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/kucoin/exposure/close-all")
    async def close_all(request: Request):
        session = require_session(request)
        require_csrf(request, session)
        try:
            return await service.close_all(session.username)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
