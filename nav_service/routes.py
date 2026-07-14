import asyncio
import logging

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.metrics import collector_registry
from sync_grafana_dashboards import sync_dashboards
from . import state
from .schemas import DividendRequest, SubscriptionRequest


LOGGER = logging.getLogger(__name__)


def register_routes(app, templates):
    @app.post("/subscribe")
    async def subscribe(req: SubscriptionRequest):
        if req.account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        account = state.accounts[req.account_name]
        try:
            await account.handle_subscription_pro(req.subscription_date, req.subscription_amount)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"申购失败: {exc}")
        return state.account_response(req.account_name, "申购", req.subscription_amount, req.subscription_date)

    @app.post("/subscribe_form/{account_name}")
    async def subscribe_form(account_name: str, subscription_date: str = Form(...), subscription_amount: float = Form(...)):
        if account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        account = state.accounts[account_name]
        try:
            await account.handle_subscription_pro(subscription_date, subscription_amount)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"申购失败: {exc}")
        return state.account_response(account_name, "申购", subscription_amount, subscription_date)

    @app.post("/dividend")
    async def dividend(req: DividendRequest):
        if req.account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        account = state.accounts[req.account_name]
        try:
            await account.handle_dividend_pro(req.dividend_date, req.dividend_amount)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"分红失败: {exc}")
        return state.account_response(req.account_name, "分红", req.dividend_amount, req.dividend_date)

    @app.post("/dividend_form/{account_name}")
    async def dividend_form(
        account_name: str,
        dividend_date: str = Form(...),
        dividend_amount: float = Form(...),
    ):
        if account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        account = state.accounts[account_name]
        try:
            await account.handle_dividend_pro(dividend_date, dividend_amount)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"分红失败: {exc}")
        return state.account_response(account_name, "分红", dividend_amount, dividend_date)

    @app.get("/")
    async def read_root():
        return Response(generate_latest(collector_registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        return templates.TemplateResponse(
            "accounts.html",
            {
                "request": request,
                "account_groups": state.build_account_table_groups(),
                "account_count": len(state.account_infos),
            },
        )

    @app.get("/api/accounts")
    async def list_accounts():
        return {"accounts": state.build_account_options()}

    @app.get("/accounts/new", response_class=HTMLResponse)
    async def new_account_page(request: Request):
        return templates.TemplateResponse(
            "account_new.html",
            {"request": request, "exchanges": state.build_exchange_options()},
        )

    @app.post("/accounts/new")
    async def create_account(
        product_name: str = Form(...),
        initial_unit: str = Form(...),
        ccy: str = Form(...),
        exchange: str = Form(...),
        account_type: str = Form(...),
        client: str = Form(...),
        interest_rate: str = Form(""),
        api_key: str = Form(...),
        secret_key: str = Form(...),
    ):
        try:
            account_info = state.append_account_config(
                product_name,
                initial_unit,
                ccy,
                exchange,
                account_type,
                client,
                interest_rate,
                api_key,
                secret_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"新增账户失败: {exc}")

        grafana_result = None
        grafana_error = ""
        try:
            grafana_result = await asyncio.to_thread(
                sync_dashboards,
                account_names=[product_name],
            )
        except Exception as exc:
            LOGGER.exception("Grafana Panel creation failed for account %s", product_name)
            grafana_error = str(exc)

        message = f"{product_name} 新增成功"
        if grafana_error:
            message += "，但 Grafana Panel 创建失败，请检查服务日志或手动执行修复脚本"

        return {
            "message": message,
            "account_name": product_name,
            "account_type": account_info["account_type"],
            "initial_unit": account_info["initial_unit"],
            "client": account_info["client"],
            "interest_rate": account_info["interest_rate"],
            "ccy": account_info["ccy"],
            "exchange": account_info["exchange"],
            "grafana": grafana_result,
            "grafana_error": bool(grafana_error),
        }

    @app.get("/operations", response_class=HTMLResponse)
    async def read_operations(request: Request):
        return templates.TemplateResponse("index.html", {"request": request, "accounts": state.build_account_options()})

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(collector_registry), media_type=CONTENT_TYPE_LATEST)


async def update_loop():
    for account_name in state.accounts:
        state.start_account_update_task(account_name)
    await asyncio.gather(*state.account_update_tasks.values())
