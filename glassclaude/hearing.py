"""音频流模式（插件的通信方式选「音频流」）：平台推来的声音转给自己的实时识别，整句结果当成用户说的话。

千问自己的识别会把闲聊这类话滤掉，到不了服务，所以另开一条路：
平台在会话里一直推 16 kHz 单声道 PCM16LE，每条二进制消息前 20 字节是头（大端：流编号、序号、平台时间戳、长度）。
每收到 20 片回一个 16 字节的 ACK，回慢了平台会停流（``CONSUMER_TOO_SLOW``）。

识别用 OpenAI GA 版 Realtime 的转写会话（``intent=transcription``），比如 AxonHub 网关，网关转给百炼 Paraformer。
AxonHub 转百炼时的规矩：只收 24 kHz，推出去的声音按秒计费，约 60 秒收不到音频百炼就断开。

- 说完由这边判断：网关自己断句（server_vad，靠百炼判断句尾）在真眼镜音频上要等 16～21 秒，越来越晚；
  网关新版收到 ``commit`` 就让百炼马上结束这一句，实测 0.7～1.1 秒出整句。所以本地响度门限判断开口和说完：
  安静满 :data:`END_SECONDS` 就发 ``commit``。server_vad 留着兜底，它先断了句，这边的 ``commit`` 只会收到一个报错。
- 只推说话的那几段：开口前 :data:`PREROLL_SECONDS` 的声音先攒着，开口时补发；说完之后的安静不推，不为静音付钱。
- 开口才连，几句话共用一个连接（建连接要 1～5 秒）。安静满 :data:`IDLE_SECONDS`、话都出了结果才断开，下次开口再连。
- 一条消息推 :data:`BATCH_SECONDS` 的声音：20 毫秒一条消息太多。
- 平台这边不能被识别拖住：分片进一个有上限的队列，识别跟不上就丢最旧的，ACK 照常回。
- 识别出的整句交给 ``on_text``，和千问的 ``UTTERANCE`` 走同一条路：只进草稿，单击才发。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import struct
import time
from array import array
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets

log = logging.getLogger(__name__)

HEADER = struct.Struct(">IIQI")  # streamId, sequence, serverTimestampMs, payloadLength
ACK = struct.Struct(">IIQ")
#: 每收到这么多片回一个 ACK
ACK_EVERY = 20
#: 平台的采样率（SESSION_INIT 的 media.audio），和 GA 协议只收的采样率
SOURCE_RATE, TARGET_RATE = 16000, 24000
#: 响度门限（16 位采样的均方根）。眼镜离嘴近，说话一般在几千，环境声几百
GATE_RMS = 400
#: 安静这么久算一句说完，发 ``commit``。句中的停顿比这短；切碎了也不要紧，草稿会把几段接起来
END_SECONDS = 0.8
END_SAMPLES = int(END_SECONDS * SOURCE_RATE)
#: 网关自己断句等的静音，只作兜底
SILENCE_MS = 1500
#: 开口前的声音攒这么多，连上后补发
PREROLL_SECONDS = 0.5
#: 安静这么久、话都出了结果，就断开连接：百炼约 60 秒收不到音频就会自己断
IDLE_SECONDS = 50.0
#: 队列里表示“这句说完了”的记号
COMMIT = "commit"
#: 一条消息攒这么多声音再推
BATCH_SECONDS = 0.1
#: 等最后一句结果最多这么久
FINISH_SECONDS = 4.0
#: 等识别的音频最多攒这么多秒，再多就丢最旧的
QUEUE_SECONDS = 10.0
#: 隔这么久在日志里记一次音频统计：平台送到这里晚了多少、有多响
STATS_SECONDS = 10.0
#: 识别连接出错，隔这么久再试，最长这么久
RETRY_MIN_SECONDS, RETRY_MAX_SECONDS = 1.0, 30.0


@dataclass(frozen=True)
class AsrConfig:
    """自己的实时识别：GA 版 Realtime 转写会话的完整地址（带 ``intent=transcription`` 和 ``model``）和 Key。"""

    url: str
    key: str
    language: str = "zh"
    gate: int = GATE_RMS

    @classmethod
    def from_env(cls) -> AsrConfig | None:
        url, key = os.environ.get("GLASSCLAUDE_ASR_URL", ""), os.environ.get("GLASSCLAUDE_ASR_KEY", "")
        if not url or not key:
            return None
        return cls(url, key, os.environ.get("GLASSCLAUDE_ASR_LANGUAGE", "zh"), int(os.environ.get("GLASSCLAUDE_ASR_GATE", str(GATE_RMS))))


def parse_chunk(data: bytes) -> tuple[int, int, int, bytes] | None:
    """一条二进制分片：(流编号, 序号, 平台时间戳, PCM)。长度对不上的不要。"""
    if len(data) < HEADER.size:
        return None
    stream, sequence, stamp, length = HEADER.unpack_from(data)
    if len(data) != HEADER.size + length:
        return None
    return stream, sequence, stamp, data[HEADER.size:]


def loudness(pcm: bytes) -> float:
    """PCM16LE 的均方根。"""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def upsample(pcm: bytes) -> bytes:
    """16 kHz 转 24 kHz，线性插值：每 2 个采样变 3 个。网关会再降回 16 kHz，这点失真识别不在乎。"""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    out = array("h")
    for i in range(0, len(samples) - 1, 2):
        a, b = samples[i], samples[i + 1]
        c = samples[i + 2] if i + 2 < len(samples) else b
        out.extend((a, (a + 2 * b) // 3, (2 * b + c) // 3))
    if len(samples) % 2:
        out.extend((samples[-1], samples[-1]))
    return out.tobytes()


class Hearing:
    """一个眼镜会话的收音。``chunk`` 在收消息的循环里调，只做解析、ACK 和门限；``run`` 是后台任务，连识别、推音频、收结果。"""

    def __init__(self, config: AsrConfig, on_text: Callable[[str], Awaitable[None]], label: str = ""):
        self.config = config
        self.on_text = on_text
        self.label = label
        self.stream: int | None = None
        self.count = 0
        self.queue: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self.queued_bytes = 0
        self.preroll: deque[bytes] = deque()
        self.preroll_bytes = 0
        self.quiet = END_SAMPLES  # 连着安静了多少个采样。一开始当作安静：没说话不推
        self.talking = False  # 正在说一句话：声音要推，安静够了要发 commit
        self.pending = 0  # 发了 commit、还没收到整句的句数
        self.connected = False
        self.dropped = 0
        self.closed = False
        # 查延迟用：平台的分片晚到多少、有多响，发 commit 的时刻
        self.stats: dict[str, float] = {"count": 0, "lag": 0.0, "worst": 0.0, "loudest": 0.0, "since": time.monotonic()}
        self.committed_at = 0.0

    # ------------------------------------------------------------------ 平台那边

    def start(self, stream: Any) -> None:
        """MEDIA_START：新的一条流，序号从 1 重新算。"""
        self.stream = stream if isinstance(stream, int) else None
        self.count = 0
        log.info("音频流开始 %s（会话 %s）", stream, self.label)

    def end(self, reason: Any) -> None:
        """MEDIA_END：不足 20 片的尾巴不用补 ACK。"""
        log.info("音频流结束 %s，原因 %s（会话 %s）", self.stream, reason, self.label)
        self.stream = None
        self.count = 0
        self.quiet = END_SAMPLES
        self.talking = False
        self._push(None)

    def chunk(self, data: bytes) -> bytes | None:
        """一条二进制分片。到了 20 片返回要回给平台的 ACK。"""
        parsed = parse_chunk(data)
        if parsed is None:
            return None
        stream, sequence, stamp, pcm = parsed
        self.count += 1
        self._measure(stamp, pcm)
        self._gate(pcm)
        return ACK.pack(stream, sequence, stamp) if self.count % ACK_EVERY == 0 else None

    def close(self) -> None:
        self.closed = True
        self._push(None)

    def _measure(self, stamp: int, pcm: bytes) -> None:
        stats = self.stats
        lag = time.time() * 1000 - stamp  # 平台的钟和这边的钟都对时，差一点不要紧
        stats["count"] += 1
        stats["lag"] += lag
        stats["worst"] = max(stats["worst"], lag)
        stats["loudest"] = max(stats["loudest"], loudness(pcm))
        if time.monotonic() - stats["since"] >= STATS_SECONDS:
            log.info("音频：%d 片，平台到这里平均晚 %.0f 毫秒、最多 %.0f，最响 %.0f，%s（会话 %s）", stats["count"], stats["lag"] / stats["count"],
                     stats["worst"], stats["loudest"], "连着识别" if self.connected else "没连", self.label)
            self.stats = {"count": 0, "lag": 0.0, "worst": 0.0, "loudest": 0.0, "since": time.monotonic()}

    # ------------------------------------------------------------------ 静音门限

    def _gate(self, pcm: bytes) -> None:
        """开口时补上前面一小段，说话时推，安静满 :data:`END_SECONDS` 就记一个 commit，之后的安静只攒着备用。"""
        if loudness(pcm) >= self.config.gate:
            if not self.talking:
                if not self.connected:
                    log.info("开口了，去连识别（会话 %s）", self.label)
                while self.preroll:
                    self._push(self.preroll.popleft())
                self.preroll_bytes = 0
            self.quiet = 0
            self.talking = True
            self._push(pcm)
            return
        self.quiet += len(pcm) // 2
        if self.talking and self.quiet < END_SAMPLES:  # 句中的停顿：照推
            self._push(pcm)
            return
        if self.talking:  # 安静够了：这句说完
            self.talking = False
            self._push(COMMIT)
        self.preroll.append(pcm)
        self.preroll_bytes += len(pcm)
        while self.preroll_bytes > PREROLL_SECONDS * SOURCE_RATE * 2:
            self.preroll_bytes -= len(self.preroll.popleft())

    def _push(self, pcm: bytes | str | None) -> None:
        if isinstance(pcm, bytes):
            self.queued_bytes += len(pcm)
            while self.queued_bytes > QUEUE_SECONDS * SOURCE_RATE * 2 and not self.queue.empty():  # 识别跟不上：丢最旧的
                old = self.queue.get_nowait()
                if isinstance(old, bytes):
                    self.queued_bytes -= len(old)
                    self.dropped += 1
        self.queue.put_nowait(pcm)

    async def _next(self, timeout: float | None) -> bytes | str | None:
        """下一项：一段音频、:data:`COMMIT`，或者 ``None``（流或会话结束）。超时抛 TimeoutError。"""
        item = await asyncio.wait_for(self.queue.get(), timeout)
        if isinstance(item, bytes):
            self.queued_bytes -= len(item)
        return item

    # ------------------------------------------------------------------ 识别那边

    async def run(self) -> None:
        """开口了才连识别，一句话说完、结果拿到就断开。会话结束（``close``）才退出。"""
        delay = RETRY_MIN_SECONDS
        while not self.closed:
            first = await self._next(None)
            if not isinstance(first, bytes):  # 没连着时的 commit 和流结束都不用管
                continue
            try:
                await self._utterance(first)
                delay = RETRY_MIN_SECONDS
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                log.warning("识别连接出错：%s，%.0f 秒后再连（会话 %s）", exc, delay, self.label)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_SECONDS)
            except Exception:  # 抛出去会断掉整条眼镜连接，只记下来
                log.exception("识别出错（会话 %s）", self.label)
                await asyncio.sleep(delay)

    async def _utterance(self, first: bytes) -> None:
        """一条识别连接：说话时推，说完发 commit，安静够久、话都出了结果再断开。"""
        headers = {"Authorization": f"Bearer {self.config.key}"}
        self.connected = True
        try:
            began = time.monotonic()
            async with websockets.connect(self.config.url, additional_headers=headers, open_timeout=15, max_size=2**20) as ws:
                log.info("连上识别，用了 %.1f 秒，队列里攒了 %.1f 秒的声音（会话 %s）", time.monotonic() - began, self.queued_bytes / 2 / SOURCE_RATE, self.label)
                self.pending = 0
                await ws.send(json.dumps(self.session_update()))
                listener = asyncio.create_task(self._listen(ws))
                batch = [first]
                heard = time.monotonic()
                try:
                    while not listener.done():
                        try:
                            item = await self._next(BATCH_SECONDS)
                        except TimeoutError:  # 没有新声音：手里的先推掉
                            item = b""
                        if item is None:  # 流或会话结束：这句没说完也收尾，等结果再断
                            await self._append(ws, b"".join(batch))
                            if self.talking or batch:
                                await self._commit(ws)
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(self._settled(), FINISH_SECONDS)
                            return
                        if item == COMMIT:
                            await self._append(ws, b"".join(batch))
                            batch = []
                            await self._commit(ws)
                            continue
                        if item:
                            batch.append(item)
                            heard = time.monotonic()
                        if batch and (sum(map(len, batch)) >= BATCH_SECONDS * SOURCE_RATE * 2 or not item):
                            await self._append(ws, b"".join(batch))
                            batch = []
                        if not self.pending and not self.talking and time.monotonic() - heard >= IDLE_SECONDS:  # 安静够久，话都出了结果：断开
                            return
                    listener.result()  # 识别那边先断了：有错就抛出去
                finally:
                    listener.cancel()
        finally:
            self.connected = False

    async def _commit(self, ws: Any) -> None:
        self.pending += 1
        self.committed_at = time.monotonic()
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def _settled(self) -> None:
        while self.pending:
            await asyncio.sleep(0.1)

    def session_update(self) -> dict[str, Any]:
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": TARGET_RATE},
                        "transcription": {"language": self.config.language},
                        "turn_detection": {"type": "server_vad", "silence_duration_ms": SILENCE_MS},
                    },
                },
            },
        }

    async def _append(self, ws: Any, pcm: bytes) -> None:
        if not pcm:
            return
        await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(upsample(pcm)).decode("ascii")}))


    async def _listen(self, ws: Any) -> None:
        async for raw in ws:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "conversation.item.input_audio_transcription.completed":
                self.pending = max(self.pending - 1, 0)
                text = str(event.get("transcript") or "").strip()
                if text:
                    after = time.monotonic() - self.committed_at if self.committed_at else -1
                    log.info("自己的识别：%d 字，发 commit 后 %.1f 秒出结果（会话 %s）", len(text), after, self.label)
                    await self.on_text(text)
            elif kind == "error":
                error = event.get("error") or {}
                if "commit" in str(error.get("code", "")):  # 网关先断了句，这边的 commit 落空：不再等它的结果
                    self.pending = max(self.pending - 1, 0)
                    log.info("commit 落空：%s（会话 %s）", error.get("code"), self.label)
                else:
                    log.warning("识别报错：%s（会话 %s）", error, self.label)
        if ws.close_code not in (None, 1000):  # 1011 是百炼那边失败，比如额度用完：原因写在 close_reason 里
            log.warning("识别连接被关掉：%s %s（会话 %s）", ws.close_code, ws.close_reason, self.label)
