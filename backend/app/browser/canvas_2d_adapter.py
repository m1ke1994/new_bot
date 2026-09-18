from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

from backend.app.demo.models import NextGoalOdds
from xbet_config import SELECTORS

from . import market as hybrid_market


Logger = Callable[[str, str], Awaitable[Any]]

CANVAS_SELECTOR = SELECTORS.canvas or "canvas.market-grid-canvas__canvas"
MIN_ODDS = 1.01
MAX_ODDS = 100.0
ODDS_RE = re.compile(r"^\s*(\d{1,2}(?:[.,]\d{1,3})?)\s*$")
LOCK_WORD_RE = re.compile(r"lock|locked|blocked|closed|disable|forbid|замок|заблок", re.I)
PRIVATE_GLYPH_RE = re.compile(r"[\ue000-\uf8ff]")
HOOKED_PAGE_IDS: set[int] = set()


CANVAS_2D_HOOK_SCRIPT = r"""
(() => {
    if (window.__autobetCanvas2D && window.__autobetCanvas2D.version === 2) {
        return;
    }

    const state = {
        version: 2,
        seq: 0,
        nextCanvasId: 1,
        canvases: new Map(),
        diagnostics: {
            installed_at: Date.now(),
            calls: {},
            contexts: {},
            transfer_control_to_offscreen: 0,
            create_image_bitmap: 0,
            offscreen_canvas_available: typeof OffscreenCanvas !== "undefined",
            offscreen_2d_available: typeof OffscreenCanvasRenderingContext2D !== "undefined",
        },
        recentCalls: [],
    };

    const maxItems = 2500;
    const maxRecentCalls = 80;

    const bump = (name) => {
        state.diagnostics.calls[name] = (state.diagnostics.calls[name] || 0) + 1;
    };

    const rememberCall = (method, details = {}) => {
        state.recentCalls.push({
            seq: ++state.seq,
            method,
            ...details,
        });
        if (state.recentCalls.length > maxRecentCalls) {
            state.recentCalls.splice(0, state.recentCalls.length - maxRecentCalls);
        }
    };

    const trim = (items) => {
        if (items.length > maxItems) {
            items.splice(0, items.length - maxItems);
        }
    };

    const canvasState = (canvas) => {
        if (!canvas) return null;
        let id = canvas.__autobetCanvas2DId;
        if (!id) {
            id = state.nextCanvasId++;
            try {
                Object.defineProperty(canvas, "__autobetCanvas2DId", {
                    value: id,
                    configurable: true,
                });
            } catch (_) {
                canvas.__autobetCanvas2DId = id;
            }
        }
        let item = state.canvases.get(id);
        if (!item) {
            item = {
                id,
                generation: 0,
                texts: [],
                rects: [],
                images: [],
                width: canvas.width || 0,
                height: canvas.height || 0,
            };
            state.canvases.set(id, item);
        }
        if (item.width !== canvas.width || item.height !== canvas.height) {
            item.generation += 1;
            item.texts = [];
            item.rects = [];
            item.images = [];
            item.width = canvas.width || 0;
            item.height = canvas.height || 0;
        }
        return item;
    };

    const point = (matrix, x, y) => ({
        x: matrix.a * x + matrix.c * y + matrix.e,
        y: matrix.b * x + matrix.d * y + matrix.f,
    });

    const bounds = (ctx, points) => {
        const matrix = ctx.getTransform();
        const mapped = points.map(([x, y]) => point(matrix, Number(x) || 0, Number(y) || 0));
        const xs = mapped.map((p) => p.x);
        const ys = mapped.map((p) => p.y);
        const left = Math.min(...xs);
        const top = Math.min(...ys);
        const right = Math.max(...xs);
        const bottom = Math.max(...ys);
        return {
            x: left,
            y: top,
            width: Math.max(1, right - left),
            height: Math.max(1, bottom - top),
        };
    };

    const rectBounds = (ctx, x, y, width, height) => bounds(ctx, [
        [x, y],
        [x + width, y],
        [x + width, y + height],
        [x, y + height],
    ]);

    const textBounds = (ctx, text, x, y) => {
        let metrics;
        try {
            metrics = ctx.measureText(String(text));
        } catch (_) {
            metrics = null;
        }
        const fontMatch = /([\d.]+)px/.exec(String(ctx.font || ""));
        const fontPx = fontMatch ? Number(fontMatch[1]) : 14;
        const left = metrics && Number.isFinite(metrics.actualBoundingBoxLeft)
            ? metrics.actualBoundingBoxLeft
            : 0;
        const right = metrics && Number.isFinite(metrics.actualBoundingBoxRight)
            ? metrics.actualBoundingBoxRight
            : (metrics && Number.isFinite(metrics.width) ? metrics.width : fontPx * String(text).length * 0.55);
        const ascent = metrics && Number.isFinite(metrics.actualBoundingBoxAscent) && metrics.actualBoundingBoxAscent > 0
            ? metrics.actualBoundingBoxAscent
            : fontPx * 0.8;
        const descent = metrics && Number.isFinite(metrics.actualBoundingBoxDescent)
            ? metrics.actualBoundingBoxDescent
            : fontPx * 0.2;
        return bounds(ctx, [
            [x - left, y - ascent],
            [x + right, y - ascent],
            [x + right, y + descent],
            [x - left, y + descent],
        ]);
    };

    const anchorPoint = (ctx, x, y) => {
        const matrix = ctx.getTransform();
        return point(matrix, Number(x) || 0, Number(y) || 0);
    };

    const recordText = (ctx, text, x, y, kind) => {
        bump(kind);
        rememberCall(kind, {
            text: String(text),
            x: Number(x) || 0,
            y: Number(y) || 0,
            context: ctx && ctx.constructor ? ctx.constructor.name : "unknown",
        });
        const item = canvasState(ctx.canvas);
        if (!item) return;
        const box = textBounds(ctx, text, x, y);
        const anchor = anchorPoint(ctx, x, y);
        item.texts.push({
            seq: ++state.seq,
            generation: item.generation,
            kind,
            text: String(text),
            x: box.x,
            y: box.y,
            width: box.width,
            height: box.height,
            anchor_x: anchor.x,
            anchor_y: anchor.y,
            font: String(ctx.font || ""),
            fill_style: String(ctx.fillStyle ?? ""),
            stroke_style: String(ctx.strokeStyle ?? ""),
            alpha: Number(ctx.globalAlpha ?? 1),
        });
        trim(item.texts);
    };

    const recordRect = (ctx, x, y, width, height, kind) => {
        bump(kind);
        const item = canvasState(ctx.canvas);
        if (!item) return;
        const box = rectBounds(ctx, Number(x) || 0, Number(y) || 0, Number(width) || 0, Number(height) || 0);
        item.rects.push({
            seq: ++state.seq,
            generation: item.generation,
            kind,
            x: box.x,
            y: box.y,
            width: box.width,
            height: box.height,
            fill_style: String(ctx.fillStyle ?? ""),
            stroke_style: String(ctx.strokeStyle ?? ""),
            alpha: Number(ctx.globalAlpha ?? 1),
        });
        trim(item.rects);
    };

    const sourceMeta = (image) => {
        if (!image) return "";
        const parts = [];
        for (const key of ["currentSrc", "src", "id", "alt", "title"]) {
            try {
                const value = image[key];
                if (value) parts.push(String(value));
            } catch (_) {}
        }
        try {
            if (image.className) {
                const classValue = typeof image.className === "string"
                    ? image.className
                    : image.className.baseVal;
                if (classValue) parts.push(String(classValue));
            }
        } catch (_) {}
        try {
            if (image.tagName) parts.push(String(image.tagName));
        } catch (_) {}
        try {
            if (image.constructor && image.constructor.name) parts.push(String(image.constructor.name));
        } catch (_) {}
        return parts.join(" ");
    };

    const recordImage = (ctx, args) => {
        bump("drawImage");
        const item = canvasState(ctx.canvas);
        if (!item || !args.length) return;
        const image = args[0];
        let dx = 0, dy = 0, dw = 0, dh = 0;
        if (args.length >= 9) {
            dx = Number(args[5]) || 0;
            dy = Number(args[6]) || 0;
            dw = Number(args[7]) || 0;
            dh = Number(args[8]) || 0;
        } else if (args.length >= 5) {
            dx = Number(args[1]) || 0;
            dy = Number(args[2]) || 0;
            dw = Number(args[3]) || 0;
            dh = Number(args[4]) || 0;
        } else if (args.length >= 3) {
            dx = Number(args[1]) || 0;
            dy = Number(args[2]) || 0;
            try {
                dw = Number(image.naturalWidth || image.videoWidth || image.width) || 0;
                dh = Number(image.naturalHeight || image.videoHeight || image.height) || 0;
            } catch (_) {}
        }
        if (dw <= 0 || dh <= 0) return;
        rememberCall("drawImage", {
            source: sourceMeta(image),
            x: dx,
            y: dy,
            width: dw,
            height: dh,
            context: ctx && ctx.constructor ? ctx.constructor.name : "unknown",
        });
        const box = rectBounds(ctx, dx, dy, dw, dh);
        item.images.push({
            seq: ++state.seq,
            generation: item.generation,
            kind: "drawImage",
            x: box.x,
            y: box.y,
            width: box.width,
            height: box.height,
            source: sourceMeta(image),
            alpha: Number(ctx.globalAlpha ?? 1),
        });
        trim(item.images);
    };

    const patchMethod = (proto, name, wrapper) => {
        if (!proto) return;
        const original = proto[name];
        if (typeof original !== "function") return;
        if (original.__autobetCanvas2DWrapped) return;
        const wrapped = function(...args) {
            return wrapper.call(this, original, args);
        };
        try {
            Object.defineProperty(wrapped, "__autobetCanvas2DWrapped", { value: true });
        } catch (_) {
            wrapped.__autobetCanvas2DWrapped = true;
        }
        proto[name] = wrapped;
    };

    const patch2DPrototype = (proto) => {
        if (!proto) return;

        patchMethod(proto, "fillText", function(original, args) {
        try { recordText(this, args[0], args[1], args[2], "fillText"); } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "strokeText", function(original, args) {
        try { recordText(this, args[0], args[1], args[2], "strokeText"); } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "fillRect", function(original, args) {
        try { recordRect(this, args[0], args[1], args[2], args[3], "fillRect"); } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "strokeRect", function(original, args) {
        try { recordRect(this, args[0], args[1], args[2], args[3], "strokeRect"); } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "rect", function(original, args) {
        try { recordRect(this, args[0], args[1], args[2], args[3], "rect"); } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "roundRect", function(original, args) {
        try { recordRect(this, args[0], args[1], args[2], args[3], "roundRect"); } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "drawImage", function(original, args) {
        try { recordImage(this, args); } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "clearRect", function(original, args) {
        try {
            const item = canvasState(this.canvas);
            if (item) {
                const box = rectBounds(this, Number(args[0]) || 0, Number(args[1]) || 0, Number(args[2]) || 0, Number(args[3]) || 0);
                const canvasArea = Math.max(1, Number(this.canvas.width || 0) * Number(this.canvas.height || 0));
                const clearArea = Math.max(0, box.width * box.height);
                if (clearArea / canvasArea >= 0.35) {
                    item.generation += 1;
                    item.texts = [];
                    item.rects = [];
                    item.images = [];
                }
            }
        } catch (_) {}
        return original.apply(this, args);
        });

        patchMethod(proto, "putImageData", function(original, args) {
            bump("putImageData");
            const image = args[0];
            rememberCall("putImageData", {
                x: Number(args[1]) || 0,
                y: Number(args[2]) || 0,
                width: image && Number(image.width) ? Number(image.width) : 0,
                height: image && Number(image.height) ? Number(image.height) : 0,
                context: this && this.constructor ? this.constructor.name : "unknown",
            });
            return original.apply(this, args);
        });

        for (const method of [
            "beginPath", "moveTo", "lineTo", "bezierCurveTo", "quadraticCurveTo",
            "arc", "arcTo", "ellipse", "fill", "stroke", "clip",
        ]) {
            patchMethod(proto, method, function(original, args) {
                bump(method);
                return original.apply(this, args);
            });
        }
    };

    patch2DPrototype(
        window.CanvasRenderingContext2D
            ? CanvasRenderingContext2D.prototype
            : null
    );
    patch2DPrototype(
        window.OffscreenCanvasRenderingContext2D
            ? OffscreenCanvasRenderingContext2D.prototype
            : null
    );

    if (window.HTMLCanvasElement && HTMLCanvasElement.prototype) {
        patchMethod(
            HTMLCanvasElement.prototype,
            "getContext",
            function(original, args) {
                const type = String(args[0] || "unknown").toLowerCase();
                state.diagnostics.contexts[type] =
                    (state.diagnostics.contexts[type] || 0) + 1;
                rememberCall("getContext", {
                    type,
                    width: Number(this.width || 0),
                    height: Number(this.height || 0),
                });
                return original.apply(this, args);
            }
        );

        patchMethod(
            HTMLCanvasElement.prototype,
            "transferControlToOffscreen",
            function(original, args) {
                state.diagnostics.transfer_control_to_offscreen += 1;
                bump("transferControlToOffscreen");
                rememberCall("transferControlToOffscreen", {
                    width: Number(this.width || 0),
                    height: Number(this.height || 0),
                });
                return original.apply(this, args);
            }
        );
    }

    if (window.OffscreenCanvas && OffscreenCanvas.prototype) {
        patchMethod(
            OffscreenCanvas.prototype,
            "getContext",
            function(original, args) {
                const type = String(args[0] || "unknown").toLowerCase();
                const key = "offscreen:" + type;
                state.diagnostics.contexts[key] =
                    (state.diagnostics.contexts[key] || 0) + 1;
                rememberCall("offscreen.getContext", {
                    type,
                    width: Number(this.width || 0),
                    height: Number(this.height || 0),
                });
                return original.apply(this, args);
            }
        );
    }

    if (typeof window.createImageBitmap === "function") {
        const originalCreateImageBitmap = window.createImageBitmap;
        if (!originalCreateImageBitmap.__autobetCanvas2DWrapped) {
            const wrappedCreateImageBitmap = function(...args) {
                state.diagnostics.create_image_bitmap += 1;
                bump("createImageBitmap");
                rememberCall("createImageBitmap", {
                    source: sourceMeta(args[0]),
                });
                return originalCreateImageBitmap.apply(this, args);
            };
            try {
                Object.defineProperty(
                    wrappedCreateImageBitmap,
                    "__autobetCanvas2DWrapped",
                    { value: true }
                );
            } catch (_) {
                wrappedCreateImageBitmap.__autobetCanvas2DWrapped = true;
            }
            window.createImageBitmap = wrappedCreateImageBitmap;
        }
    }

    const latestTextByAnchor = (items) => {
        const latest = new Map();
        for (const item of items) {
            const key = `${Math.round(item.anchor_x * 2) / 2}:${Math.round(item.anchor_y * 2) / 2}:${item.kind}`;
            const previous = latest.get(key);
            if (!previous || previous.seq < item.seq) latest.set(key, item);
        }
        return Array.from(latest.values()).sort((a, b) => a.seq - b.seq);
    };

    state.snapshot = (selector) => {
        let canvas = null;
        try {
            canvas = document.querySelector(selector);
        } catch (_) {}
        if (!canvas) {
            for (const candidate of document.querySelectorAll("canvas")) {
                if (candidate.__autobetCanvas2DId) {
                    canvas = candidate;
                    break;
                }
            }
        }
        if (!canvas) {
            return {
                hooked: true,
                status: "CANVAS_NOT_FOUND",
                canvas: null,
                texts: [],
                rects: [],
                images: [],
            };
        }

        const item = canvasState(canvas);
        const generation = item ? item.generation : 0;
        const rect = canvas.getBoundingClientRect();
        const sameGeneration = (entry) => entry.generation === generation;

        return {
            hooked: true,
            hook_version: state.version,
            status: "READY",
            generation,
            canvas: {
                id: item ? item.id : null,
                width: Number(canvas.width || 0),
                height: Number(canvas.height || 0),
                css_width: Number(rect.width || 0),
                css_height: Number(rect.height || 0),
            },
            texts: item ? latestTextByAnchor(item.texts.filter(sameGeneration)) : [],
            rects: item ? item.rects.filter(sameGeneration).slice(-1200) : [],
            images: item ? item.images.filter(sameGeneration).slice(-600) : [],
            diagnostics: {
                ...state.diagnostics,
                calls: { ...state.diagnostics.calls },
                contexts: { ...state.diagnostics.contexts },
                canvases_seen: state.canvases.size,
                recent_calls: state.recentCalls.slice(-40),
            },
        };
    };

    window.__autobetCanvas2D = state;
})();
"""


