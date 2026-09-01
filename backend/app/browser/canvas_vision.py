import asyncio
import json
import os
import re
import shutil
import time
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

import numpy as np

from backend.app.demo.config import CONFIG, ROOT_DIR

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

try:
    import pytesseract
    from pytesseract import Output, TesseractNotFoundError
except ImportError:  # pragma: no cover
    pytesseract = None
    Output = None
    TesseractNotFoundError = RuntimeError

try:
    from rapidocr import RapidOCR
except ImportError:  # pragma: no cover
    RapidOCR = None


CANVAS_SELECTOR = "canvas.market-grid-canvas__canvas"
MIN_ODDS = 1.01
MAX_ODDS = 100.0
OCR_MIN_CONFIDENCE = (
    CONFIG.ocr_min_confidence / 100.0
    if CONFIG.ocr_min_confidence > 1
    else CONFIG.ocr_min_confidence
)
DIAGNOSTICS_DIR = ROOT_DIR / "backend" / "diagnostics" / "canvas"
ANALYSIS_FILE = DIAGNOSTICS_DIR / "latest_analysis.json"
ODDS_RE = re.compile(r"(?<!\d)\d{1,2}[.,]\d{1,3}(?!\d)")


class CanvasVisionError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def decode_canvas_image(image_bytes: bytes):
    if cv2 is None:
        raise CanvasVisionError("CV_ENGINE_NOT_AVAILABLE", "OpenCV не установлен.")
    if not image_bytes:
        raise CanvasVisionError("CANVAS_CAPTURE_INVALID", "Canvas screenshot пуст.")
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise CanvasVisionError("CANVAS_CAPTURE_INVALID", "OpenCV не декодировал canvas PNG.")
    height, width = image.shape[:2]
    variance = float(np.var(image))
    if width < 20 or height < 20 or variance < 1.0:
        raise CanvasVisionError(
            "CANVAS_CAPTURE_INVALID",
            f"Некорректный canvas: {width}x{height}, variance={variance:.3f}",
        )
    return image


