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
    if (window.__autobetCanvas2D && window.__autobetCanvas2D.version === 3) {
        return;
    }

    const state = {
        version: 3,
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
                element: canvas,
                generation: 0,
                texts: [],
                rects: [],
                images: [],
                paths: [],
                events: [],
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
            item.paths = [];
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

    const eventMeta = (item, kind) => ({
        seq: ++state.seq,
        timestamp_ms: Date.now(),
        perf_ms: typeof performance !== "undefined" ? Number(performance.now()) : null,
        canvas_id: item.id,
        generation: item.generation,
        kind,
    });

    const rememberEvent = (item, event) => {
        item.events.push(event);
        trim(item.events);
        return event;
    };

    const pathStates = new WeakMap();

    const resetTrackedPath = (ctx) => {
        pathStates.set(ctx, {
            points: [],
            op_count: 0,
            commands: [],
        });
    };

    const trackedPath = (ctx) => {
        let tracked = pathStates.get(ctx);
        if (!tracked) {
            tracked = { points: [], op_count: 0, commands: [] };
            pathStates.set(ctx, tracked);
        }
        return tracked;
    };

    const addTrackedPoints = (ctx, command, points) => {
        const tracked = trackedPath(ctx);
        const matrix = ctx.getTransform();
        for (const [x, y] of points) {
            tracked.points.push(point(matrix, Number(x) || 0, Number(y) || 0));
        }
        tracked.op_count += 1;
        tracked.commands.push(command);
        if (tracked.commands.length > 32) {
            tracked.commands.splice(0, tracked.commands.length - 32);
        }
    };

    const addTrackedRect = (ctx, command, x, y, width, height) => {
        addTrackedPoints(ctx, command, [
            [x, y],
            [Number(x) + Number(width), y],
            [Number(x) + Number(width), Number(y) + Number(height)],
            [x, Number(y) + Number(height)],
        ]);
    };

    const addTrackedArc = (ctx, command, x, y, radiusX, radiusY = radiusX) => {
        const rx = Math.abs(Number(radiusX) || 0);
        const ry = Math.abs(Number(radiusY) || 0);
        addTrackedPoints(ctx, command, [
            [Number(x) - rx, Number(y) - ry],
            [Number(x) + rx, Number(y) - ry],
            [Number(x) + rx, Number(y) + ry],
            [Number(x) - rx, Number(y) + ry],
        ]);
    };

    const trackedPathBounds = (tracked) => {
        if (!tracked || !tracked.points.length) return null;
        const xs = tracked.points.map((p) => p.x);
        const ys = tracked.points.map((p) => p.y);
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

    const recordPathPaint = (ctx, kind) => {
        bump(kind);
        const item = canvasState(ctx.canvas);
        const tracked = trackedPath(ctx);
        const box = trackedPathBounds(tracked);
        if (!item || !box || tracked.op_count <= 0) return;
        const event = {
            ...eventMeta(item, kind),
            event_type: "path",
            x: box.x,
            y: box.y,
            width: box.width,
            height: box.height,
            path_op_count: tracked.op_count,
            commands: tracked.commands.slice(-24),
            fill_style: String(ctx.fillStyle ?? ""),
            stroke_style: String(ctx.strokeStyle ?? ""),
            alpha: Number(ctx.globalAlpha ?? 1),
            line_width: Number(ctx.lineWidth ?? 1),
        };
        item.paths.push(event);
        trim(item.paths);
        rememberEvent(item, { ...event });
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
        const event = {
            ...eventMeta(item, kind),
            event_type: "text",
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
        };
        item.texts.push(event);
        trim(item.texts);
        rememberEvent(item, { ...event });
    };

    const recordRect = (ctx, x, y, width, height, kind) => {
        bump(kind);
        const item = canvasState(ctx.canvas);
        if (!item) return;
        const box = rectBounds(ctx, Number(x) || 0, Number(y) || 0, Number(width) || 0, Number(height) || 0);
        const event = {
            ...eventMeta(item, kind),
            event_type: "rect",
            x: box.x,
            y: box.y,
            width: box.width,
            height: box.height,
            fill_style: String(ctx.fillStyle ?? ""),
            stroke_style: String(ctx.strokeStyle ?? ""),
            alpha: Number(ctx.globalAlpha ?? 1),
        };
        item.rects.push(event);
        trim(item.rects);
        rememberEvent(item, { ...event });
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
        let sx = 0, sy = 0, sw = 0, sh = 0;
        let sourceCanvasId = null;
        let sourceCanvasClass = "";
        try {
            const isCanvasLike = image && (
                image instanceof HTMLCanvasElement
                || (typeof OffscreenCanvas !== "undefined" && image instanceof OffscreenCanvas)
            );
            if (isCanvasLike) {
                const sourceItem = canvasState(image);
                sourceCanvasId = sourceItem ? sourceItem.id : null;
                sourceCanvasClass = String(
                    (image.className && (
                        typeof image.className === "string"
                            ? image.className
                            : image.className.baseVal
                    )) || ""
                );
            }
        } catch (_) {}

        if (args.length >= 9) {
            sx = Number(args[1]) || 0;
            sy = Number(args[2]) || 0;
            sw = Number(args[3]) || 0;
            sh = Number(args[4]) || 0;
            dx = Number(args[5]) || 0;
            dy = Number(args[6]) || 0;
            dw = Number(args[7]) || 0;
            dh = Number(args[8]) || 0;
        } else if (args.length >= 5) {
            dx = Number(args[1]) || 0;
            dy = Number(args[2]) || 0;
            dw = Number(args[3]) || 0;
            dh = Number(args[4]) || 0;
            try {
                sw = Number(image.naturalWidth || image.videoWidth || image.width) || dw;
                sh = Number(image.naturalHeight || image.videoHeight || image.height) || dh;
            } catch (_) {
                sw = dw;
                sh = dh;
            }
        } else if (args.length >= 3) {
            dx = Number(args[1]) || 0;
            dy = Number(args[2]) || 0;
            try {
                dw = Number(image.naturalWidth || image.videoWidth || image.width) || 0;
                dh = Number(image.naturalHeight || image.videoHeight || image.height) || 0;
                sw = dw;
                sh = dh;
            } catch (_) {}
        }
        if (dw <= 0 || dh <= 0) return;
        rememberCall("drawImage", {
            source: sourceMeta(image),
            source_canvas_id: sourceCanvasId,
            source_canvas_class: sourceCanvasClass,
            source_x: sx,
            source_y: sy,
            source_width: sw,
            source_height: sh,
            x: dx,
            y: dy,
            width: dw,
            height: dh,
            context: ctx && ctx.constructor ? ctx.constructor.name : "unknown",
        });
        const box = rectBounds(ctx, dx, dy, dw, dh);
        const event = {
            ...eventMeta(item, "drawImage"),
            event_type: "image",
            x: box.x,
            y: box.y,
            width: box.width,
            height: box.height,
            source: sourceMeta(image),
            source_canvas_id: sourceCanvasId,
            source_canvas_class: sourceCanvasClass,
            source_x: sx,
            source_y: sy,
            source_width: sw,
            source_height: sh,
            alpha: Number(ctx.globalAlpha ?? 1),
        };
        item.images.push(event);
        trim(item.images);
        rememberEvent(item, { ...event });
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
        try {
            recordRect(this, args[0], args[1], args[2], args[3], "rect");
            addTrackedRect(this, "rect", args[0], args[1], args[2], args[3]);
        } catch (_) {}
        return original.apply(this, args);
    });

        patchMethod(proto, "roundRect", function(original, args) {
        try {
            recordRect(this, args[0], args[1], args[2], args[3], "roundRect");
            addTrackedRect(this, "roundRect", args[0], args[1], args[2], args[3]);
        } catch (_) {}
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
                rememberEvent(item, {
                    ...eventMeta(item, "clearRect"),
                    event_type: "clear",
                    x: box.x,
                    y: box.y,
                    width: box.width,
                    height: box.height,
                    alpha: Number(this.globalAlpha ?? 1),
                });
                if (clearArea / canvasArea >= 0.35) {
                    item.generation += 1;
                    item.texts = [];
                    item.rects = [];
                    item.images = [];
                    item.paths = [];
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

        patchMethod(proto, "beginPath", function(original, args) {
            bump("beginPath");
            try { resetTrackedPath(this); } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "moveTo", function(original, args) {
            bump("moveTo");
            try { addTrackedPoints(this, "moveTo", [[args[0], args[1]]]); } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "lineTo", function(original, args) {
            bump("lineTo");
            try { addTrackedPoints(this, "lineTo", [[args[0], args[1]]]); } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "bezierCurveTo", function(original, args) {
            bump("bezierCurveTo");
            try {
                addTrackedPoints(this, "bezierCurveTo", [
                    [args[0], args[1]],
                    [args[2], args[3]],
                    [args[4], args[5]],
                ]);
            } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "quadraticCurveTo", function(original, args) {
            bump("quadraticCurveTo");
            try {
                addTrackedPoints(this, "quadraticCurveTo", [
                    [args[0], args[1]],
                    [args[2], args[3]],
                ]);
            } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "arc", function(original, args) {
            bump("arc");
            try { addTrackedArc(this, "arc", args[0], args[1], args[2]); } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "arcTo", function(original, args) {
            bump("arcTo");
            try {
                const radius = Math.abs(Number(args[4]) || 0);
                addTrackedPoints(this, "arcTo", [
                    [Number(args[0]) - radius, Number(args[1]) - radius],
                    [Number(args[0]) + radius, Number(args[1]) + radius],
                    [Number(args[2]) - radius, Number(args[3]) - radius],
                    [Number(args[2]) + radius, Number(args[3]) + radius],
                ]);
            } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "ellipse", function(original, args) {
            bump("ellipse");
            try { addTrackedArc(this, "ellipse", args[0], args[1], args[2], args[3]); } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "closePath", function(original, args) {
            bump("closePath");
            try {
                const tracked = trackedPath(this);
                tracked.op_count += 1;
                tracked.commands.push("closePath");
            } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "fill", function(original, args) {
            try { recordPathPaint(this, "fill"); } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "stroke", function(original, args) {
            try { recordPathPaint(this, "stroke"); } catch (_) {}
            return original.apply(this, args);
        });

        patchMethod(proto, "clip", function(original, args) {
            bump("clip");
            return original.apply(this, args);
        });
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

    const serializeCanvas = (canvasItem) => {
        if (!canvasItem) return null;
        const generation = canvasItem.generation;
        const sameGeneration = (entry) => entry.generation === generation;
        let className = "";
        let tagName = "";
        try {
            const element = canvasItem.element;
            tagName = String((element && element.tagName) || "");
            if (element && element.className) {
                className = typeof element.className === "string"
                    ? element.className
                    : String(element.className.baseVal || "");
            }
        } catch (_) {}
        return {
            id: canvasItem.id,
            generation,
            width: Number(canvasItem.width || 0),
            height: Number(canvasItem.height || 0),
            class_name: className,
            tag_name: tagName,
            texts: latestTextByAnchor(
                canvasItem.texts.filter(sameGeneration)
            ),
            rects: canvasItem.rects.filter(sameGeneration).slice(-1200),
            images: canvasItem.images.filter(sameGeneration).slice(-600),
            paths: canvasItem.paths.filter(sameGeneration).slice(-800),
            events: canvasItem.events.slice(-1600),
            snapshot_at_ms: Date.now(),
        };
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
                paths: [],
                events: [],
                snapshot_at_ms: Date.now(),
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
            selected_canvas_id: item ? item.id : null,
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
            paths: item ? item.paths.filter(sameGeneration).slice(-800) : [],
            events: item ? item.events.slice(-1600) : [],
            snapshot_at_ms: Date.now(),
            canvases: Array.from(state.canvases.values())
                .map(serializeCanvas)
                .filter(Boolean),
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
    team1_click_region: dict[str, float]
    team2_click_region: dict[str, float]
    team1_text_region: dict[str, Any]
    team2_text_region: dict[str, Any]
    source_canvas_id: int | None
    source_canvas: dict[str, Any]
    canvas: dict[str, Any]


@dataclass(frozen=True)
class _CachedMarket:
    url: str
    next_goal_number: int
    team1_odds: float
    team2_odds: float
    team1_region: dict[str, float]
    team2_region: dict[str, float]
    team1_click_region: dict[str, float]
    team2_click_region: dict[str, float]
    source_canvas_id: int | None
    canvas: dict[str, Any]


_LAST_MARKETS: dict[int, _CachedMarket] = {}
_LAST_DIAGNOSTIC_SIGNATURES: dict[int, str] = {}
_LAST_REPORTED_LOCK_SEQ: dict[tuple[int, str, int, int, int, int, int], int] = {}


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


def _map_next_goal_layer(
    layer: dict[str, Any],
    expected_goal_number: int,
) -> tuple[
    float,
    float,
    dict[str, float],
    dict[str, float],
    dict[str, Any],
    dict[str, Any],
] | None:
    width = float(layer.get("width") or 0.0)
    height = float(layer.get("height") or 0.0)
    if width <= 0 or height <= 0:
        return None

    texts = [
        item
        for item in (layer.get("texts") or [])
        if str(item.get("text") or "").strip()
        and float(item.get("alpha") if item.get("alpha") is not None else 1.0) > 0.03
    ]
    rects = list(layer.get("rects") or [])
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

        return (
            team1_odds,
            team2_odds,
            _button_region(
                team1_text,
                rects,
                canvas_width=width,
                canvas_height=height,
            ),
            _button_region(
                team2_text,
                rects,
                canvas_width=width,
                canvas_height=height,
            ),
            dict(team1_text),
            dict(team2_text),
        )
    return None


def _canvas_layer_by_id(
    snapshot: dict[str, Any],
    canvas_id: int | None,
) -> dict[str, Any] | None:
    if canvas_id is None:
        return None
    for layer in snapshot.get("canvases") or []:
        if int(layer.get("id") or -1) == int(canvas_id):
            return layer
    return None


def _project_region_to_visible_canvas(
    region: dict[str, float],
    *,
    source_layer: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, float]:
    source_id = source_layer.get("id")
    selected_id = snapshot.get("selected_canvas_id")
    if source_id is None or selected_id is None or int(source_id) == int(selected_id):
        return dict(region)

    visible_images = snapshot.get("images") or []
    projections = [
        image
        for image in visible_images
        if image.get("source_canvas_id") is not None
        and int(image.get("source_canvas_id")) == int(source_id)
    ]
    if not projections:
        return dict(region)

    image = max(projections, key=lambda item: int(item.get("seq") or 0))
    source_width = float(
        image.get("source_width")
        or source_layer.get("width")
        or 0.0
    )
    source_height = float(
        image.get("source_height")
        or source_layer.get("height")
        or 0.0
    )
    if source_width <= 0 or source_height <= 0:
        return dict(region)

    source_x = float(image.get("source_x") or 0.0)
    source_y = float(image.get("source_y") or 0.0)
    dest_x = float(image.get("x") or 0.0)
    dest_y = float(image.get("y") or 0.0)
    dest_width = float(image.get("width") or source_width)
    dest_height = float(image.get("height") or source_height)

    scale_x = dest_width / source_width
    scale_y = dest_height / source_height
    return {
        "x": dest_x + (float(region["x"]) - source_x) * scale_x,
        "y": dest_y + (float(region["y"]) - source_y) * scale_y,
        "width": float(region["width"]) * scale_x,
        "height": float(region["height"]) * scale_y,
    }


def _project_visible_region_to_layer(
    region: dict[str, float],
    *,
    target_layer: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, float] | None:
    target_id = target_layer.get("id")
    selected_id = snapshot.get("selected_canvas_id")
    if target_id is None:
        return None
    if selected_id is not None and int(target_id) == int(selected_id):
        return dict(region)

    visible_images = snapshot.get("images") or []
    projections = [
        image
        for image in visible_images
        if image.get("source_canvas_id") is not None
        and int(image.get("source_canvas_id")) == int(target_id)
    ]
    if not projections:
        return None

    image = max(projections, key=lambda item: int(item.get("seq") or 0))
    source_width = float(
        image.get("source_width")
        or target_layer.get("width")
        or 0.0
    )
    source_height = float(
        image.get("source_height")
        or target_layer.get("height")
        or 0.0
    )
    dest_width = float(image.get("width") or 0.0)
    dest_height = float(image.get("height") or 0.0)
    if (
        source_width <= 0
        or source_height <= 0
        or dest_width <= 0
        or dest_height <= 0
    ):
        return None

    source_x = float(image.get("source_x") or 0.0)
    source_y = float(image.get("source_y") or 0.0)
    dest_x = float(image.get("x") or 0.0)
    dest_y = float(image.get("y") or 0.0)
    scale_x = dest_width / source_width
    scale_y = dest_height / source_height
    if scale_x <= 0 or scale_y <= 0:
        return None

    return {
        "x": source_x + (float(region["x"]) - dest_x) / scale_x,
        "y": source_y + (float(region["y"]) - dest_y) / scale_y,
        "width": float(region["width"]) / scale_x,
        "height": float(region["height"]) / scale_y,
    }


def map_next_goal_snapshot(
    snapshot: dict[str, Any],
    expected_goal_number: int,
) -> Canvas2DMarketMapping | None:
    visible_canvas = snapshot.get("canvas") or {}
    layers = list(snapshot.get("canvases") or [])

    # Prefer the internal market layer with actual fillText calls. The visible
    # canvas often contains only one drawImage() of this offscreen HTML canvas.
    layers.sort(
        key=lambda layer: (
            len(layer.get("texts") or []),
            len(layer.get("rects") or []),
        ),
        reverse=True,
    )

    # Backward compatibility for tests/snapshots created before multi-canvas
    # instrumentation.
    if not layers:
        layers = [
            {
                "id": snapshot.get("selected_canvas_id"),
                "width": visible_canvas.get("width"),
                "height": visible_canvas.get("height"),
                "texts": snapshot.get("texts") or [],
                "rects": snapshot.get("rects") or [],
                "images": snapshot.get("images") or [],
                "class_name": "",
            }
        ]

    for layer in layers:
        mapped = _map_next_goal_layer(layer, expected_goal_number)
        if mapped is None:
            continue

        (
            team1_odds,
            team2_odds,
            team1_region,
            team2_region,
            team1_text,
            team2_text,
        ) = mapped

        team1_click_region = _project_region_to_visible_canvas(
            team1_region,
            source_layer=layer,
            snapshot=snapshot,
        )
        team2_click_region = _project_region_to_visible_canvas(
            team2_region,
            source_layer=layer,
            snapshot=snapshot,
        )

        return Canvas2DMarketMapping(
            next_goal_number=expected_goal_number,
            team1_odds=team1_odds,
            team2_odds=team2_odds,
            team1_region=team1_region,
            team2_region=team2_region,
            team1_click_region=team1_click_region,
            team2_click_region=team2_click_region,
            team1_text_region=team1_text,
            team2_text_region=team2_text,
            source_canvas_id=(
                int(layer["id"]) if layer.get("id") is not None else None
            ),
            source_canvas={
                "id": layer.get("id"),
                "width": layer.get("width"),
                "height": layer.get("height"),
                "class_name": layer.get("class_name"),
            },
            canvas=dict(visible_canvas),
        )
    return None


def _consume_recent_lock_sides(
    page: Page,
    lock_state: dict[str, Any],
    *,
    market_context: int,
) -> tuple[tuple[int, ...], dict[str, Any]]:
    """Emit each transient lock once within its page/generation/market context."""
    current = {int(side) for side in lock_state.get("locked_sides") or ()}
    recent = {int(side) for side in lock_state.get("recent_locked_sides") or ()}
    recent_markers = lock_state.get("recent_markers") or {}
    emitted: list[int] = []
    emitted_markers: dict[str, Any] = {}

    def cursor_key(
        side: int,
        marker: dict[str, Any],
    ) -> tuple[int, str, int, int, int, int, int]:
        return (
            id(page),
            str(getattr(page, "url", "") or ""),
            int(marker.get("hook_installed_at") or 0),
            int(marker.get("canvas_id") or -1),
            int(marker.get("generation") or 0),
            int(market_context),
            side,
        )

    for side in sorted(current):
        marker = (lock_state.get("markers") or {}).get(str(side)) or {}
        seq = int(marker.get("seq") or 0)
        if seq > 0:
            key = cursor_key(side, marker)
            _LAST_REPORTED_LOCK_SEQ[key] = max(
                seq,
                _LAST_REPORTED_LOCK_SEQ.get(key, 0),
            )

    for side in sorted(recent):
        marker = recent_markers.get(str(side)) or {}
        seq = int(marker.get("seq") or 0)
        key = cursor_key(side, marker)
        if seq <= _LAST_REPORTED_LOCK_SEQ.get(key, 0):
            continue
        _LAST_REPORTED_LOCK_SEQ[key] = seq
        emitted.append(side)
        emitted_markers[str(side)] = marker

    return tuple(emitted), emitted_markers


def _mapping_source_layer(
    snapshot: dict[str, Any],
    source_canvas_id: int | None,
) -> dict[str, Any]:
    layer = _canvas_layer_by_id(snapshot, source_canvas_id)
    if layer is not None:
        return {
            **layer,
            "hook_installed_at": (snapshot.get("diagnostics") or {}).get(
                "installed_at"
            ),
        }
    return {
        "generation": snapshot.get("generation"),
        "hook_installed_at": (snapshot.get("diagnostics") or {}).get(
            "installed_at"
        ),
        "texts": snapshot.get("texts") or [],
        "rects": snapshot.get("rects") or [],
        "images": snapshot.get("images") or [],
        "paths": snapshot.get("paths") or [],
        "events": snapshot.get("events") or [],
        "snapshot_at_ms": snapshot.get("snapshot_at_ms"),
    }


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


def _overlap_ratio(
    region: dict[str, float],
    marker: dict[str, Any],
) -> float:
    rx = float(region.get("x") or 0.0)
    ry = float(region.get("y") or 0.0)
    rw = max(0.0, float(region.get("width") or 0.0))
    rh = max(0.0, float(region.get("height") or 0.0))
    mx = float(marker.get("x") or 0.0)
    my = float(marker.get("y") or 0.0)
    mw = max(0.0, float(marker.get("width") or 0.0))
    mh = max(0.0, float(marker.get("height") or 0.0))
    if rw <= 0 or rh <= 0 or mw <= 0 or mh <= 0:
        return 0.0
    left = max(rx, mx)
    top = max(ry, my)
    right = min(rx + rw, mx + mw)
    bottom = min(ry + rh, my + mh)
    if right <= left or bottom <= top:
        return 0.0
    return ((right - left) * (bottom - top)) / (rw * rh)


def _small_icon_in_region(
    region: dict[str, float],
    marker: dict[str, Any],
) -> bool:
    rw = max(1.0, float(region.get("width") or 1.0))
    rh = max(1.0, float(region.get("height") or 1.0))
    mw = float(marker.get("width") or 0.0)
    mh = float(marker.get("height") or 0.0)
    if mw <= 0 or mh <= 0:
        return False
    if not _rect_intersects_marker(region, marker):
        return False
    aspect = mw / mh
    return (
        max(4.0, rw * 0.035) <= mw <= min(34.0, rw * 0.38)
        and max(6.0, rh * 0.18) <= mh <= min(34.0, rh * 0.95)
        and 0.35 <= aspect <= 1.45
    )


def _lock_reason_for_marker(
    region: dict[str, float],
    item: dict[str, Any],
) -> str | None:
    kind = str(item.get("kind") or "")
    event_type = str(item.get("event_type") or "")

    if event_type == "text" or kind in {"fillText", "strokeText"}:
        return _lock_text_reason(str(item.get("text") or ""))

    if event_type == "image" or kind == "drawImage":
        source = str(item.get("source") or "")
        if LOCK_WORD_RE.search(source):
            return "lock-image"
        if _small_icon_in_region(region, item):
            return "canvas-image-icon"
        return None

    if event_type == "path" or kind in {"fill", "stroke"}:
        if (
            int(item.get("path_op_count") or 0) >= 3
            and _small_icon_in_region(region, item)
        ):
            return "canvas-vector-icon"
        return None

    if event_type == "rect" or kind in {"fillRect", "strokeRect", "rect", "roundRect"}:
        alpha = float(item.get("alpha") if item.get("alpha") is not None else 1.0)
        if (
            kind == "fillRect"
            and 0.05 <= alpha < 0.98
            and _overlap_ratio(region, item) >= 0.70
        ):
            return "canvas-dim-overlay"
    return None


def _marker_payload(item: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "seq": int(item.get("seq") or 0),
        "timestamp_ms": item.get("timestamp_ms"),
        "canvas_id": item.get("canvas_id"),
        "generation": item.get("generation"),
        "x": float(item.get("x") or 0.0),
        "y": float(item.get("y") or 0.0),
        "width": float(item.get("width") or 0.0),
        "height": float(item.get("height") or 0.0),
        "reason": reason,
        "kind": item.get("kind"),
        "event_type": item.get("event_type"),
        "text": item.get("text"),
        "source": item.get("source"),
        "alpha": item.get("alpha"),
        "fill_style": item.get("fill_style"),
        "stroke_style": item.get("stroke_style"),
        "path_op_count": item.get("path_op_count"),
    }


def _odds_events_in_region(
    snapshot: dict[str, Any],
    region: dict[str, float],
) -> list[dict[str, Any]]:
    """Return odds draw events for this button in the active Canvas generation."""
    by_seq: dict[int, dict[str, Any]] = {}
    generation = snapshot.get("generation")

    items = list(snapshot.get("texts") or [])
    items.extend(
        item
        for item in (snapshot.get("events") or [])
        if str(item.get("event_type") or "") == "text"
    )

    for item in items:
        item_generation = item.get("generation")
        if (
            generation is not None
            and item_generation is not None
            and int(item_generation) != int(generation)
        ):
            continue

        if not _rect_intersects_marker(region, item):
            continue

        if _parse_odds(str(item.get("text") or "")) is None:
            continue

        seq = int(item.get("seq") or 0)
        if seq > 0:
            by_seq[seq] = item

    return [by_seq[seq] for seq in sorted(by_seq)]


def _same_render_burst(
    marker: dict[str, Any],
    odds_item: dict[str, Any] | None,
) -> bool:
    if odds_item is None:
        return False
    marker_seq = int(marker.get("seq") or 0)
    odds_seq = int(odds_item.get("seq") or 0)
    if marker_seq <= 0 or odds_seq <= 0 or abs(odds_seq - marker_seq) > 128:
        return False
    marker_generation = marker.get("generation")
    odds_generation = odds_item.get("generation")
    if (
        marker_generation is not None
        and odds_generation is not None
        and int(marker_generation) != int(odds_generation)
    ):
        return False
    marker_ts = marker.get("timestamp_ms")
    odds_ts = odds_item.get("timestamp_ms")
    if marker_ts is None or odds_ts is None:
        return abs(odds_seq - marker_seq) <= 24
    return abs(float(odds_ts) - float(marker_ts)) <= 350.0


def detect_lock_state(
    snapshot: dict[str, Any],
    *,
    team1_region: dict[str, float],
    team2_region: dict[str, float],
) -> dict[str, Any]:
    current_items = [
        *(snapshot.get("texts") or []),
        *(snapshot.get("images") or []),
        *(snapshot.get("paths") or []),
        *(snapshot.get("rects") or []),
    ]
    events = list(snapshot.get("events") or [])
    generation = snapshot.get("generation")
    if generation is not None:
        events = [
            item
            for item in events
            if int(item.get("generation") or 0) == int(generation)
        ]

    current_result: dict[str, Any] = {}
    recent_result: dict[str, Any] = {}
    hook_installed_at = snapshot.get("hook_installed_at")

    for side, region in ((1, team1_region), (2, team2_region)):
        odds_events = _odds_events_in_region(snapshot, region)
        odds_sequences = [int(item.get("seq") or 0) for item in odds_events]
        latest_odds_item = odds_events[-1] if odds_events else None
        latest_odds_seq = (
            int(latest_odds_item.get("seq") or 0)
            if latest_odds_item is not None
            else 0
        )

        current_candidates: list[dict[str, Any]] = []
        for item in current_items:
            if not _rect_intersects_marker(region, item):
                continue
            reason = _lock_reason_for_marker(region, item)
            if reason is None:
                continue
            marker_seq = int(item.get("seq") or 0)
            if marker_seq < latest_odds_seq:
                if (
                    reason == "canvas-vector-icon"
                    or not _same_render_burst(item, latest_odds_item)
                ):
                    continue
            payload = _marker_payload(item, reason)
            payload["hook_installed_at"] = hook_installed_at
            current_candidates.append(payload)

        if current_candidates:
            current_result[str(side)] = max(
                current_candidates,
                key=lambda marker: int(marker.get("seq") or 0),
            )

        recent_candidates: list[dict[str, Any]] = []
        for item in events:
            if not _rect_intersects_marker(region, item):
                continue
            reason = _lock_reason_for_marker(region, item)
            if reason is None:
                continue
            marker_seq = int(item.get("seq") or 0)
            explicit_lock = reason in {
                "lock-symbol",
                "lock-text",
                "private-icon-glyph",
                "lock-image",
            }
            if not explicit_lock:
                had_odds_before = any(seq < marker_seq for seq in odds_sequences)
                redrawn_odds_after = any(seq > marker_seq for seq in odds_sequences)
                if not (had_odds_before and redrawn_odds_after):
                    continue

            payload = _marker_payload(item, reason)
            payload["hook_installed_at"] = hook_installed_at
            recent_candidates.append(payload)

        if recent_candidates:
            recent_result[str(side)] = max(
                recent_candidates,
                key=lambda marker: int(marker.get("seq") or 0),
            )

    locked_sides = tuple(sorted(int(side) for side in current_result))
    recent_locked_sides = tuple(
        sorted(int(side) for side in recent_result if int(side) not in locked_sides)
    )
    return {
        "locked_sides": locked_sides,
        "recent_locked_sides": recent_locked_sides,
        "markers": current_result,
        "recent_markers": recent_result,
    }


def _scale_region_between_layers(
    region: dict[str, float],
    *,
    source_layer: dict[str, Any],
    target_layer: dict[str, Any],
) -> dict[str, float] | None:
    source_width = float(source_layer.get("width") or 0.0)
    source_height = float(source_layer.get("height") or 0.0)
    target_width = float(target_layer.get("width") or 0.0)
    target_height = float(target_layer.get("height") or 0.0)
    if (
        source_width <= 0
        or source_height <= 0
        or target_width <= 0
        or target_height <= 0
    ):
        return None
    return {
        "x": float(region["x"]) * target_width / source_width,
        "y": float(region["y"]) * target_height / source_height,
        "width": float(region["width"]) * target_width / source_width,
        "height": float(region["height"]) * target_height / source_height,
    }


def _detect_multilayer_lock_state(
    snapshot: dict[str, Any],
    *,
    source_canvas_id: int | None,
    team1_region: dict[str, float],
    team2_region: dict[str, float],
    team1_click_region: dict[str, float],
    team2_click_region: dict[str, float],
) -> dict[str, Any]:
    layers = list(snapshot.get("canvases") or [])
    if not layers:
        return detect_lock_state(
            snapshot,
            team1_region=team1_region,
            team2_region=team2_region,
        )

    source_layer = _canvas_layer_by_id(snapshot, source_canvas_id)
    selected_id = snapshot.get("selected_canvas_id")
    current_markers: dict[str, Any] = {}
    recent_markers: dict[str, Any] = {}
    checked_layers: list[int] = []

    for layer in layers:
        layer_id = layer.get("id")
        if layer_id is not None:
            checked_layers.append(int(layer_id))

        if (
            source_canvas_id is not None
            and layer_id is not None
            and int(layer_id) == int(source_canvas_id)
        ):
            region1 = dict(team1_region)
            region2 = dict(team2_region)
        elif (
            selected_id is not None
            and layer_id is not None
            and int(layer_id) == int(selected_id)
        ):
            region1 = dict(team1_click_region)
            region2 = dict(team2_click_region)
        else:
            region1 = _project_visible_region_to_layer(
                team1_click_region,
                target_layer=layer,
                snapshot=snapshot,
            )
            region2 = _project_visible_region_to_layer(
                team2_click_region,
                target_layer=layer,
                snapshot=snapshot,
            )
            if (region1 is None or region2 is None) and source_layer is not None:
                region1 = _scale_region_between_layers(
                    team1_region,
                    source_layer=source_layer,
                    target_layer=layer,
                )
                region2 = _scale_region_between_layers(
                    team2_region,
                    source_layer=source_layer,
                    target_layer=layer,
                )

        if region1 is None or region2 is None:
            continue

        layer_snapshot = {
            **layer,
            "snapshot_at_ms": (
                layer.get("snapshot_at_ms")
                or snapshot.get("snapshot_at_ms")
            ),
        }
        state = detect_lock_state(
            layer_snapshot,
            team1_region=region1,
            team2_region=region2,
        )

        for side, marker in (state.get("markers") or {}).items():
            candidate = {
                **marker,
                "detected_canvas_id": layer_id,
            }
            previous = current_markers.get(str(side))
            if previous is None or int(candidate.get("seq") or 0) > int(
                previous.get("seq") or 0
            ):
                current_markers[str(side)] = candidate

        for side, marker in (state.get("recent_markers") or {}).items():
            candidate = {
                **marker,
                "detected_canvas_id": layer_id,
            }
            previous = recent_markers.get(str(side))
            if previous is None or int(candidate.get("seq") or 0) > int(
                previous.get("seq") or 0
            ):
                recent_markers[str(side)] = candidate

    locked_sides = tuple(sorted(int(side) for side in current_markers))
    recent_locked_sides = tuple(
        sorted(
            int(side)
            for side in recent_markers
            if int(side) not in locked_sides
        )
    )
    return {
        "locked_sides": locked_sides,
        "recent_locked_sides": recent_locked_sides,
        "markers": current_markers,
        "recent_markers": recent_markers,
        "checked_canvas_ids": tuple(sorted(set(checked_layers))),
    }


async def read_next_goal_lock_state(
    page: Page,
    next_goal_number: int,
) -> dict[str, Any]:
    """Read only Canvas 2D lock state using the last mapped Next Goal buttons."""
    cached = _LAST_MARKETS.get(id(page))
    if (
        cached is None
        or cached.url != page.url
        or int(cached.next_goal_number) != int(next_goal_number)
    ):
        return {
            "available": False,
            "locked_sides": (),
            "recent_locked_sides": (),
            "markers": {},
            "recent_markers": {},
            "reason": "NO_CACHED_MARKET_MAPPING",
        }

    snapshot = await capture_canvas_2d_snapshot(page)
    lock_state = _detect_multilayer_lock_state(
        snapshot,
        source_canvas_id=cached.source_canvas_id,
        team1_region=cached.team1_region,
        team2_region=cached.team2_region,
        team1_click_region=cached.team1_click_region,
        team2_click_region=cached.team2_click_region,
    )
    recent_locked_sides, recent_lock_markers = _consume_recent_lock_sides(
        page,
        lock_state,
        market_context=int(next_goal_number),
    )
    return {
        "available": True,
        "locked_sides": lock_state["locked_sides"],
        "recent_locked_sides": recent_locked_sides,
        "markers": lock_state["markers"],
        "recent_markers": recent_lock_markers,
        "checked_canvas_ids": lock_state.get("checked_canvas_ids") or (),
        "reason": None,
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
        f"hook_v={snapshot.get('hook_version') or diagnostics.get('version') or 3}; "
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
            lock_state = _detect_multilayer_lock_state(
                snapshot,
                source_canvas_id=cached.source_canvas_id,
                team1_region=cached.team1_region,
                team2_region=cached.team2_region,
                team1_click_region=cached.team1_click_region,
                team2_click_region=cached.team2_click_region,
            )
            recent_locked_sides, recent_lock_markers = _consume_recent_lock_sides(
                page,
                lock_state,
                market_context=next_goal_number,
            )
            if lock_state["locked_sides"] or recent_locked_sides:
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
                        cached.team1_click_region,
                        cached.canvas,
                    ),
                    team2_locator=Canvas2DCoefficientLocator(
                        page,
                        cached.team2_click_region,
                        cached.canvas,
                    ),
                    locked_sides=lock_state["locked_sides"],
                    lock_markers=lock_state["markers"],
                    recent_locked_sides=recent_locked_sides,
                    recent_lock_markers=recent_lock_markers,
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

    lock_state = _detect_multilayer_lock_state(
        snapshot,
        source_canvas_id=mapping.source_canvas_id,
        team1_region=mapping.team1_region,
        team2_region=mapping.team2_region,
        team1_click_region=mapping.team1_click_region,
        team2_click_region=mapping.team2_click_region,
    )
    recent_locked_sides, recent_lock_markers = _consume_recent_lock_sides(
        page,
        lock_state,
        market_context=next_goal_number,
    )

    _LAST_MARKETS[cache_key] = _CachedMarket(
        url=page.url,
        next_goal_number=next_goal_number,
        team1_odds=mapping.team1_odds,
        team2_odds=mapping.team2_odds,
        team1_region=mapping.team1_region,
        team2_region=mapping.team2_region,
        team1_click_region=mapping.team1_click_region,
        team2_click_region=mapping.team2_click_region,
        source_canvas_id=mapping.source_canvas_id,
        canvas=mapping.canvas,
    )

    market = f"Следующий гол №{next_goal_number}"
    await _log(
        logger,
        "CANVAS_2D_READ",
        (
            f"goal={next_goal_number}; odds={mapping.team1_odds}/{mapping.team2_odds}; "
            f"locked_sides={list(lock_state['locked_sides'])}; "
            f"recent_locked_sides={list(recent_locked_sides)}; "
            f"checked_canvas_ids={list(lock_state.get('checked_canvas_ids') or ())}; "
            f"source_canvas_id={mapping.source_canvas_id}; "
            f"source_class={mapping.source_canvas.get('class_name')}; "
            f"team1_source_button={mapping.team1_region}; "
            f"team2_source_button={mapping.team2_region}; "
            f"team1_click={mapping.team1_click_region}; "
            f"team2_click={mapping.team2_click_region}"
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
            mapping.team1_click_region,
            mapping.canvas,
        ),
        team2_locator=Canvas2DCoefficientLocator(
            page,
            mapping.team2_click_region,
            mapping.canvas,
        ),
        locked_sides=lock_state["locked_sides"],
        lock_markers=lock_state["markers"],
        recent_locked_sides=recent_locked_sides,
        recent_lock_markers=recent_lock_markers,
    )