@dataclass(frozen=True)
class Canvas2DMarketMapping:
    next_goal_number: int
    team1_odds: float
    team2_odds: float
    team1_region: dict[str, float]
    team2_region: dict[str, float]
    team1_text_region: dict[str, Any]
    team2_text_region: dict[str, Any]
    canvas: dict[str, Any]


@dataclass(frozen=True)
class _CachedMarket:
    url: str
    next_goal_number: int
    team1_odds: float
    team2_odds: float
    team1_region: dict[str, float]
    team2_region: dict[str, float]
    canvas: dict[str, Any]


_LAST_MARKETS: dict[int, _CachedMarket] = {}
_LAST_DIAGNOSTIC_SIGNATURES: dict[int, str] = {}


async def _log(logger: Logger | None, event: str, message: str) -> None:
    if logger is not None:
        await logger(event, message)


def _clean_text(value: str) -> str:
    return " ".join(
        str(value)
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
        .casefold()
        .split()
    )


def _parse_odds(value: str) -> float | None:
    match = ODDS_RE.fullmatch(str(value).replace("\xa0", " "))
    if match is None:
        return None
    odds = float(match.group(1).replace(",", "."))
    if not (MIN_ODDS <= odds <= MAX_ODDS):
        return None
    return odds


def _center_y(item: dict[str, Any]) -> float:
    return float(item.get("y") or 0.0) + float(item.get("height") or 0.0) / 2.0


