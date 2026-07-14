import asyncio
import logging

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.metrics import collector_registry
from sync_grafana_dashboards import sync_dashboards
from . import state
from .schemas import (
    DividendRequest,
    InterestDeductionRequest,
    SubscriptionRequest,
    WithdrawalRequest,
)


LOGGER = logging.getLogger(__name__)


def register_routes(app, templates):
    @app.post("/subscribe")
    async def subscribe(req: SubscriptionRequest):
        if req.account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        account = state.accounts[req.account_name]
        try:
            await account.handle_subscription_pro(req.subscription_date, req.subscription_amount)
            state.adjust_principal(req.account_name, req.subscription_amount)
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
            state.adjust_principal(account_name, subscription_amount)
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

    @app.post("/interest_deduction")
    async def interest_deduction(req: InterestDeductionRequest):
        if req.account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            await state.accounts[req.account_name].handle_interest_deduction_pro(
                req.deduction_date, req.deduction_amount
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"扣息失败: {exc}")
        return state.account_response(req.account_name, "扣息", req.deduction_amount, req.deduction_date)

    @app.post("/interest_deduction_form/{account_name}")
    async def interest_deduction_form(
        account_name: str,
        deduction_date: str = Form(...),
        deduction_amount: float = Form(...),
    ):
        if account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            await state.accounts[account_name].handle_interest_deduction_pro(
                deduction_date, deduction_amount
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"扣息失败: {exc}")
        return state.account_response(account_name, "扣息", deduction_amount, deduction_date)

    @app.post("/withdrawal")
    async def withdrawal(req: WithdrawalRequest):
        if req.account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            account_info = state.account_infos[req.account_name]
            current_principal = float(account_info.get("principal", account_info["initial_unit"]))
            if req.withdrawal_amount >= current_principal:
                raise ValueError("赎回后的本金必须大于 0")
            await state.accounts[req.account_name].handle_withdrawal_pro(
                req.withdrawal_date, req.withdrawal_amount
            )
            state.deduct_principal(req.account_name, req.withdrawal_amount)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"赎回失败: {exc}")
        return state.account_response(req.account_name, "赎回", req.withdrawal_amount, req.withdrawal_date)

    @app.post("/withdrawal_form/{account_name}")
    async def withdrawal_form(
        account_name: str,
        withdrawal_date: str = Form(...),
        withdrawal_amount: float = Form(...),
    ):
        if account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            account_info = state.account_infos[account_name]
            current_principal = float(account_info.get("principal", account_info["initial_unit"]))
            if withdrawal_amount >= current_principal:
                raise ValueError("赎回后的本金必须大于 0")
            await state.accounts[account_name].handle_withdrawal_pro(
                withdrawal_date, withdrawal_amount
            )
            state.deduct_principal(account_name, withdrawal_amount)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"赎回失败: {exc}")
        return state.account_response(account_name, "赎回", withdrawal_amount, withdrawal_date)

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

        account_type_label = "普通账户" if account_info["account_type"] == "account" else "Pro 账户"
        initial_unit_text = f'{float(account_info["initial_unit"]):g}'
        interest_rate_text = f'{float(account_info["interest_rate"]):g}'
        message = (
            f"{account_type_label} {product_name} 新增成功，"
            f"数量 {initial_unit_text} {account_info['ccy']}，"
            f"客户：{account_info['client']}，利率：{interest_rate_text}"
        )
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
