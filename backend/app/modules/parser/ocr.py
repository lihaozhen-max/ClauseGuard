"""PaddleOCR 引擎封装（SPEC FR-PARSE-02/03、PS-04、CF-19…CF-21）。

三个**实测踩坑点**（详见 `docs/M2-验收记录.md`）：

1. **模型目录必须是纯 ASCII 路径**：Paddle Inference 在 Windows 上打不开含中文的路径，
   报出误导性的 ``json parse error: attempting to parse an empty input``；
2. **必须关闭 oneDNN/MKLDNN**：paddle 3.3.1 的 PIR 执行器在该路径上有转换缺陷
   （``ConvertPirAttribute2RuntimeAttribute not support``），默认 `enable_mkldnn=True` 会必崩；
3. OCR 常把数字识别成被空格切开（``80%`` → ``8 0 %``），需要后处理修复。

引擎**懒加载**：只有真正遇到扫描页时才导入 paddleocr 并加载模型（首次会联网下载模型）。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.modules.parser.cleaning import collapse_whitespace, normalize_ocr_text

logger = get_logger(__name__)

#: paddlex 的模型缓存根目录环境变量
MODEL_CACHE_ENV = "PADDLE_PDX_CACHE_HOME"


@dataclass
class OcrLine:
    """OCR 识别出的一行文本（含文本框左上角坐标，用于 §2.6 的 OCR 位置格式）。"""

    text: str
    score: float
    bbox: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "score": round(self.score, 4), "bbox": list(self.bbox)}


class OcrEngine:
    """PaddleOCR 的进程级单例包装（模型加载昂贵，只做一次）。"""

    _singleton: OcrEngine | None = None
    _lock = threading.Lock()

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine: Any = None
        self._init_seconds: float | None = None
        self._warned_non_ascii = False

    @classmethod
    def instance(cls) -> OcrEngine:
        if cls._singleton is None:
            with cls._lock:
                if cls._singleton is None:
                    cls._singleton = cls()
        return cls._singleton

    # ── 引擎加载 ──────────────────────────────────────────────────────────

    def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine

        settings = self._settings
        cache_dir = settings.ocr_cache_dir
        if cache_dir != settings.ocr_model_path and not self._warned_non_ascii:
            self._warned_non_ascii = True
            logger.warning(
                "OCR_MODEL_DIR(%s) 含非 ASCII 字符，Paddle 无法读取，已改用 %s（见 M2 记录 R-01）",
                settings.ocr_model_path,
                cache_dir,
            )
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ[MODEL_CACHE_ENV] = str(cache_dir)
        # 跳过联网连通性探测，省掉每次启动的等待
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

        started = time.perf_counter()
        try:
            from paddleocr import PaddleOCR  # noqa: PLC0415 - 重量级依赖，延迟导入
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.OCR_FAILED,
                f"PaddleOCR 导入失败：{type(exc).__name__}",
                detail={"error": str(exc)},
            ) from exc

        try:
            self._engine = PaddleOCR(
                lang=settings.ocr_lang,
                # 实测必须关闭：paddle 3.3.1 的 PIR 执行器在 oneDNN 路径上有缺陷
                enable_mkldnn=settings.ocr_enable_mkldnn,
                # 合同是纯文本版面，关掉方向分类/矫正/行方向，省时且更稳
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.OCR_FAILED,
                f"OCR 引擎初始化失败：{type(exc).__name__}",
                detail={"error": str(exc), "model_dir": str(cache_dir)},
            ) from exc

        self._init_seconds = time.perf_counter() - started
        logger.info(
            "OCR 引擎就绪：lang=%s mkldnn=%s 模型缓存=%s 初始化 %.1fs",
            settings.ocr_lang,
            settings.ocr_enable_mkldnn,
            cache_dir,
            self._init_seconds,
        )
        return self._engine

    # ── 识别 ──────────────────────────────────────────────────────────────

    def recognize(self, image: Any) -> list[OcrLine]:
        """识别一张图片，返回按阅读顺序排列的行。

        入参可以是路径（``str``/``Path``）或 **BGR ndarray**。生产路径统一走
        ndarray（由 :func:`load_image_bgr` / :func:`pixmap_to_bgr` 生成）：
        这样既不把路径交给 Paddle/OpenCV，也避免中文路径的任何潜在问题。

        失败（引擎不可用/推理异常）→ ``OCR_FAILED``（SPEC §5.4：任务转 blocked/parsing）。
        """
        engine = self._ensure_engine()
        label = str(image) if isinstance(image, (str, Path)) else f"ndarray{getattr(image, 'shape', '')}"
        started = time.perf_counter()
        try:
            raw_results = engine.predict(image)
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.OCR_FAILED,
                f"OCR 识别失败：{type(exc).__name__}",
                detail={"image": label, "error": str(exc)[:300]},
            ) from exc

        lines = _lines_from_results(raw_results)
        logger.info(
            "OCR 完成：%s → %d 行，耗时 %.1fs",
            label,
            len(lines),
            time.perf_counter() - started,
        )
        return lines


def load_image_bgr(path: Path) -> Any:
    """用 Python 读字节 + cv2 解码，得到 BGR 数组（对非 ASCII 路径免疫）。"""
    import cv2  # noqa: PLC0415 - paddleocr 的既有依赖
    import numpy as np  # noqa: PLC0415

    try:
        buffer = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    except OSError as exc:
        raise AppError(
            ErrorCode.PARSE_FAILED,
            f"合同图片读取失败：{path.name}",
            detail={"error": str(exc)},
        ) from exc
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise AppError(
            ErrorCode.PARSE_FAILED,
            f"合同图片解码失败（可能已损坏）：{path.name}",
            detail={"file_name": path.name},
        )
    return image


def pixmap_to_bgr(pixmap: Any) -> Any:
    """PyMuPDF 的 Pixmap → BGR 数组（扫描页 OCR 用）。"""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    buffer = np.frombuffer(pixmap.tobytes("png"), dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise AppError(ErrorCode.OCR_FAILED, "扫描页转图后解码失败")
    return image


def _lines_from_results(raw_results: Any) -> list[OcrLine]:
    """把 PaddleOCR 的返回结构归一化成 ``OcrLine`` 列表。

    PaddleOCR 3.x 返回 ``Result`` 对象列表，``res`` 中含
    ``rec_texts`` / ``rec_scores`` / ``rec_polys``（4 点文本框）。
    """
    if not raw_results:
        return []
    payload = raw_results[0]
    payload = getattr(payload, "json", payload)
    if isinstance(payload, dict) and "res" in payload:
        payload = payload["res"]
    if not isinstance(payload, dict):
        return []

    texts = payload.get("rec_texts") or []
    scores = payload.get("rec_scores") or []
    boxes = payload.get("rec_polys")
    if boxes is None:
        boxes = payload.get("dt_polys") or []

    lines: list[OcrLine] = []
    for index, raw_text in enumerate(texts):
        text = normalize_ocr_text(collapse_whitespace(str(raw_text)))
        if not text:
            continue
        score = float(scores[index]) if index < len(scores) else 0.0
        bbox = (0, 0)
        if index < len(boxes):
            box = boxes[index]
            box = box.tolist() if hasattr(box, "tolist") else box
            if box:
                corner = box[0]
                bbox = (int(round(corner[0])), int(round(corner[1])))
        lines.append(OcrLine(text=text, score=score, bbox=bbox))
    return lines