def preprocess_canvas(image) -> dict[str, Any]:
    if cv2 is None:
        raise CanvasVisionError("CV_ENGINE_NOT_AVAILABLE", "OpenCV не установлен.")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    scaled_2x = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    scaled_3x = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, otsu = cv2.threshold(scaled_2x, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, otsu_inv = cv2.threshold(
        scaled_2x, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    adaptive = cv2.adaptiveThreshold(
        scaled_2x,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        9,
    )
    contrast = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    return {
        "gray": gray,
        "scaled_2x": scaled_2x,
        "scaled_3x": scaled_3x,
        "otsu": otsu,
        "otsu_inv": otsu_inv,
        "adaptive": adaptive,
        "contrast": contrast,
    }


def parse_odds(text: str) -> list[float]:
    values = []
    for token in ODDS_RE.findall(text):
        value = float(token.replace(",", "."))
        if MIN_ODDS <= value <= MAX_ODDS:
            values.append(value)
    return values


def _tesseract_path() -> str | None:
    candidates = [
        os.getenv("TESSERACT_CMD", "").strip(),
        shutil.which("tesseract") or "",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    return next((value for value in candidates if value and Path(value).is_file()), None)


class CanvasVision:
    """Reusable OCR engines and a validated layout cache for one match page."""

    def __init__(self) -> None:
        self.rapidocr = None
        self.rapidocr_error: str | None = None
        self.tesseract_available = False
        self.tesseract_version: str | None = None
        self._initialized = False
        self._cached_market_bbox: dict[str, int] | None = None
        self._cached_mapping: dict[str, Any] | None = None
        self._cached_shape: tuple[int, int] | None = None

    def invalidate_layout(self) -> None:
        self._cached_market_bbox = None
        self._cached_mapping = None
        self._cached_shape = None

    def _recognize_cached_box(self, image, item: dict[str, Any]) -> dict[str, Any] | None:
        if self.rapidocr is None:
            return None
        margin_x, margin_y = 10, 6
        height, width = image.shape[:2]
        left = max(0, item["x"] - margin_x)
        top = max(0, item["y"] - margin_y)
        right = min(width, item["x"] + item["width"] + margin_x)
        bottom = min(height, item["y"] + item["height"] + margin_y)
        result = self.rapidocr(
            image[top:bottom, left:right],
            use_det=False,
            use_cls=False,
            use_rec=True,
        )
        if not result.txts:
            return None
        values = parse_odds(result.txts[0])
        confidence = float(result.scores[0]) if result.scores else 0.0
        if len(values) != 1 or confidence < OCR_MIN_CONFIDENCE:
            return None
        return {
            **item,
            "text": result.txts[0],
            "value": values[0],
            "confidence": round(confidence, 4),
            "engine": "RapidOCR",
            "variant": "cached_outcome_roi",
            "usable": True,
        }

    def _read_cached_mapping(self, image) -> dict[str, Any] | None:
        height, width = image.shape[:2]
        if self._cached_shape != (height, width) or not self._cached_mapping:
            return None
        team1 = self._recognize_cached_box(image, self._cached_mapping["team1"])
        team2 = self._recognize_cached_box(image, self._cached_mapping["team2"])
        if team1 is None or team2 is None:
            self.invalidate_layout()
            return None
        return {**self._cached_mapping, "team1": team1, "team2": team2}

    def _initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        if RapidOCR is not None:
            try:
                self.rapidocr = RapidOCR(params={"Global.log_level": "warning"})
            except Exception as error:  # pragma: no cover
                self.rapidocr_error = f"{type(error).__name__}: {error}"
        if pytesseract is not None:
            path = _tesseract_path()
            if path:
                try:
                    pytesseract.pytesseract.tesseract_cmd = path
                    self.tesseract_version = str(pytesseract.get_tesseract_version())
                    self.tesseract_available = True
                except (TesseractNotFoundError, OSError) as error:
                    self.tesseract_version = str(error)

    def backend_status(self) -> dict[str, Any]:
        self._initialize()
        try:
            rapid_version = package_version("rapidocr") if RapidOCR is not None else None
        except Exception:
            rapid_version = None
        return {
            "rapidocr": {
                "available": self.rapidocr is not None,
                "version": rapid_version,
                "engine": "onnxruntime" if self.rapidocr is not None else None,
                "error": self.rapidocr_error,
            },
            "tesseract": {
                "available": self.tesseract_available,
                "version": self.tesseract_version,
                "path": _tesseract_path(),
            },
        }

    @staticmethod
    def _box_dict(
        text: str,
        confidence: float,
        box: Any,
        engine: str,
        *,
        scale: float = 1.0,
        offset_y: int = 0,
        variant: str = "original",
    ) -> dict[str, Any]:
        points = np.asarray(box, dtype=float)
        left, top = points.min(axis=0)
        right, bottom = points.max(axis=0)
        return {
            "text": str(text).strip(),
            "confidence": round(float(confidence), 4),
            "x": round(left / scale),
            "y": round(top / scale) + offset_y,
            "width": max(1, round((right - left) / scale)),
            "height": max(1, round((bottom - top) / scale)),
            "engine": engine,
            "variant": variant,
        }

    def _rapid_regions(
        self,
        image,
        *,
        scale: float = 1.0,
        offset_y: int = 0,
        variant: str = "original",
    ) -> list[dict[str, Any]]:
        if self.rapidocr is None:
            return []
        result = self.rapidocr(image)
        if result is None or result.txts is None:
            return []
        return [
            self._box_dict(
                text,
                score,
                box,
                "RapidOCR",
                scale=scale,
                offset_y=offset_y,
                variant=variant,
            )
            for text, score, box in zip(result.txts, result.scores, result.boxes, strict=True)
            if str(text).strip()
        ]

    def _tesseract_regions(
        self,
        image,
        *,
        numeric_only: bool,
        scale: float = 1.0,
        offset_y: int = 0,
        variant: str = "original",
    ) -> list[dict[str, Any]]:
        if not self.tesseract_available:
            return []
        config = "--psm 6"
        language = "eng"
        if numeric_only:
            config += " -c tessedit_char_whitelist=0123456789.,"
        else:
            try:
                languages = set(pytesseract.get_languages(config=""))
                language = "rus+eng" if "rus" in languages else "eng"
            except Exception:
                pass
        data = pytesseract.image_to_data(
            image, lang=language, output_type=Output.DICT, config=config
        )
        regions = []
        for index, raw_text in enumerate(data.get("text", [])):
            text = str(raw_text).strip()
            if not text:
                continue
            try:
                confidence = max(0.0, float(data["conf"][index]) / 100.0)
            except (ValueError, TypeError, KeyError):
                confidence = 0.0
            left = float(data["left"][index])
            top = float(data["top"][index])
            width = float(data["width"][index])
            height = float(data["height"][index])
            box = [
                [left, top],
                [left + width, top],
                [left + width, top + height],
                [left, top + height],
            ]
            regions.append(
                self._box_dict(
                    text,
                    confidence,
                    box,
                    "Tesseract",
                    scale=scale,
                    offset_y=offset_y,
                    variant=variant,
                )
            )
        return regions

    def _detect(self, image, *, extended: bool = False) -> list[dict[str, Any]]:
        self._initialize()
        if self.rapidocr is None and not self.tesseract_available:
            raise CanvasVisionError(
                "OCR_ENGINE_NOT_AVAILABLE", "Ни RapidOCR, ни Tesseract не доступны."
            )

        height, width = image.shape[:2]
        if self._cached_shape == (height, width) and self._cached_market_bbox:
            roi = self._cached_market_bbox
            top = max(0, roi["y"] - 8)
            bottom = min(height, roi["y"] + roi["height"] + 8)
            regions = self._rapid_regions(
                image[top:bottom], offset_y=top, variant="cached_roi"
            )
            if regions:
                return regions

        regions = self._rapid_regions(image)
        if not regions and self.tesseract_available:
            regions = self._tesseract_regions(image, numeric_only=False)
        if regions and not extended:
            return regions

        processed = preprocess_canvas(image)
        expanded = list(regions)
        for variant, scale in (("scaled_2x", 2.0), ("contrast", 1.0), ("otsu", 2.0)):
            source = processed[variant]
            expanded.extend(self._rapid_regions(source, scale=scale, variant=variant))
            if self.tesseract_available:
                expanded.extend(
                    self._tesseract_regions(
                        source,
                        numeric_only=False,
                        scale=scale,
                        variant=variant,
                    )
                )

        chunk_height, overlap = 280, 40
        for top in range(0, height, chunk_height - overlap):
            bottom = min(height, top + chunk_height)
            expanded.extend(
                self._rapid_regions(
                    image[top:bottom], offset_y=top, variant="vertical_chunk"
                )
            )
            if bottom == height:
                break
        return _deduplicate_regions(expanded)

    def analyze_image(
        self, image, *, save: bool = True, extended: bool = False
    ) -> dict[str, Any]:
        height, width = image.shape[:2]
        if width < 20 or height < 20 or float(np.var(image)) < 1.0:
            raise CanvasVisionError("CANVAS_CAPTURE_INVALID", "Canvas не содержит pixels.")

        started = time.perf_counter()
        mapping = self._read_cached_mapping(image)
        if mapping:
            regions = [mapping["team1"], mapping["team2"]]
            numbers = regions
            next_goal_regions = []
        else:
            regions = self._detect(image, extended=extended)
            numbers = _numeric_candidates(regions)
            headers = _find_header_bands(image)
            next_goal_regions = _find_next_goal_lines(regions)
            mapping = _map_next_goal_market(regions, numbers, headers, width, height)
        latency = round(time.perf_counter() - started, 3)
        status = "CANVAS_ANALYZED"
        if not regions:
            status = "OCR_EMPTY"
        elif not mapping:
            status = "ODDS_MAPPING_UNCERTAIN"

        if mapping:
            self._cached_market_bbox = mapping["market_bbox"]
            self._cached_mapping = mapping
            self._cached_shape = (height, width)

        processed = preprocess_canvas(image)
        timestamp = _timestamp()
        paths = (
            _save_diagnostics(image, processed, numbers, regions, timestamp)
            if save
            else {}
        )
        analysis = {
            "ok": status == "CANVAS_ANALYZED",
            "status": status,
            "canvas": {
                "width": width,
                "height": height,
                "variance": round(float(np.var(image)), 3),
            },
            "numbers": numbers,
            "text_regions": regions,
            "next_goal_regions": next_goal_regions,
            "next_goal_mapping": mapping,
            "market_bbox": mapping.get("market_bbox") if mapping else None,
            "ocr_backend": _primary_engine(regions),
            "ocr_backends": self.backend_status(),
            "ocr_min_confidence": OCR_MIN_CONFIDENCE,
            "latency_seconds": latency,
            "diagnostics": paths,
            "error": None,
        }
        if save:
            _write_analysis(analysis)
        return analysis


def _primary_engine(regions: list[dict[str, Any]]) -> str | None:
    engines = Counter(item["engine"] for item in regions)
    return engines.most_common(1)[0][0] if engines else None


def _overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    x1 = max(left["x"], right["x"])
    y1 = max(left["y"], right["y"])
    x2 = min(left["x"] + left["width"], right["x"] + right["width"])
    y2 = min(left["y"] + left["height"], right["y"] + right["height"])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    smallest = min(left["width"] * left["height"], right["width"] * right["height"])
    return intersection / smallest if smallest else 0.0


def _deduplicate_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate in sorted(regions, key=lambda item: item["confidence"], reverse=True):
        duplicate = any(
            item["text"].lower() == candidate["text"].lower()
            and (
                _overlap_ratio(item, candidate) >= 0.45
                or (
                    abs(item["x"] - candidate["x"]) <= 10
                    and abs(item["y"] - candidate["y"]) <= 10
                )
            )
            for item in result
        )
        if not duplicate:
            result.append(candidate)
    return sorted(result, key=lambda item: (item["y"], item["x"]))


def _numeric_candidates(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for region in regions:
        for value in parse_odds(region["text"]):
            candidates.append(
                {
                    **region,
                    "value": value,
                    "usable": region["confidence"] >= OCR_MIN_CONFIDENCE,
                }
            )
    result: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: item["confidence"], reverse=True):
        duplicate = any(
            abs(item["value"] - candidate["value"]) < 0.0001
            and (
                _overlap_ratio(item, candidate) >= 0.35
                or (
                    abs(item["x"] - candidate["x"]) <= 12
                    and abs(item["y"] - candidate["y"]) <= 12
                )
            )
            for item in result
        )
        if not duplicate:
            result.append(candidate)
    return sorted(result, key=lambda item: (item["y"], item["x"]))


def _normalize_text(value: str) -> str:
    return " ".join(
        re.sub(r"[^a-zа-я0-9]+", " ", value.lower().replace("ё", "е")).split()
    )


def _find_next_goal_lines(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in regions:
        normalized = _normalize_text(item["text"])
        similarity = SequenceMatcher(None, normalized, "следующий гол").ratio()
        if ("следующ" in normalized and "гол" in normalized) or similarity >= 0.68:
            result.append({**item, "similarity": round(similarity, 3)})
    return result


def _find_header_bands(image) -> list[dict[str, int]]:
    height, width = image.shape[:2]
    mask = []
    for row in image:
        median = np.median(row, axis=0)
        close = np.all(
            np.abs(row.astype(np.int16) - median.astype(np.int16)) <= 10, axis=1
        )
        brightness = float(np.mean(median))
        blue_tint = float(median[0] - median[2])
        mask.append(bool(close.mean() >= 0.58 and 170 <= brightness <= 248 and blue_tint >= 7))
    bands = []
    start = None
    for index, active in enumerate(mask + [False]):
        if active and start is None:
            start = index
        elif not active and start is not None:
            if index - start >= 14:
                bands.append(
                    {"x": 0, "y": start, "width": width, "height": index - start}
                )
            start = None
    return bands


def _goal_label(region: dict[str, Any], side: int) -> int | None:
    text = _normalize_text(region["text"])
    numbers = [int(value) for value in re.findall(r"\d+", text)]
    if len(numbers) >= 2 and numbers[0] == side:
        return numbers[1]
    if f"команда {side}" in text and numbers:
        return numbers[-1]
    return None


def _map_next_goal_market(
    regions: list[dict[str, Any]],
    numbers: list[dict[str, Any]],
    headers: list[dict[str, int]],
    width: int,
    height: int,
) -> dict[str, Any] | None:
    rows: list[list[dict[str, Any]]] = []
    for item in (candidate for candidate in numbers if candidate["usable"]):
        center = item["y"] + item["height"] / 2
        row = next(
            (
                group
                for group in rows
                if abs(
                    center
                    - sum(x["y"] + x["height"] / 2 for x in group) / len(group)
                )
                <= 12
            ),
            None,
        )
        if row is None:
            rows.append([item])
        else:
            row.append(item)

    for row in sorted(rows, key=lambda group: min(item["y"] for item in group)):
        row = sorted(row, key=lambda item: item["x"])
        left = [item for item in row if item["x"] + item["width"] / 2 < width * 0.36]
        middle = [
            item
            for item in row
            if width * 0.36 <= item["x"] + item["width"] / 2 < width * 0.70
        ]
        if not left or not middle:
            continue
        first, second = left[-1], middle[-1]
        center_y = (first["y"] + second["y"]) / 2
        side1_labels = [
            item
            for item in regions
            if item["x"] < first["x"]
            and abs(item["y"] + item["height"] / 2 - center_y) <= 18
            and _goal_label(item, 1) is not None
        ]
        side2_labels = [
            item
            for item in regions
            if width * 0.30 < item["x"] < second["x"]
            and abs(item["y"] + item["height"] / 2 - center_y) <= 18
            and _goal_label(item, 2) is not None
        ]
        if not side1_labels or not side2_labels:
            continue
        goal1 = _goal_label(side1_labels[0], 1)
        goal2 = _goal_label(side2_labels[0], 2)
        if goal1 is None or goal1 != goal2:
            continue

        preceding = [band for band in headers if band["y"] + band["height"] <= center_y]
        header = preceding[-1] if preceding else None
        if header is None or center_y - (header["y"] + header["height"]) > 65:
            continue
        following = [band for band in headers if band["y"] > center_y]
        bottom = following[0]["y"] if following else min(height, int(center_y + 90))
        market_bbox = {
            "x": 0,
            "y": header["y"],
            "width": width,
            "height": max(1, bottom - header["y"]),
        }
        confidence = min(
            first["confidence"],
            second["confidence"],
            side1_labels[0]["confidence"],
            side2_labels[0]["confidence"],
        )
        return {
            "market": f"Следующий гол ({goal1})",
            "next_goal_number": goal1,
            "market_bbox": market_bbox,
            "mapping_method": "STRUCTURAL_OUTCOME_LABELS",
            "team1": first,
            "team2": second,
            "confidence": round(float(confidence), 4),
        }
    return None


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _save_diagnostics(image, processed, numbers, regions, timestamp: str) -> dict[str, str]:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    paths = {
        "original": DIAGNOSTICS_DIR / f"canvas_original_{timestamp}.png",
        "gray": DIAGNOSTICS_DIR / f"canvas_gray_{timestamp}.png",
        "scaled": DIAGNOSTICS_DIR / f"canvas_scaled_{timestamp}.png",
        "otsu": DIAGNOSTICS_DIR / f"canvas_otsu_{timestamp}.png",
        "adaptive": DIAGNOSTICS_DIR / f"canvas_adaptive_{timestamp}.png",
        "boxes": DIAGNOSTICS_DIR / f"canvas_boxes_{timestamp}.png",
    }
    cv2.imwrite(str(paths["original"]), image)
    cv2.imwrite(str(paths["gray"]), processed["gray"])
    cv2.imwrite(str(paths["scaled"]), processed["scaled_2x"])
    cv2.imwrite(str(paths["otsu"]), processed["otsu"])
    cv2.imwrite(str(paths["adaptive"]), processed["adaptive"])
    annotated = image.copy()
    for item in regions:
        cv2.rectangle(
            annotated,
            (item["x"], item["y"]),
            (item["x"] + item["width"], item["y"] + item["height"]),
            (165, 106, 39),
            1,
        )
    for item in numbers:
        cv2.rectangle(
            annotated,
            (item["x"], item["y"]),
            (item["x"] + item["width"], item["y"] + item["height"]),
            (63, 219, 147),
            2,
        )
    cv2.imwrite(str(paths["boxes"]), annotated)
    return {key: str(path.resolve()) for key, path in paths.items()}


def _write_analysis(analysis: dict[str, Any]) -> None:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    ANALYSIS_FILE.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def capture_market_canvas(page) -> dict[str, Any]:
    canvas = page.locator(CANVAS_SELECTOR).first
    await canvas.wait_for(state="visible", timeout=10_000)
    box = await canvas.bounding_box()
    if box is None:
        raise CanvasVisionError("CANVAS_NOT_READY", "Canvas не имеет bounding box.")
    image_bytes = await canvas.screenshot()
    image = await asyncio.to_thread(decode_canvas_image, image_bytes)
    return {
        "bytes": image_bytes,
        "image": image,
        "width": image.shape[1],
        "height": image.shape[0],
    }


async def analyze_market_canvas(page, *, extended: bool = False) -> dict[str, Any]:
    captured = await capture_market_canvas(page)
    return await asyncio.to_thread(
        VISION.analyze_image, captured["image"], save=True, extended=extended
    )


async def read_market_odds_from_canvas(page) -> dict[str, Any]:
    readings = []
    for index in range(2):
        extended = index > 0 and not readings[0]["ok"]
        readings.append(await analyze_market_canvas(page, extended=extended))
        if index < 1:
            await page.wait_for_timeout(150)

    signatures = []
    for item in readings:
        mapping = item.get("next_goal_mapping") or {}
        signatures.append(
            (
                round(float(mapping["team1"]["value"]), 3),
                round(float(mapping["team2"]["value"]), 3),
                mapping.get("next_goal_number"),
            )
            if mapping
            else None
        )
    confirmed = Counter(value for value in signatures if value is not None).most_common(1)
    stable = confirmed[0][0] if confirmed and confirmed[0][1] >= 2 else None
    selected = next(
        (
            item
            for item, signature in reversed(list(zip(readings, signatures, strict=True)))
            if signature == stable
        ),
        readings[-1],
    )
    selected = dict(selected)
    selected["readings"] = [
        {"status": item["status"], "signature": list(signature) if signature else None}
        for item, signature in zip(readings, signatures, strict=True)
    ]
    selected["stability"] = "ODDS_CONFIRMED" if stable else "ODDS_UNSTABLE"
    _write_analysis(selected)
    return selected


async def get_next_goal_odds(page, team1: str, team2: str) -> dict[str, Any]:
    analysis = await read_market_odds_from_canvas(page)
    mapping = analysis.get("next_goal_mapping")
    if analysis["status"] != "CANVAS_ANALYZED" or not mapping:
        return {
            "ok": False,
            "status": analysis["status"],
            "source": "CANVAS_OCR",
            "confidence": None,
            "analysis": analysis,
        }
    if analysis["stability"] != "ODDS_CONFIRMED":
        return {
            "ok": False,
            "status": "ODDS_UNSTABLE",
            "source": "CANVAS_OCR",
            "confidence": None,
            "analysis": analysis,
        }
    return {
        "ok": True,
        "status": "ODDS_READY",
        "source": "CANVAS_OCR",
        "ocr_backend": analysis.get("ocr_backend"),
        "confidence": mapping["confidence"],
        "market": mapping["market"],
        "next_goal_number": mapping["next_goal_number"],
        "team1": {"name": team1, "odds": mapping["team1"]["value"]},
        "team2": {"name": team2, "odds": mapping["team2"]["value"]},
        "analysis": analysis,
    }


def load_latest_analysis() -> dict[str, Any] | None:
    if not ANALYSIS_FILE.exists():
        return None
    try:
        return json.loads(ANALYSIS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


VISION = CanvasVision()
