"""麦克风采集：纯 ctypes 调 winmm 的 waveIn，不依赖任何第三方录音库。

## 为什么不用 sounddevice / pyaudio
宿主插件进程跑在 N.E.K.O. 自带的嵌入式 Python 里，那个环境**装不了包**，
只有 numpy / scipy / av / onnxruntime / aiohttp / websockets 这些自带的东西。
所以录音只能走 Windows 自带的 winmm.dll：一次 `waveInOpen` + 若干个
`waveInAddBuffer`，然后轮询每个缓冲区的 `WHDR_DONE` 标志位，拿到就重新投回去。

## 采样格式
16000 Hz / 单声道 / 16 bit。这是宿主语音协议里明确支持的两种之一
（桌面 48000、移动/兼容 16000），取 16k 是为了把上行带宽砍一半——
按住说话这种场景音质要求没那么高，识别用的是同一套后端。

## 线程模型
`start()` 起一条采集线程，把 PCM 块塞进 `queue.Queue`；`read()` 从队列取。
回调方式（CALLBACK_FUNCTION）在驱动线程里被调用，容易踩到重入和 GIL 的坑，
所以这里用"投缓冲 + 轮询 DONE"的笨办法，稳。
"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

__all__ = ["MicError", "list_devices", "Recorder", "SAMPLE_RATE"]

SAMPLE_RATE = 16000
CHANNELS = 1
BITS = 16

# winmm 常量
WAVE_MAPPER = -1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0
WAVE_FORMAT_PCM = 1


class MicError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind          # no_device / busy / open_failed / not_started
        self.message = message


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.POINTER(ctypes.c_char)),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


class _WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


_winmm = None


def _mm():
    """延迟加载 winmm —— 插件在非 Windows 上 import 也不该炸。"""
    global _winmm
    if _winmm is None:
        _winmm = ctypes.WinDLL("winmm", use_last_error=True)
        _winmm.waveInGetNumDevs.restype = wintypes.UINT
        _winmm.waveInOpen.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), wintypes.UINT,
            ctypes.POINTER(_WAVEFORMATEX), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD,
        ]
        _winmm.waveInOpen.restype = wintypes.UINT
        _winmm.waveInPrepareHeader.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(_WAVEHDR), wintypes.UINT]
        _winmm.waveInPrepareHeader.restype = wintypes.UINT
        _winmm.waveInUnprepareHeader.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(_WAVEHDR), wintypes.UINT]
        _winmm.waveInUnprepareHeader.restype = wintypes.UINT
        _winmm.waveInAddBuffer.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(_WAVEHDR), wintypes.UINT]
        _winmm.waveInAddBuffer.restype = wintypes.UINT
        _winmm.waveInStart.argtypes = [ctypes.c_void_p]
        _winmm.waveInStart.restype = wintypes.UINT
        _winmm.waveInStop.argtypes = [ctypes.c_void_p]
        _winmm.waveInStop.restype = wintypes.UINT
        _winmm.waveInReset.argtypes = [ctypes.c_void_p]
        _winmm.waveInReset.restype = wintypes.UINT
        _winmm.waveInClose.argtypes = [ctypes.c_void_p]
        _winmm.waveInClose.restype = wintypes.UINT
    return _winmm


def list_devices() -> list[str]:
    """列出录音设备名。拿不到就返回空列表，不抛。"""
    try:
        mm = _mm()
        n = int(mm.waveInGetNumDevs())
    except Exception:  # noqa: BLE001 非 Windows / winmm 缺失
        return []
    out: list[str] = []
    for i in range(n):
        caps = _WAVEINCAPSW()
        try:
            rc = mm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
            if rc == MMSYSERR_NOERROR:
                out.append(f"{i}: {caps.szPname}")
        except Exception:  # noqa: BLE001
            continue
    return out


class Recorder:
    """按住说话期间的录音器。

    用法::

        rec = Recorder(chunk_ms=32)
        rec.start()
        while 按住: chunk = rec.read(0.2)   # bytes | None
        rec.stop()

    `chunk_ms=32` → 每块 512 个采样点（16 kHz），正好是宿主协议里
    "移动端 512 samples / 约 32 ms" 那个尺寸，避免触发它对帧长的检查。
    """

    def __init__(self, device: int = WAVE_MAPPER, chunk_ms: int = 32,
                 buffers: int = 8) -> None:
        self._device = device
        self._rate = SAMPLE_RATE
        self._chunk_samples = max(64, int(SAMPLE_RATE * chunk_ms / 1000))
        self._chunk_bytes = self._chunk_samples * 2
        self._n_buf = max(4, int(buffers))
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self._hwi = ctypes.c_void_p()
        self._hdrs: list[_WAVEHDR] = []
        self._raws: list[ctypes.Array] = []
        self._fmt: _WAVEFORMATEX | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._opened = False
        self._peak = 0

    # ── 属性 ────────────────────────────────────

    @property
    def sample_rate(self) -> int:
        return self._rate

    @property
    def peak(self) -> int:
        """录到过的最大振幅（0-32767）。用来判断"到底有没有收到声音"。"""
        return self._peak

    # ── 生命周期 ────────────────────────────────

    def start(self) -> None:
        mm = _mm()
        fmt = _WAVEFORMATEX(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=CHANNELS,
            nSamplesPerSec=self._rate,
            nAvgBytesPerSec=self._rate * CHANNELS * BITS // 8,
            nBlockAlign=CHANNELS * BITS // 8,
            wBitsPerSample=BITS,
            cbSize=0,
        )
        self._fmt = fmt
        rc = mm.waveInOpen(ctypes.byref(self._hwi), wintypes.UINT(self._device & 0xFFFFFFFF),
                           ctypes.byref(fmt), None, None, CALLBACK_NULL)
        if rc != MMSYSERR_NOERROR:
            # 32 = MMSYSERR_NODRIVER, 33 = MMSYSERR_NOMEM, 4 = MMSYSERR_ALLOCATED
            hint = {
                4: "麦克风被别的程序独占了（录音软件/语音通话）。",
                32: "系统里找不到可用的录音设备。",
                33: "系统内存不够，开不出录音缓冲。",
            }.get(rc, f"waveInOpen 返回 {rc}。")
            raise MicError("open_failed", f"打不开麦克风：{hint}")
        self._opened = True

        self._hdrs = []
        self._raws = []
        for _ in range(self._n_buf):
            raw = ctypes.create_string_buffer(self._chunk_bytes)
            hdr = _WAVEHDR()
            hdr.lpData = ctypes.cast(raw, ctypes.POINTER(ctypes.c_char))
            hdr.dwBufferLength = self._chunk_bytes
            rc = mm.waveInPrepareHeader(self._hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
            if rc != MMSYSERR_NOERROR:
                self._close()
                raise MicError("open_failed", f"申请录音缓冲失败（waveInPrepareHeader {rc}）。")
            rc = mm.waveInAddBuffer(self._hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
            if rc != MMSYSERR_NOERROR:
                self._close()
                raise MicError("open_failed", f"投递录音缓冲失败（waveInAddBuffer {rc}）。")
            self._hdrs.append(hdr)
            self._raws.append(raw)

        rc = mm.waveInStart(self._hwi)
        if rc != MMSYSERR_NOERROR:
            self._close()
            raise MicError("open_failed", f"启动录音失败（waveInStart {rc}）。")

        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._pump, name="hoi4-mic",
                                        daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        mm = _mm()
        while not self._stop_evt.is_set():
            got = False
            for hdr in self._hdrs:
                if hdr.dwFlags & WHDR_DONE:
                    n = int(hdr.dwBytesRecorded) or int(hdr.dwBufferLength)
                    if n > 0:
                        data = ctypes.string_at(hdr.lpData, n)
                        self._q.put(data)
                        self._track_peak(data)
                    hdr.dwFlags &= ~WHDR_DONE
                    hdr.dwBytesRecorded = 0
                    mm.waveInAddBuffer(self._hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
                    got = True
            if not got:
                self._stop_evt.wait(0.008)

    def _track_peak(self, data: bytes) -> None:
        # 只为了"有没有声音"这个判断，不用 numpy 也能算，量很小
        try:
            peak = 0
            for i in range(0, len(data) - 1, 2):
                v = data[i] | (data[i + 1] << 8)
                if v & 0x8000:
                    v -= 0x10000
                a = v if v >= 0 else -v
                if a > peak:
                    peak = a
            if peak > self._peak:
                self._peak = peak
        except Exception:  # noqa: BLE001 统计失败不影响录音
            pass

    def read(self, timeout: float = 0.3) -> bytes | None:
        """取一块 PCM。超时返回 None（不是错误，可能只是这一瞬没攒够）。"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> bytes:
        """把队列里剩下的都取出来拼成一段。"""
        out = bytearray()
        while True:
            try:
                out += self._q.get_nowait()
            except queue.Empty:
                break
        return bytes(out)

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None
        self._close()

    def _close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        mm = _mm()
        with self._lock:
            try:
                mm.waveInStop(self._hwi)
            except Exception:  # noqa: BLE001
                pass
            try:
                mm.waveInReset(self._hwi)
            except Exception:  # noqa: BLE001
                pass
            for hdr in self._hdrs:
                try:
                    mm.waveInUnprepareHeader(self._hwi, ctypes.byref(hdr),
                                             ctypes.sizeof(hdr))
                except Exception:  # noqa: BLE001
                    pass
            self._hdrs = []
            self._raws = []
            try:
                mm.waveInClose(self._hwi)
            except Exception:  # noqa: BLE001
                pass
            self._hwi = ctypes.c_void_p()

    def __enter__(self) -> "Recorder":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
