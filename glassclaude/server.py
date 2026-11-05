"""云端服务：``/agent`` 接千问平台，``/bridge`` 接 Mac 上的桥接程序，都是 WebSocket。

平台这头的握手、重连和保活和 reader 的文字版相同。``X-Request-Id`` 要原样写回，101 和 401 都要。
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .hub import Delivery, Hub
from .hearing import AsrConfig, Hearing
from .panel import KEEPALIVE_SECONDS, SPEC_VERSION, Panel

__all__ = ["create_app"]

log = logging.getLogger(__name__)

#: 连接建立后等 SESSION_INIT 多久
INIT_TIMEOUT_SECONDS = 30
#: 发给 Mac 的消息在内存里排多少条
BRIDGE_BUFFER = 64
#: 多久看一次有没有攒着的进度要发给眼镜
TICK_SECONDS = 1.0


def _authorized(header: str | None, token: str) -> bool:
    scheme, _, value = (header or "").partition(" ")
    return bool(token) and scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), token.encode())


def _dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code)
    except (RuntimeError, WebSocketDisconnect):  # 对方已经断了
        pass


async def _deny(ws: WebSocket, request_id: str) -> None:
    headers = {"X-Request-Id": request_id} if request_id else {}
    try:
        await ws.send_denial_response(JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers))
    except RuntimeError:  # 服务器不支持拒绝时带响应体，退回普通的 403
        await ws.close(1008)


async def _deliver(delivery: Delivery) -> None:
    for panel, messages in delivery:
        if messages and panel.emit is not None:
            try:
                await panel.emit(messages)
            except (RuntimeError, WebSocketDisconnect):
                log.info("眼镜连接已断，这次更新没发出去（会话 %s）", panel.id)


async def serve_platform(ws: WebSocket, hub: Hub, token: str, asr: AsrConfig | None = None) -> None:
    """一条平台连接的生命周期。"""
    request_id = ws.headers.get("x-request-id", "")
    if not _authorized(ws.headers.get("authorization"), token):
        log.warning("拒绝平台连接：Bearer Token 缺失或不匹配")
        await _deny(ws, request_id)
        return
    await ws.accept(headers=[(b"x-request-id", request_id.encode("latin-1"))] if request_id else None)
    lock = anyio.Lock()
    session_id = ""

    async def send(messages: list[dict[str, Any]]) -> None:
        try:
            async with lock:
                for message in messages:
                    await ws.send_text(_dumps(message))
        except (RuntimeError, WebSocketDisconnect):  # 连接断了，收消息的循环会看到
            log.info("眼镜连接已断，这次更新没发出去（会话 %s）", session_id)

    async def send_bytes(data: bytes) -> None:
        try:
            async with lock:
                await ws.send_bytes(data)
        except (RuntimeError, WebSocketDisconnect) as exc:
            log.info("ACK 没发出去：%r（会话 %s）", exc, session_id)

    try:
        with anyio.fail_after(INIT_TIMEOUT_SECONDS):
            first = json.loads(await ws.receive_text())
    except (TimeoutError, ValueError, WebSocketDisconnect):
        await _close(ws, 1002)
        return
    payload = first.get("payload") or {}
    session_id = str(first.get("sessionId", ""))
    if first.get("type") != "SESSION_INIT" or first.get("specVersion") != SPEC_VERSION or not session_id:
        await _close(ws, 1002)
        return
    panel, mode = hub.attach(session_id, int(payload.get("connectionEpoch", 0)))
    if mode == "STALE":
        await _close(ws, 4002)
        return
    if panel is None:
        stranger = Panel(session_id, hub)
        await send([stranger.error("UNKNOWN_SESSION", "这个会话已经不在了", fatal=True)])
        await _close(ws, 1011)
        return
    previous, panel.socket = panel.socket, ws
    panel.emit = send
    try:
        await send([panel.ready(mode)])
        if previous is not None:
            await _close(previous, 4001)

        async def step(work: Any, *args: Any) -> None:
            try:
                messages = work(*args)
            except Exception:  # 抛到任务组会断开连接，重连后撞上同一个错又断，一直循环
                log.exception("处理出错（会话 %s）", panel.id)
                messages = panel.failed()
            await send(messages)

        await step(panel.start, payload) if mode == "CREATED" else await step(panel.resumed)
        if payload.get("inputMode"):  # 恢复时沿用原来的，平台可能不再写
            panel.input_mode = str(payload["inputMode"])
        hearing = None
        if panel.input_mode == "AUDIO":
            if asr is None:
                log.warning("插件选的是音频流，但没有配置自己的识别（GLASSCLAUDE_ASR_URL、GLASSCLAUDE_ASR_KEY），听不到用户说话")
            else:

                async def heard(text: str) -> None:
                    await step(panel.handle, {"type": "UTTERANCE", "payload": {"text": text}})

                hearing = Hearing(asr, heard, label=panel.id)

        async def beat() -> None:
            while True:
                await anyio.sleep(KEEPALIVE_SECONDS)
                if panel.expired():  # 主动结束，麦克风才会关。主循环要等下一条消息才看 closing，所以在这里关
                    log.info("太久没操作，结束眼镜会话 %s", panel.id)
                    panel.closing = True
                    await _close(ws, 1000)
                    return
                await send(panel.keepalive())

        async def flush() -> None:
            while True:
                await anyio.sleep(TICK_SECONDS)
                await step(panel.tick)

        async with anyio.create_task_group() as tg:
            tg.start_soon(beat)
            tg.start_soon(flush)
            if hearing is not None:
                tg.start_soon(hearing.run)
            try:
                while not panel.closing:
                    received = await ws.receive()
                    if received["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(received.get("code", 1000))
                    if received.get("bytes") is not None:  # 音频分片：只解析、回 ACK、过门限，识别在后台
                        ack = hearing.chunk(received["bytes"]) if hearing is not None else None
                        if ack is not None:
                            await send_bytes(ack)
                        continue
                    try:
                        message = json.loads(received.get("text") or "")
                    except ValueError:
                        await send([panel.error("BAD_JSON", "消息不是 JSON")])
                        continue
                    kind, body = message.get("type"), message.get("payload") or {}
                    if kind in ("MEDIA_START", "MEDIA_END"):
                        if hearing is not None:
                            hearing.start(body.get("streamId")) if kind == "MEDIA_START" else hearing.end(body.get("reason"))
                        continue
                    await step(panel.handle, message)
                await _close(ws, 1000)
                hub.drop(session_id)
            except WebSocketDisconnect as exc:
                if exc.code == 1000 or panel.closing:  # 平台正常结束；异常断开要留着等重连
                    hub.drop(session_id)
            finally:
                if hearing is not None:
                    hearing.close()
                tg.cancel_scope.cancel()
    except WebSocketDisconnect:
        log.info("眼镜连接在握手时断了（会话 %s）", panel.id)
    finally:
        if panel.socket is ws:
            panel.socket = panel.emit = None
        hub.released()


async def serve_bridge(ws: WebSocket, hub: Hub, token: str, replace: dict[str, Any]) -> None:
    """一条 Mac 连接的生命周期。新连接顶掉旧的：电脑休眠醒来时旧连接可能还没断。"""
    if not _authorized(ws.headers.get("authorization"), token):
        log.warning("拒绝桥接连接：Bearer Token 缺失或不匹配")
        await _deny(ws, "")
        return
    await ws.accept()
    previous, replace["socket"] = replace.get("socket"), ws
    if previous is not None:
        await _close(previous, 4001)
    writer, reader = anyio.create_memory_object_stream[dict[str, Any]](BRIDGE_BUFFER)

    def outbox(message: dict[str, Any]) -> bool:
        try:
            writer.send_nowait(message)
        except (anyio.WouldBlock, anyio.BrokenResourceError, anyio.ClosedResourceError):
            log.warning("发给 Mac 的消息排满了，丢掉 %s", message.get("type"))
            return False
        return True

    async def pump() -> None:
        async for message in reader:
            await ws.send_text(_dumps(message))

    await _deliver(hub.connected(outbox))
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pump)
            try:
                while True:
                    try:
                        message = json.loads(await ws.receive_text())
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        await _deliver(hub.receive(message))
            except WebSocketDisconnect:
                pass
            finally:
                tg.cancel_scope.cancel()
    finally:
        if replace.get("socket") is ws:
            replace["socket"] = None
        await _deliver(hub.disconnected(outbox))


def create_app(agent_token: str, bridge_token: str, allowed_devices: frozenset[str] = frozenset(), asr: AsrConfig | None = None) -> Starlette:
    hub = Hub(allowed_devices)
    bridge_slot: dict[str, Any] = {}

    async def agent(ws: WebSocket) -> None:
        await serve_platform(ws, hub, agent_token, asr)

    async def bridge(ws: WebSocket) -> None:
        await serve_bridge(ws, hub, bridge_token, bridge_slot)

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})  # 公开地址，不说 Mac 在不在线

    app = Starlette(routes=[Route("/healthz", healthz), WebSocketRoute("/agent", agent), WebSocketRoute("/bridge", bridge)])
    app.state.hub = hub
    return app
