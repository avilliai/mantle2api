"""
管理接口路由
"""
import concurrent.futures
import asyncio
import logging
import os
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.cookie_manager import cm

log    = logging.getLogger("cosverse_proxy.admin")
router = APIRouter(prefix="/admin")

REGISTER_CONCURRENCY_LIMIT = max(
    1, int(os.getenv("REGISTER_CONCURRENCY_LIMIT", "2"))
)
ADMIN_PROBE_CONCURRENCY = max(
    1, int(os.getenv("ADMIN_PROBE_CONCURRENCY", "2"))
)
register_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=REGISTER_CONCURRENCY_LIMIT
)


def _register_worker(task_id: int) -> None:
    try:
        import asyncio
        from app.cosverse_client import CosverseRegister
        result = asyncio.run(CosverseRegister().run())
        if result:
            cm.add_account(result)
            cm.save()
            log.info(f"🎉 注册任务-{task_id} 成功: {result['email']}")
        else:
            log.warning(f"💥 注册任务-{task_id} 失败")
    except Exception as e:
        log.error(f"注册 Worker-{task_id} 异常: {e}")


def _account_summary() -> dict:
    return {
        "total": len(cm.accounts),
        "alive": sum(1 for a in cm.accounts if a.healthy and not a.disabled),
    }


def _account_view(acc) -> dict:
    return {
        "index":    acc.index,
        "email":    acc.email,
        "user_id":  acc.user_id,
        "is_alive": acc.healthy,
        "disabled": acc.disabled,
    }


@router.get("/tokens/detail")
async def get_all_tokens_detail():
    # 直接返回 cm.get_tokens_detail()，能完美匹配前端 dashboard.html
    return cm.get_tokens_detail()

@router.post("/tokens/refresh")
async def tokens_refresh_all(background_tasks: BackgroundTasks):
    background_tasks.add_task(cm.keepalive_tick)
    return {"message": "活跃池 usage 余额探测已启动，不会扫描全量账号或自动发送 OTP"}


@router.post("/tokens/{idx}/refresh")
async def token_refresh_one(idx: int):
    acc = next((a for a in cm.accounts if a.index == idx), None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    alive = await cm.check_session(acc)
    return {
        "message": "usage 余额探测完成",
        "alive": alive,
        "token": _account_view(acc),
    }


@router.post("/tokens/bulk/refresh")
async def tokens_bulk_refresh(req: Request, background_tasks: BackgroundTasks):
    indexes: list[int] = (await req.json()).get("indexes", [])
    selected = [a for a in cm.accounts if a.index in indexes and not a.disabled]

    async def _task():
        sem = asyncio.Semaphore(ADMIN_PROBE_CONCURRENCY)

        async def _probe(acc):
            async with sem:
                await cm.check_session(acc, save=False)
                await asyncio.sleep(1)

        await asyncio.gather(*[_probe(acc) for acc in selected])
        await cm.async_save()

    background_tasks.add_task(_task)
    return {
        "message": f"已启动 {len(selected)} 个账号的 usage 余额探测",
        "count": len(selected),
    }


@router.post("/tokens/bulk/disable")
async def tokens_bulk_disable(req: Request):
    indexes: list[int] = (await req.json()).get("indexes", [])
    for acc in cm.accounts:
        if acc.index in indexes:
            acc.disabled = True
    cm.save()
    return {"disabled": len(indexes)}


@router.post("/tokens/bulk/enable")
async def tokens_bulk_enable(req: Request):
    indexes: list[int] = (await req.json()).get("indexes", [])
    for acc in cm.accounts:
        if acc.index in indexes:
            acc.disabled = False
    cm.save()
    return {"enabled": len(indexes)}


@router.post("/tokens/bulk/delete")
async def tokens_bulk_delete(req: Request):
    indexes: list[int] = (await req.json()).get("indexes", [])
    with cm._sync_lock:
        before         = len(cm.accounts)
        cm.accounts    = [a for a in cm.accounts if a.index not in indexes]
        removed        = before - len(cm.accounts)
    cm.save()
    return {"removed": removed}


@router.delete("/tokens/{idx}")
async def token_delete_one(idx: int):
    with cm._sync_lock:
        cm.accounts = [a for a in cm.accounts if a.index != idx]
    cm.save()
    return {"message": "已删除"}


@router.post("/tokens/add")
async def tokens_add(req: Request):
    data   = await req.json()
    tokens = data.get("tokens", [data])
    added  = 0
    for t in tokens:
        if cm.add_account(t):
            added += 1
    cm.save()
    return {"added": added, "total": len(cm.accounts)}


@router.post("/register/start")
async def register_start(req: Request):
    global register_executor
    data  = await req.json()
    count = int(data.get("count", 1))
    requested_conc = int(data.get("concurrency", 1))
    conc = max(1, min(requested_conc, REGISTER_CONCURRENCY_LIMIT))
    if register_executor._max_workers != conc:
        register_executor = concurrent.futures.ThreadPoolExecutor(max_workers=conc)
    for i in range(count):
        register_executor.submit(_register_worker, i + 1)
    return {
        "message": (
            f"成功投递 {count} 个注册任务 (并发 {conc}，"
            f"上限 {REGISTER_CONCURRENCY_LIMIT})"
        )
    }


@router.get("/register/status")
async def register_status():
    return {"total_accounts": len(cm.accounts)}