def _center_x(item: dict[str, Any]) -> float:
    return float(item.get("x") or 0.0) + float(item.get("width") or 0.0) / 2.0


def _goal_from_text(value: str, side: int) -> int | None:
    text = _clean_text(value)
    if "гол" not in text:
        return None

    patterns = (
        rf"команда\s*{side}\b.*?(\d+)\s*-?\s*[йяе]?\s*гол",
        rf"\b{side}\s*команда\b.*?(\d+)\s*-?\s*[йяе]?\s*гол",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is not None:
            return int(match.group(1))

    numbers = [int(number) for number in re.findall(r"\d+", text)]
    if len(numbers) >= 2 and numbers[0] == side:
        return numbers[1]
    return None


def _row_groups(
    texts: list[dict[str, Any]],
    *,
    tolerance: float,
) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for item in sorted(texts, key=lambda entry: (_center_y(entry), _center_x(entry))):
        center = _center_y(item)
        row = next(
            (
                candidate
                for candidate in rows
                if abs(
                    center
                    - sum(_center_y(entry) for entry in candidate) / len(candidate)
                )
                <= tolerance
            ),
            None,
        )
        if row is None:
            rows.append([item])
        else:
            row.append(item)
    return rows


def _button_region(
    odds_region: dict[str, Any],
    rects: list[dict[str, Any]],
    *,
    canvas_width: float,
    canvas_height: float,
) -> dict[str, float]:
    cx = _center_x(odds_region)
    cy = _center_y(odds_region)
    text_width = max(1.0, float(odds_region.get("width") or 1.0))
    text_height = max(1.0, float(odds_region.get("height") or 1.0))

    candidates: list[dict[str, Any]] = []
    for rect in rects:
        width = float(rect.get("width") or 0.0)
        height = float(rect.get("height") or 0.0)
        if width < text_width + 4 or height < text_height + 4:
            continue
        if width > canvas_width * 0.38 or height > max(140.0, canvas_height * 0.16):
            continue
        left = float(rect.get("x") or 0.0)
        top = float(rect.get("y") or 0.0)
        if left <= cx <= left + width and top <= cy <= top + height:
            candidates.append(rect)

    if candidates:
        selected = min(
            candidates,
            key=lambda item: float(item.get("width") or 0.0)
            * float(item.get("height") or 0.0),
        )
        return {
            "x": float(selected["x"]),
            "y": float(selected["y"]),
            "width": float(selected["width"]),
            "height": float(selected["height"]),
        }

    pad_x = max(18.0, text_width * 0.55)
    pad_y = max(10.0, text_height * 0.60)
    x = max(0.0, float(odds_region.get("x") or 0.0) - pad_x)
    y = max(0.0, float(odds_region.get("y") or 0.0) - pad_y)
    width = min(canvas_width - x, text_width + pad_x * 2.0)
    height = min(canvas_height - y, text_height + pad_y * 2.0)
    return {
        "x": x,
        "y": y,
        "width": max(1.0, width),
        "height": max(1.0, height),
    }


def map_next_goal_snapshot(
    snapshot: dict[str, Any],
    expected_goal_number: int,
) -> Canvas2DMarketMapping | None:
    canvas = snapshot.get("canvas") or {}
    width = float(canvas.get("width") or 0.0)
    height = float(canvas.get("height") or 0.0)
    if width <= 0 or height <= 0:
        return None

    texts = [
        item
        for item in (snapshot.get("texts") or [])
        if str(item.get("text") or "").strip()
        and float(item.get("alpha") if item.get("alpha") is not None else 1.0) > 0.03
    ]
    rects = list(snapshot.get("rects") or [])
    if not texts:
        return None

    tolerance = max(12.0, min(28.0, height * 0.025))
    for row in _row_groups(texts, tolerance=tolerance):
        row = sorted(row, key=_center_x)
        numeric: list[tuple[dict[str, Any], float]] = []
        for item in row:
            odds = _parse_odds(str(item.get("text") or ""))
            if odds is not None:
                numeric.append((item, odds))
        if len(numeric) < 2:
            continue

        left = [pair for pair in numeric if _center_x(pair[0]) < width * 0.36]
        middle = [
            pair
            for pair in numeric
            if width * 0.36 <= _center_x(pair[0]) < width * 0.70
        ]
        if not left or not middle:
            continue

        team1_text, team1_odds = left[-1]
        team2_text, team2_odds = middle[-1]
        center = (_center_y(team1_text) + _center_y(team2_text)) / 2.0

        side1_parts = [
            str(item.get("text") or "")
            for item in row
            if float(item.get("x") or 0.0) < float(team1_text.get("x") or 0.0)
            and abs(_center_y(item) - center) <= tolerance
            and _parse_odds(str(item.get("text") or "")) is None
        ]
        side2_parts = [
            str(item.get("text") or "")
            for item in row
            if width * 0.30 < float(item.get("x") or 0.0) < float(team2_text.get("x") or 0.0)
            and abs(_center_y(item) - center) <= tolerance
            and _parse_odds(str(item.get("text") or "")) is None
        ]

        goal1 = _goal_from_text(" ".join(side1_parts), 1)
        goal2 = _goal_from_text(" ".join(side2_parts), 2)
        if goal1 != expected_goal_number or goal2 != expected_goal_number:
            continue

        return Canvas2DMarketMapping(
            next_goal_number=expected_goal_number,
            team1_odds=team1_odds,
            team2_odds=team2_odds,
            team1_region=_button_region(
                team1_text,
                rects,
                canvas_width=width,
                canvas_height=height,
            ),
            team2_region=_button_region(
                team2_text,
                rects,
                canvas_width=width,
                canvas_height=height,
            ),
            team1_text_region=dict(team1_text),
            team2_text_region=dict(team2_text),
            canvas=dict(canvas),
        )
    return None


def _lock_text_reason(value: str) -> str | None:
    text = str(value or "")
    if not text:
        return None
    if any(symbol in text for symbol in ("🔒", "🔐")):
        return "lock-symbol"
    if LOCK_WORD_RE.search(text):
        return "lock-text"
    if len(text) <= 2 and PRIVATE_GLYPH_RE.search(text):
        return "private-icon-glyph"
    return None


def _rect_intersects_marker(
    region: dict[str, float],
    marker: dict[str, Any],
) -> bool:
    rx = float(region.get("x") or 0.0)
    ry = float(region.get("y") or 0.0)
    rw = float(region.get("width") or 0.0)
    rh = float(region.get("height") or 0.0)
    mx = _center_x(marker)
    my = _center_y(marker)
    pad_x = max(8.0, rw * 0.10)
    pad_y = max(6.0, rh * 0.20)
    return (
        rx - pad_x <= mx <= rx + rw + pad_x
        and ry - pad_y <= my <= ry + rh + pad_y
    )


def detect_lock_state(
    snapshot: dict[str, Any],
    *,
    team1_region: dict[str, float],
    team2_region: dict[str, float],
) -> dict[str, Any]:
    markers: list[dict[str, Any]] = []
    for item in snapshot.get("texts") or []:
        reason = _lock_text_reason(str(item.get("text") or ""))
        if reason is not None:
            markers.append({**item, "reason": reason, "source": "text"})

    for item in snapshot.get("images") or []:
        source = str(item.get("source") or "")
        if LOCK_WORD_RE.search(source):
            markers.append({**item, "reason": "lock-image", "source": source})

    result: dict[str, Any] = {}
    for side, region in ((1, team1_region), (2, team2_region)):
        candidates = [
            marker
            for marker in markers
            if _rect_intersects_marker(region, marker)
        ]
        if candidates:
            selected = max(candidates, key=lambda marker: int(marker.get("seq") or 0))
            result[str(side)] = {
                "x": float(selected.get("x") or 0.0),
                "y": float(selected.get("y") or 0.0),
                "width": float(selected.get("width") or 0.0),
                "height": float(selected.get("height") or 0.0),
                "reason": selected.get("reason"),
                "text": selected.get("text"),
                "source": selected.get("source"),
            }

    return {
        "locked_sides": tuple(sorted(int(side) for side in result)),
        "markers": result,
    }


class Canvas2DCoefficientLocator:
    """Click one Canvas outcome using a draw-call-derived internal rectangle."""

    def __init__(
        self,
        page: Page,
        region: dict[str, Any],
        canvas_shape: dict[str, Any],
    ) -> None:
        self.page = page
        self.region = dict(region)
        self.canvas_width = max(1.0, float(canvas_shape.get("width") or 1.0))
        self.canvas_height = max(1.0, float(canvas_shape.get("height") or 1.0))

    def _position(self, box: dict[str, float]) -> dict[str, float]:
        center_x = float(self.region["x"]) + float(self.region["width"]) / 2.0
        center_y = float(self.region["y"]) + float(self.region["height"]) / 2.0
        x = center_x * float(box["width"]) / self.canvas_width
        y = center_y * float(box["height"]) / self.canvas_height
        return {
            "x": min(max(x, 1.0), max(1.0, float(box["width"]) - 1.0)),
            "y": min(max(y, 1.0), max(1.0, float(box["height"]) - 1.0)),
        }

    async def click(self, **kwargs: Any) -> None:
        canvas = self.page.locator(CANVAS_SELECTOR).first
        await canvas.wait_for(
            state="visible",
            timeout=int(kwargs.pop("timeout", 10_000)),
        )
        await canvas.scroll_into_view_if_needed()
        box = await canvas.bounding_box()
        if box is None:
            raise hybrid_market.MarketNotAvailable(
                "Canvas исчез перед кликом по коэффициенту.",
                status="CANVAS_NOT_READY",
                details={"source": "CANVAS_2D"},
            )
        await canvas.click(position=self._position(box), **kwargs)


async def ensure_canvas_2d_hook(page: Page) -> None:
    page_id = id(page)
    if page_id not in HOOKED_PAGE_IDS:
        await page.add_init_script(CANVAS_2D_HOOK_SCRIPT)
        HOOKED_PAGE_IDS.add(page_id)
    await page.evaluate(CANVAS_2D_HOOK_SCRIPT)


async def capture_canvas_2d_snapshot(page: Page) -> dict[str, Any]:
    await ensure_canvas_2d_hook(page)
    return await page.evaluate(
        """async (selector) => {
            await new Promise((resolve) => {
                requestAnimationFrame(() => setTimeout(resolve, 0));
            });
            const api = window.__autobetCanvas2D;
            if (!api || typeof api.snapshot !== 'function') {
                return {
                    hooked: false,
                    status: 'HOOK_NOT_AVAILABLE',
                    canvas: null,
                    texts: [],
                    rects: [],
                    images: [],
                    diagnostics: {},
                };
            }
            return api.snapshot(selector);
        }""",
        CANVAS_SELECTOR,
    )


async def _capture_mapping(
    page: Page,
    expected_goal_number: int,
) -> tuple[dict[str, Any], Canvas2DMarketMapping | None]:
    last_snapshot: dict[str, Any] = {}
    for _ in range(4):
        snapshot = await capture_canvas_2d_snapshot(page)
        last_snapshot = snapshot
        mapping = map_next_goal_snapshot(snapshot, expected_goal_number)
        if mapping is not None:
            return snapshot, mapping
        await page.wait_for_timeout(35)
    return last_snapshot, None


async def _read_dom_only(
    page: Page,
    team1: str,
    team2: str,
    next_goal_number: int,
    logger: Logger | None,
) -> NextGoalOdds:
    return await hybrid_market._read_next_goal_odds_dom(
        page,
        team1,
        team2,
        next_goal_number,
        logger,
    )


def _cached_market_is_usable(
    page: Page,
    cached: _CachedMarket | None,
    next_goal_number: int,
    snapshot: dict[str, Any],
) -> bool:
    if cached is None or cached.next_goal_number != next_goal_number:
        return False
    if cached.url != page.url:
        return False
    canvas = snapshot.get("canvas") or {}
    return (
        int(float(canvas.get("width") or 0))
        == int(float(cached.canvas.get("width") or 0))
        and int(float(canvas.get("height") or 0))
        == int(float(cached.canvas.get("height") or 0))
    )


def _diagnostic_signature(snapshot: dict[str, Any]) -> str:
    diagnostics = snapshot.get("diagnostics") or {}
    calls = diagnostics.get("calls") or {}
    contexts = diagnostics.get("contexts") or {}
    recent = diagnostics.get("recent_calls") or []
    recent_methods = [str(item.get("method") or "") for item in recent[-8:]]
    return repr(
        (
            tuple(sorted((str(key), int(value)) for key, value in calls.items())),
            tuple(sorted((str(key), int(value)) for key, value in contexts.items())),
            int(diagnostics.get("transfer_control_to_offscreen") or 0),
            int(diagnostics.get("create_image_bitmap") or 0),
            tuple(recent_methods),
        )
    )


def _diagnostic_message(snapshot: dict[str, Any]) -> str:
    diagnostics = snapshot.get("diagnostics") or {}
    calls = diagnostics.get("calls") or {}
    contexts = diagnostics.get("contexts") or {}
    recent = diagnostics.get("recent_calls") or []
    sample_parts: list[str] = []
    for item in recent[-12:]:
        method = str(item.get("method") or "?")
        if method in {"fillText", "strokeText"}:
            sample_parts.append(
                f"{method}({str(item.get('text') or '')!r}@"
                f"{item.get('x')},{item.get('y')})"
            )
        elif method in {
            "drawImage",
            "putImageData",
            "transferControlToOffscreen",
            "getContext",
            "offscreen.getContext",
            "createImageBitmap",
        }:
            sample_parts.append(f"{method}({item})")

    return (
        f"hook_v={snapshot.get('hook_version') or diagnostics.get('version') or 2}; "
        f"status={snapshot.get('status')}; "
        f"contexts={contexts}; calls={calls}; "
        f"offscreen_available={diagnostics.get('offscreen_canvas_available')}; "
        f"offscreen_2d_available={diagnostics.get('offscreen_2d_available')}; "
        f"transfer_offscreen={diagnostics.get('transfer_control_to_offscreen', 0)}; "
        f"createImageBitmap={diagnostics.get('create_image_bitmap', 0)}; "
        f"canvases_seen={diagnostics.get('canvases_seen', 0)}; "
        f"texts={len(snapshot.get('texts') or [])}; "
        f"rects={len(snapshot.get('rects') or [])}; "
        f"images={len(snapshot.get('images') or [])}; "
        f"recent=[{' | '.join(sample_parts)}]"
    )


async def _log_canvas_diagnostics(
    page: Page,
    snapshot: dict[str, Any],
    logger: Logger | None,
    *,
    force: bool = False,
) -> None:
    if logger is None:
        return
    signature = _diagnostic_signature(snapshot)
    page_id = id(page)
    if not force and _LAST_DIAGNOSTIC_SIGNATURES.get(page_id) == signature:
        return
    _LAST_DIAGNOSTIC_SIGNATURES[page_id] = signature
    await _log(
        logger,
        "CANVAS_2D_DIAGNOSTICS",
        _diagnostic_message(snapshot),
    )


async def read_next_goal_odds(
    page: Page,
    team1: str,
    team2: str,
    score1: int,
    score2: int,
    logger: Logger | None = None,
    *,
    read_only: bool = False,
) -> NextGoalOdds:
    del read_only
    next_goal_number = score1 + score2 + 1
    await ensure_canvas_2d_hook(page)

    try:
        await hybrid_market._prepare_market_search(
            page,
            hybrid_market.NEXT_GOAL_SEARCH_TEXT,
            logger,
        )
    except hybrid_market.MarketReadError as search_error:
        try:
            return await _read_dom_only(
                page,
                team1,
                team2,
                next_goal_number,
                logger,
            )
        except hybrid_market.MarketReadError:
            raise search_error

    canvas = page.locator(CANVAS_SELECTOR).first
    try:
        canvas_ready = bool(await canvas.count() and await canvas.is_visible())
    except Exception:
        canvas_ready = False

    if not canvas_ready:
        return await _read_dom_only(
            page,
            team1,
            team2,
            next_goal_number,
            logger,
        )

    snapshot, mapping = await _capture_mapping(page, next_goal_number)
    await _log_canvas_diagnostics(page, snapshot, logger)
    cache_key = id(page)
    cached = _LAST_MARKETS.get(cache_key)

    if mapping is None and not (snapshot.get("texts") or []):
        await _log(
            logger,
            "CANVAS_2D_NO_DRAW_CALLS",
            "Canvas виден, но fillText/strokeText ещё не перехвачены; принудительно обновляем фильтр рынка.",
        )
        search_input, _, _ = await hybrid_market._locate_market_search_input(
            page,
            timeout_ms=500,
        )
        if search_input is not None:
            try:
                await search_input.fill("")
                await search_input.fill(hybrid_market.NEXT_GOAL_SEARCH_TEXT)
                await page.wait_for_timeout(80)
                snapshot, mapping = await _capture_mapping(page, next_goal_number)
                await _log_canvas_diagnostics(
                    page,
                    snapshot,
                    logger,
                    force=True,
                )
            except Exception:
                pass

    if mapping is None:
        if _cached_market_is_usable(page, cached, next_goal_number, snapshot):
            assert cached is not None
            lock_state = detect_lock_state(
                snapshot,
                team1_region=cached.team1_region,
                team2_region=cached.team2_region,
            )
            if lock_state["locked_sides"]:
                await _log(
                    logger,
                    "CANVAS_2D_LOCKED_USING_LAST_ODDS",
                    (
                        f"goal={next_goal_number}; odds={cached.team1_odds}/{cached.team2_odds}; "
                        f"locked_sides={list(lock_state['locked_sides'])}"
                    ),
                )
                return NextGoalOdds(
                    team1=cached.team1_odds,
                    team2=cached.team2_odds,
                    market=f"Следующий гол №{next_goal_number}",
                    next_goal_number=next_goal_number,
                    source="CANVAS_2D",
                    ocr_backend="fillText",
                    confidence=1.0,
                    team1_locator=Canvas2DCoefficientLocator(
                        page,
                        cached.team1_region,
                        cached.canvas,
                    ),
                    team2_locator=Canvas2DCoefficientLocator(
                        page,
                        cached.team2_region,
                        cached.canvas,
                    ),
                    locked_sides=lock_state["locked_sides"],
                    lock_markers=lock_state["markers"],
                )

        raise hybrid_market.MarketNotAvailable(
            (
                f"Canvas 2D не отдал две пары label+coefficient "
                f"для гола №{next_goal_number}."
            ),
            status="ODDS_2D_NOT_FOUND",
            details={
                "source": "CANVAS_2D",
                "next_goal_number": next_goal_number,
                "text_count": len(snapshot.get("texts") or []),
                "rect_count": len(snapshot.get("rects") or []),
                "image_count": len(snapshot.get("images") or []),
                "hook_status": snapshot.get("status"),
                "canvas_2d_diagnostics": snapshot.get("diagnostics") or {},
            },
        )

    lock_state = detect_lock_state(
        snapshot,
        team1_region=mapping.team1_region,
        team2_region=mapping.team2_region,
    )

    _LAST_MARKETS[cache_key] = _CachedMarket(
        url=page.url,
        next_goal_number=next_goal_number,
        team1_odds=mapping.team1_odds,
        team2_odds=mapping.team2_odds,
        team1_region=mapping.team1_region,
        team2_region=mapping.team2_region,
        canvas=mapping.canvas,
    )

    market = f"Следующий гол №{next_goal_number}"
    await _log(
        logger,
        "CANVAS_2D_READ",
        (
            f"goal={next_goal_number}; odds={mapping.team1_odds}/{mapping.team2_odds}; "
            f"locked_sides={list(lock_state['locked_sides'])}; "
            f"team1_button={mapping.team1_region}; team2_button={mapping.team2_region}"
        ),
    )
    await _log(logger, "NEXT_GOAL_MARKET_FOUND", market)
    await _log(
        logger,
        "TEAM1_ODDS",
        f"Команда 1 / {team1} = {mapping.team1_odds}",
    )
    await _log(
        logger,
        "TEAM2_ODDS",
        f"Команда 2 / {team2} = {mapping.team2_odds}",
    )
    await _log(
        logger,
        "ODDS_SOURCE",
        "CANVAS_2D / fillText+draw-calls / OCR disabled",
    )
    await _log(
        logger,
        "ODDS_READY",
        f"{mapping.team1_odds} / {mapping.team2_odds}",
    )

    return NextGoalOdds(
        team1=mapping.team1_odds,
        team2=mapping.team2_odds,
        market=market,
        next_goal_number=next_goal_number,
        source="CANVAS_2D",
        ocr_backend="fillText",
        confidence=1.0,
        team1_locator=Canvas2DCoefficientLocator(
            page,
            mapping.team1_region,
            mapping.canvas,
        ),
        team2_locator=Canvas2DCoefficientLocator(
            page,
            mapping.team2_region,
            mapping.canvas,
        ),
        locked_sides=lock_state["locked_sides"],
        lock_markers=lock_state["markers"],
    )
