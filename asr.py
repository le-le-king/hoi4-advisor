"""本地语音识别：sherpa-onnx + Paraformer，完全离线，不经宿主。

## 为什么要自己做

宿主那条语音 WebSocket 走的是它自己的实时识别（Gemini live），中文短句
（尤其带游戏术语的）识别得不好，而且必须实时上传、要它自己判 VAD。
这条本地链路是：

    麦克风 PCM → 本地 paraformer → 文字 → 只把文字发给猫娘

好处是识别这一段完全本地、零延迟抖动、不依赖宿主那边的模型状态，
坏处是要带一个 216 MB 的模型。

## 部署形态

```
<插件>/vendor/                     sherpa_onnx 的 python 包 + pyd + onnxruntime.dll
<插件>/data/models/paraformer-zh/  model.int8.onnx + tokens.txt
```

`vendor` 跟插件一起打包（18 MB）；模型太大不进包，由
`hoi4_voice action=check` 检查、缺失时提示放哪。

## ★踩过的坑：路径里不能有中文

`sherpa_onnx` 的 C++ 层用 narrow（ANSI）路径 `ifstream` 打开模型，
传中文路径过去会"打开失败"却只报一句
`RuntimeError: No graph was found in the protobuf`——看着像模型坏了，
其实是路径。所以下面 `_ascii_open` 做了兜底：路径含非 ASCII 时先 chdir 到
模型目录再用相对文件名，加载完立刻 chdir 回来。

## 性能（本机实测，i5 级 CPU）

- 首次加载 216 MB int8 模型：约 6 秒（所以插件起来后要预热）
- 识别 1.7 秒音频：0.07 秒（RTF 0.04，比实时快 20 倍以上）
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

__all__ = ["LocalAsr", "AsrError", "DEFAULT_MODEL_NAME"]

DEFAULT_MODEL_NAME = "paraformer-zh"
STATE_IDLE = "未加载"
STATE_LOADING = "加载中"
STATE_READY = "就绪"
STATE_FAILED = "加载失败"


class AsrError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _ascii_open(fn, model_dir: Path):
    """C++ 那边只认 ANSI 路径。含中文时 chdir 进目录用相对名打开。"""
    if str(model_dir).isascii():
        return fn(str(model_dir / "model.int8.onnx"), str(model_dir / "tokens.txt"))
    old = os.getcwd()
    try:
        os.chdir(model_dir)
        return fn("model.int8.onnx", "tokens.txt")
    finally:
        try:
            os.chdir(old)
        except OSError:
            pass


class LocalAsr:
    """一个懒加载 + 可预热的本地识别器。线程安全。"""

    def __init__(self, model_dir: str | Path, vendor_dir: str | Path | None = None,
                 log=None, threads: int = 2) -> None:
        self._model_dir = Path(model_dir)
        self._vendor = Path(vendor_dir) if vendor_dir else Path(__file__).resolve().parent / "asrlib"
        self._log = log or (lambda msg: None)
        self._threads = max(1, int(threads))
        self._rec = None
        self._np = None
        self._state = STATE_IDLE
        self._error = ""
        self._load_lock = threading.Lock()
        self._decode_lock = threading.Lock()
        self._load_seconds = 0.0
        self._last_ms = 0.0

    # ── 对外 ────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict:
        return {
            "state": self._state,
            "model_dir": str(self._model_dir),
            "model_ok": self.model_present(),
            "load_seconds": round(self._load_seconds, 2),
            "last_ms": round(self._last_ms, 1),
            "error": self._error,
        }

    def model_present(self) -> bool:
        return (self._model_dir / "model.int8.onnx").is_file() and \
               (self._model_dir / "tokens.txt").is_file()

    def preload(self) -> bool:
        """后台预热：插件起来就调，别等用户第一次按下去才加载 6 秒。"""
        try:
            self._ensure()
            return True
        except AsrError:
            return False

    def transcribe(self, pcm: bytes, rate: int = 16000) -> str:
        """PCM16 单声道 → 文字。识别不出来返回空串（不是错误）。"""
        if not pcm:
            return ""
        self._ensure()
        np = self._np
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.time()
        with self._decode_lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(int(rate), samples)
            self._rec.decode_stream(stream)
            text = str(getattr(stream.result, "text", "") or "").strip()
        self._last_ms = (time.time() - t0) * 1000.0
        return text

    # ── 加载 ────────────────────────────────────

    def _ensure(self) -> None:
        if self._rec is not None:
            return
        with self._load_lock:
            if self._rec is not None:
                return
            if self._state == STATE_FAILED and self._error:
                # 失败过一次就不再重试，除非文件变了（用户后来补了模型）
                if not self.model_present():
                    raise AsrError("no_model", self._error)
            self._state = STATE_LOADING
            self._error = ""
            try:
                self._load()
            except AsrError:
                self._state = STATE_FAILED
                raise
            except Exception as exc:  # noqa: BLE001
                self._state = STATE_FAILED
                self._error = f"{type(exc).__name__}: {exc}"
                raise AsrError("load_failed", f"本地识别加载失败：{self._error}") from None
            self._state = STATE_READY

    def _load(self) -> None:
        if not self.model_present():
            self._error = ("本地识别模型不在：" + str(self._model_dir) +
                           "（需要 model.int8.onnx 和 tokens.txt）")
            raise AsrError("no_model", self._error)

        vendor = str(self._vendor)
        if not Path(vendor).is_dir():
            self._error = f"识别运行库不在：{vendor}"
            raise AsrError("no_vendor", self._error)
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        try:  # pyd 依赖同目录的 onnxruntime.dll
            os.add_dll_directory(vendor)
        except (AttributeError, OSError):
            pass

        t0 = time.time()
        try:
            import numpy as np
            import sherpa_onnx
        except Exception as exc:  # noqa: BLE001
            self._error = f"import 失败：{type(exc).__name__}: {exc}"
            raise AsrError("import_failed", self._error) from None

        def _make(model: str, tokens: str):
            return sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=model,
                tokens=tokens,
                num_threads=self._threads,
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
                debug=False,
            )

        try:
            self._rec = _ascii_open(_make, self._model_dir)
        except RuntimeError as exc:
            self._error = f"{exc}（多半是模型文件不完整，或者路径里有中文）"
            raise AsrError("load_failed", self._error) from None
        self._np = np
        self._load_seconds = time.time() - t0
        self._log(f"本地识别就绪（加载 {self._load_seconds:.1f}s，模型 {self._model_dir.name}）")
