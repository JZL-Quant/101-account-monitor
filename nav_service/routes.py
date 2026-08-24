import asyncio
import logging
import os

from fastapi import Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.background import BackgroundTask

from core.metrics import collector_registry
from core.runtime_logging import (
    available_runtime_log_dates,
    build_runtime_log_archive,
    iter_runtime_log_file_date,
    runtime_log_files_for_date,
)
from ops.sync_grafana_dashboards import sync_dashboards
from . import state
from .schemas import (
    DividendRequest,
    FundChangesRequest,
    InterestDeductionRequest,
    SubscriptionRequest,
    WithdrawalRequest,
)


LOGGER = logging.getLogger(__name__)


def register_routes(app, templates):
    # 已停用的旧版单项资金事件 API，保留源码仅用于历史参考。
    # 当前方案统一使用 POST /api/accounts/{account_name}/equity-changes，以明确的
    # 快照时间批量保存申购、分红、除息和赎回。
    '''
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
            if req.withdrawal_amount > current_principal:
                raise ValueError("赎回金额不能超过当前本金")
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
            if withdrawal_amount > current_principal:
                raise ValueError("赎回金额不能超过当前本金")
            await state.accounts[account_name].handle_withdrawal_pro(
                withdrawal_date, withdrawal_amount
            )
            state.deduct_principal(account_name, withdrawal_amount)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"赎回失败: {exc}")
        return state.account_response(account_name, "赎回", withdrawal_amount, withdrawal_date)
    '''

    @app.get("/")
    async def read_root(request: Request):
        return templates.TemplateResponse("home.html", {"request": request})

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        log_dates = await asyncio.to_thread(available_runtime_log_dates)
        return templates.TemplateResponse(
            "accounts.html",
            {
                "request": request,
                "account_groups": state.build_account_table_groups(),
                "account_count": len(state.account_infos) + len(state.archived_account_infos),
                "active_account_count": len(state.account_infos),
                "archived_account_count": len(state.archived_account_infos),
                "log_dates": log_dates,
            },
        )

    @app.get("/accounts/logs/download")
    async def download_runtime_log(log_date: str):
        available_dates = await asyncio.to_thread(available_runtime_log_dates)
        if log_date not in available_dates:
            raise HTTPException(status_code=404, detail="所选日期没有可下载的日志")
        log_files = await asyncio.to_thread(runtime_log_files_for_date, log_date)
        if not log_files:
            raise HTTPException(status_code=404, detail="所选日期没有可下载的日志")
        if len(log_files) > 1:
            archive_path = await asyncio.to_thread(
                build_runtime_log_archive,
                log_files,
                log_date,
            )
            return FileResponse(
                archive_path,
                media_type="application/zip",
                filename=f"runtime-{log_date}.zip",
                background=BackgroundTask(os.unlink, archive_path),
            )
        return StreamingResponse(
            iter_runtime_log_file_date(log_files[0], log_date),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="runtime-{log_date}.log"'
            },
        )

    @app.get("/accounts/{account_name}/snapshot")
    async def download_account_snapshot(account_name: str):
        account_info = state.account_infos.get(account_name)
        archived = False
        if account_info is None:
            account_info = state.archived_account_infos.get(account_name)
            archived = account_info is not None
        if account_info is None:
            raise HTTPException(status_code=404, detail="账户不存在")

        snapshot_file = account_info.get("snapshot_file") if archived else account_info["minute_snapshot_file"]
        if not os.path.isfile(snapshot_file):
            raise HTTPException(status_code=404, detail="该账户的快照 CSV 尚未生成")

        return FileResponse(
            path=snapshot_file,
            media_type="text/csv; charset=utf-8",
            filename=os.path.basename(snapshot_file),
        )

    @app.post("/accounts/{account_name}/archive")
    async def archive_account(account_name: str):
        try:
            archived = await state.archive_account(account_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            LOGGER.exception("Account archival failed for %s", account_name)
            raise HTTPException(status_code=500, detail=f"账户归档失败: {exc}")

        grafana_error = ""
        try:
            await asyncio.to_thread(sync_dashboards, force_rebuild=True)
        except Exception as exc:
            LOGGER.exception("Grafana panel removal failed for %s", account_name)
            grafana_error = str(exc)
        return {
            "message": f"账户 {account_name} 已归档",
            "account_name": account_name,
            "archived_at": archived["archived_at"],
            "grafana_error": grafana_error,
        }

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
        request: Request,
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
        extra_credentials = dict(await request.form())
        # Accept submissions from the previous form while keeping the state
        # layer independent from exchange-specific field names.
        if "api_passphrase" in extra_credentials and "passphrase" not in extra_credentials:
            extra_credentials["passphrase"] = extra_credentials["api_passphrase"]
        try:
            state.validate_new_account_name(product_name)
            validated_credentials = await state.validate_exchange_credentials(
                exchange,
                account_type,
                api_key,
                secret_key,
                extra_credentials=extra_credentials,
            )
            extra_credentials.update(validated_credentials)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"API 凭证或账户权限验证失败: {exc}")

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
                extra_credentials=extra_credentials,
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

        account_type_label = state.get_account_type_label(
            account_info["exchange"], account_info["account_type"]
        )
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

    @app.get("/doc")
    async def user_manual():
        manual_path = os.path.join(state.BASE_DIR, "docs", "网页操作使用手册.html")
        if not os.path.isfile(manual_path):
            raise HTTPException(status_code=404, detail="网页操作使用手册不存在")
        return FileResponse(
            path=manual_path,
            media_type="text/html; charset=utf-8",
        )

    @app.get("/api/accounts/{account_name}/equity-changes")
    async def equity_changes(account_name: str, date: str, threshold: float):
        try:
            changes = await asyncio.to_thread(
                state.find_equity_changes,
                account_name,
                date,
                threshold,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"account_name": account_name, "date": date, "changes": changes}

    @app.post("/api/accounts/{account_name}/equity-changes")
    async def process_equity_changes(account_name: str, req: FundChangesRequest):
        if account_name not in state.accounts:
            raise HTTPException(status_code=404, detail="账户不存在")
        changes = [change.model_dump() for change in req.changes]
        subscription_total = sum(change["subscription_amount"] for change in changes)
        withdrawal_total = sum(change["withdrawal_amount"] for change in changes)
        account_info = state.account_infos[account_name]
        current_principal = float(account_info.get("principal", account_info["initial_unit"]))
        remaining_principal = current_principal + subscription_total - withdrawal_total
        if remaining_principal < 0:
            raise HTTPException(status_code=400, detail="处理后的本金不能小于 0")
        try:
            results = await state.accounts[account_name].handle_fund_changes(changes)
            principal_delta = subscription_total - withdrawal_total
            if principal_delta:
                remaining_principal = state.adjust_principal(account_name, principal_delta)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"资金变动保存失败: {exc}")
        return {
            "message": f"成功保存 {len(results)} 个资金变动时刻",
            "results": results,
            "remaining_principal": remaining_principal,
        }

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(collector_registry), media_type=CONTENT_TYPE_LATEST)


async def update_loop():
    await state.resolve_missing_credentials()
    for account_name in state.accounts:
        state.start_account_update_task(account_name)
    await asyncio.gather(*state.account_update_tasks.values())
