// ── Chart Analyzer — Lightweight Charts + Drawing Tools ──
"use strict";

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let emaFastSeries = null;
let emaSlowSeries = null;
let bridge = null;

// Indicator sub-panes
let rsiSeries = null;
let macdLineSeries = null;
let macdSignalSeries = null;
let macdHistSeries = null;
let bbUpperSeries = null;
let bbLowerSeries = null;
let bbMiddleSeries = null;

// Marker data
let tradeMarkers = [];

// Theme
let theme = {
    background: "#131722",
    text: "#d1d4dc",
    grid: "#1e222d",
    border: "#2a2e39",
    crosshair: "#758696",
    upColor: "#26a69a",
    downColor: "#ef5350",
    volumeUp: "rgba(38,166,154,0.5)",
    volumeDown: "rgba(239,83,80,0.5)",
    emaFast: "#2962ff",
    emaSlow: "#ff6d00",
};

// ══════════════════════════════════════════════════════════
// DRAWING SYSTEM — Canvas overlay for trend lines, fibs, etc.
// ══════════════════════════════════════════════════════════

let drawCanvas = null;
let drawCtx = null;
let drawings = [];       // [{type, points, color, ...}, ...]
let activeTool = null;   // "trendline"|"hray"|"fib"|"rect"|"measure"|null
let drawState = null;    // tool-specific intermediate state
let hoveredDrawing = -1; // index of hovered drawing
let selectedDrawing = -1;
const DRAW_COLORS = {
    trendline: "#2962ff",
    hray: "#787b86",
    fib: "#f7a600",
    rect: "#7b1fa2",
    measure: "#26a69a",
};

function initDrawingCanvas() {
    const container = document.getElementById("chart-container");
    drawCanvas = document.createElement("canvas");
    drawCanvas.id = "draw-canvas";
    drawCanvas.style.cssText = "position:absolute;top:0;left:0;width:100%;height:100%;z-index:5;pointer-events:none;";
    container.appendChild(drawCanvas);
    drawCtx = drawCanvas.getContext("2d");
    resizeDrawCanvas();
}

function resizeDrawCanvas() {
    if (!drawCanvas) return;
    const container = document.getElementById("chart-container");
    const dpr = window.devicePixelRatio || 1;
    drawCanvas.width = container.clientWidth * dpr;
    drawCanvas.height = container.clientHeight * dpr;
    drawCanvas.style.width = container.clientWidth + "px";
    drawCanvas.style.height = container.clientHeight + "px";
    drawCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    renderDrawings();
}

// Convert price/time to pixel coordinates
function priceToY(price) {
    const cs = candleSeries.priceToCoordinate(price);
    return cs !== null ? cs : -1;
}

function timeToX(time) {
    const ts = chart.timeScale().timeToCoordinate(time);
    return ts !== null ? ts : -1;
}

function xyToPrice(y) {
    const c = candleSeries.coordinateToPrice(y);
    return c;
}

function xyToTime(x) {
    const t = chart.timeScale().coordinateToTime(x);
    return t;
}

function renderDrawings() {
    if (!drawCtx || !drawCanvas) return;
    const w = drawCanvas.clientWidth;
    const h = drawCanvas.clientHeight;
    drawCtx.clearRect(0, 0, w, h);

    for (let i = 0; i < drawings.length; i++) {
        const d = drawings[i];
        const isHovered = i === hoveredDrawing;
        const isSelected = i === selectedDrawing;
        drawCtx.save();
        drawCtx.lineWidth = isSelected ? 2.5 : isHovered ? 2 : 1.5;
        drawCtx.strokeStyle = d.color || "#787b86";
        if (isHovered) drawCtx.strokeStyle = "#ffffff";

        if (d.type === "trendline") drawTrendLine(d);
        else if (d.type === "hray") drawHRay(d, w);
        else if (d.type === "fib") drawFib(d, w);
        else if (d.type === "rect") drawRect(d);

        drawCtx.restore();
    }

    // Draw active tool preview
    if (drawState && drawState.preview) {
        drawCtx.save();
        drawCtx.lineWidth = 1.5;
        drawCtx.setLineDash([5, 4]);
        drawCtx.strokeStyle = DRAW_COLORS[activeTool] || "#787b86";
        if (activeTool === "trendline") drawTrendLine(drawState.preview);
        else if (activeTool === "hray") drawHRay(drawState.preview, w);
        else if (activeTool === "fib") drawFib(drawState.preview, w);
        else if (activeTool === "rect") drawRect(drawState.preview);
        drawCtx.restore();
    }

    // Measure overlay
    if (drawState && drawState.measureInfo) {
        const mi = drawState.measureInfo;
        drawCtx.save();
        drawCtx.setLineDash([4, 3]);
        drawCtx.strokeStyle = DRAW_COLORS.measure;
        drawCtx.lineWidth = 1;
        drawCtx.beginPath();
        drawCtx.moveTo(mi.x1, mi.y1);
        drawCtx.lineTo(mi.x2, mi.y1);
        drawCtx.lineTo(mi.x2, mi.y2);
        drawCtx.stroke();

        // Label
        drawCtx.setLineDash([]);
        drawCtx.font = "12px 'Segoe UI', sans-serif";
        drawCtx.fillStyle = theme.background;
        drawCtx.strokeStyle = DRAW_COLORS.measure;
        const txt = mi.label;
        const tw = drawCtx.measureText(txt).width;
        const lx = mi.x2 + 6, ly = (mi.y1 + mi.y2) / 2;
        drawCtx.fillRect(lx - 4, ly - 10, tw + 8, 18);
        drawCtx.strokeRect(lx - 4, ly - 10, tw + 8, 18);
        drawCtx.fillStyle = DRAW_COLORS.measure;
        drawCtx.fillText(txt, lx, ly + 4);
        drawCtx.restore();
    }
}

function drawTrendLine(d) {
    const x1 = timeToX(d.points[0].time), y1 = priceToY(d.points[0].price);
    const x2 = timeToX(d.points[1].time), y2 = priceToY(d.points[1].price);
    if (x1 < 0 || x2 < 0) return;
    drawCtx.beginPath();
    drawCtx.moveTo(x1, y1);
    drawCtx.lineTo(x2, y2);
    drawCtx.stroke();
    // Endpoints
    drawCtx.fillStyle = drawCtx.strokeStyle;
    drawCtx.beginPath(); drawCtx.arc(x1, y1, 3, 0, Math.PI * 2); drawCtx.fill();
    drawCtx.beginPath(); drawCtx.arc(x2, y2, 3, 0, Math.PI * 2); drawCtx.fill();
}

function drawHRay(d, w) {
    const y = priceToY(d.points[0].price);
    const x = timeToX(d.points[0].time);
    if (y < 0) return;
    drawCtx.beginPath();
    drawCtx.moveTo(x >= 0 ? x : 0, y);
    drawCtx.lineTo(w, y);
    drawCtx.stroke();
    // Label
    drawCtx.font = "11px 'Segoe UI'";
    drawCtx.fillStyle = drawCtx.strokeStyle;
    drawCtx.fillText(d.points[0].price.toFixed(2), (x >= 0 ? x : 4) + 6, y - 4);
}

function drawFib(d, w) {
    const p1 = d.points[0].price, p2 = d.points[1].price;
    const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    const range = p2 - p1;
    const fibColors = ["#787b86", "#f7a600", "#ff9800", "#26a69a", "#2962ff", "#e040fb", "#787b86"];
    for (let i = 0; i < levels.length; i++) {
        const price = p1 + range * (1 - levels[i]);
        const y = priceToY(price);
        if (y < 0) continue;
        drawCtx.save();
        drawCtx.strokeStyle = fibColors[i] || drawCtx.strokeStyle;
        drawCtx.globalAlpha = 0.7;
        drawCtx.beginPath();
        drawCtx.moveTo(0, y);
        drawCtx.lineTo(w, y);
        drawCtx.stroke();
        drawCtx.globalAlpha = 1;
        drawCtx.font = "10px 'Segoe UI'";
        drawCtx.fillStyle = fibColors[i];
        drawCtx.fillText(`${(levels[i] * 100).toFixed(1)}% — ${price.toFixed(2)}`, 6, y - 3);
        drawCtx.restore();
    }
}

function drawRect(d) {
    const x1 = timeToX(d.points[0].time), y1 = priceToY(d.points[0].price);
    const x2 = timeToX(d.points[1].time), y2 = priceToY(d.points[1].price);
    if (x1 < 0 || x2 < 0) return;
    drawCtx.save();
    drawCtx.globalAlpha = 0.15;
    drawCtx.fillStyle = drawCtx.strokeStyle;
    drawCtx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
    drawCtx.globalAlpha = 1;
    drawCtx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
    drawCtx.restore();
}

function setActiveTool(tool) {
    activeTool = tool; // null, "trendline", "hray", "fib", "rect", "measure"
    drawState = null;
    selectedDrawing = -1;
    const container = document.getElementById("chart-container");
    if (tool) {
        drawCanvas.style.pointerEvents = "auto";
        container.style.cursor = "crosshair";
    } else {
        drawCanvas.style.pointerEvents = "none";
        container.style.cursor = "default";
    }
    renderDrawings();
}

function undoDrawing() {
    if (drawings.length > 0) {
        drawings.pop();
        selectedDrawing = -1;
        renderDrawings();
    }
}

function deleteSelectedDrawing() {
    if (selectedDrawing >= 0 && selectedDrawing < drawings.length) {
        drawings.splice(selectedDrawing, 1);
        selectedDrawing = -1;
        renderDrawings();
    }
}

function clearAllDrawings() {
    drawings = [];
    selectedDrawing = -1;
    hoveredDrawing = -1;
    renderDrawings();
    // Also clear price lines
    removeAllUserLines();
}

// Mouse handling on canvas
function initDrawingEvents() {
    drawCanvas.addEventListener("mousedown", onDrawMouseDown);
    drawCanvas.addEventListener("mousemove", onDrawMouseMove);
    drawCanvas.addEventListener("contextmenu", onDrawContextMenu);
}

function onDrawMouseDown(e) {
    if (e.button !== 0) return; // left click only
    const rect = drawCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const price = xyToPrice(y);
    const time = xyToTime(x);
    if (price === null || time === null) return;

    if (!activeTool) return;

    if (activeTool === "hray") {
        // Single click — place ray
        drawings.push({
            type: "hray",
            points: [{ price, time }],
            color: DRAW_COLORS.hray,
        });
        renderDrawings();
        return;
    }

    if (activeTool === "measure") {
        if (!drawState) {
            drawState = { p1: { price, time, x, y } };
        } else {
            // Second click — show result and clear
            const p1 = drawState.p1;
            const diff = price - p1.price;
            const pct = ((diff / p1.price) * 100).toFixed(2);
            const sign = diff >= 0 ? "+" : "";
            const bars = Math.abs(time - p1.time) / 3600;
            drawState = {
                measureInfo: {
                    x1: p1.x, y1: p1.y, x2: x, y2: y,
                    label: `${sign}${diff.toFixed(2)} (${sign}${pct}%) | ${bars.toFixed(0)}h`,
                },
            };
            renderDrawings();
            setTimeout(() => {
                if (drawState && drawState.measureInfo) {
                    drawState = null;
                    renderDrawings();
                }
            }, 8000);
        }
        return;
    }

    // Two-click tools: trendline, fib, rect
    if (!drawState) {
        drawState = { p1: { price, time } };
    } else {
        drawings.push({
            type: activeTool,
            points: [drawState.p1, { price, time }],
            color: DRAW_COLORS[activeTool] || "#787b86",
        });
        drawState = null;
        renderDrawings();
    }
}

function onDrawMouseMove(e) {
    const rect = drawCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const price = xyToPrice(y);
    const time = xyToTime(x);

    // Preview for two-click tools
    if (drawState && drawState.p1 && activeTool && activeTool !== "measure" && activeTool !== "hray") {
        if (price !== null && time !== null) {
            drawState.preview = {
                type: activeTool,
                points: [drawState.p1, { price, time }],
                color: DRAW_COLORS[activeTool],
            };
            renderDrawings();
        }
    }

    // Measure preview
    if (activeTool === "measure" && drawState && drawState.p1 && price !== null) {
        const p1 = drawState.p1;
        const diff = price - p1.price;
        const pct = ((diff / p1.price) * 100).toFixed(2);
        const sign = diff >= 0 ? "+" : "";
        const timeDiff = time ? Math.abs(time - p1.time) / 3600 : 0;
        drawState.measureInfo = {
            x1: p1.x, y1: p1.y, x2: x, y2: y,
            label: `${sign}${diff.toFixed(2)} (${sign}${pct}%) | ${timeDiff.toFixed(0)}h`,
        };
        renderDrawings();
    }
}

function onDrawContextMenu(e) {
    e.preventDefault();
    // Cancel active tool
    if (activeTool) {
        setActiveTool(null);
        if (bridge) bridge.onToolDeactivated();
    }
}

// ══════════════════════════════════════════════════════════
// PRICE SCALE MODES
// ══════════════════════════════════════════════════════════

function setPriceScaleMode(mode) {
    // mode: "normal", "log", "percentage", "indexedTo100"
    const modeMap = {
        normal: 0,
        log: 1,
        percentage: 2,
        indexedTo100: 3,
    };
    chart.priceScale("right").applyOptions({
        mode: modeMap[mode] || 0,
    });
}

// ══════════════════════════════════════════════════════════
// INDICATOR SUB-PANES: RSI, MACD, Bollinger Bands
// ══════════════════════════════════════════════════════════

function setRsiData(jsonData) {
    const data = JSON.parse(jsonData);
    if (!rsiSeries) {
        rsiSeries = chart.addLineSeries({
            color: "#e040fb",
            lineWidth: 1,
            priceScaleId: "rsi",
            priceFormat: { type: "price", precision: 1, minMove: 0.1 },
            lastValueVisible: true,
            priceLineVisible: false,
        });
        rsiSeries.priceScale().applyOptions({
            scaleMargins: { top: 0.82, bottom: 0.02 },
            borderVisible: true,
            borderColor: theme.border,
        });
        // 70/30 levels
        const overbought = chart.addLineSeries({
            color: "rgba(239,83,80,0.4)", lineWidth: 1, lineStyle: 2,
            priceScaleId: "rsi", priceLineVisible: false, lastValueVisible: false,
            crosshairMarkerVisible: false,
        });
        overbought.setData(data.map(d => ({ time: d.time, value: 70 })));
        const oversold = chart.addLineSeries({
            color: "rgba(38,166,154,0.4)", lineWidth: 1, lineStyle: 2,
            priceScaleId: "rsi", priceLineVisible: false, lastValueVisible: false,
            crosshairMarkerVisible: false,
        });
        oversold.setData(data.map(d => ({ time: d.time, value: 30 })));
    }
    rsiSeries.setData(data);
}

function removeRsi() {
    if (rsiSeries) {
        chart.removeSeries(rsiSeries);
        rsiSeries = null;
    }
}

function setMacdData(jsonLine, jsonSignal, jsonHist) {
    const lineData = JSON.parse(jsonLine);
    const signalData = JSON.parse(jsonSignal);
    const histData = JSON.parse(jsonHist);

    if (!macdLineSeries) {
        macdLineSeries = chart.addLineSeries({
            color: "#2962ff", lineWidth: 1, priceScaleId: "macd",
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        macdSignalSeries = chart.addLineSeries({
            color: "#ff6d00", lineWidth: 1, priceScaleId: "macd",
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        macdHistSeries = chart.addHistogramSeries({
            priceScaleId: "macd",
            priceFormat: { type: "price", precision: 2, minMove: 0.01 },
            lastValueVisible: false,
        });
        macdLineSeries.priceScale().applyOptions({
            scaleMargins: { top: 0.82, bottom: 0.02 },
        });
    }
    macdLineSeries.setData(lineData);
    macdSignalSeries.setData(signalData);
    macdHistSeries.setData(histData);
}

function removeMacd() {
    if (macdLineSeries) { chart.removeSeries(macdLineSeries); macdLineSeries = null; }
    if (macdSignalSeries) { chart.removeSeries(macdSignalSeries); macdSignalSeries = null; }
    if (macdHistSeries) { chart.removeSeries(macdHistSeries); macdHistSeries = null; }
}

function setBollingerData(jsonUpper, jsonMiddle, jsonLower) {
    const upper = JSON.parse(jsonUpper);
    const middle = JSON.parse(jsonMiddle);
    const lower = JSON.parse(jsonLower);

    if (!bbUpperSeries) {
        bbUpperSeries = chart.addLineSeries({
            color: "rgba(38,166,154,0.5)", lineWidth: 1,
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        bbMiddleSeries = chart.addLineSeries({
            color: "rgba(120,123,134,0.5)", lineWidth: 1, lineStyle: 2,
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        bbLowerSeries = chart.addLineSeries({
            color: "rgba(239,83,80,0.5)", lineWidth: 1,
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
    }
    bbUpperSeries.setData(upper);
    bbMiddleSeries.setData(middle);
    bbLowerSeries.setData(lower);
}

function removeBollinger() {
    if (bbUpperSeries) { chart.removeSeries(bbUpperSeries); bbUpperSeries = null; }
    if (bbMiddleSeries) { chart.removeSeries(bbMiddleSeries); bbMiddleSeries = null; }
    if (bbLowerSeries) { chart.removeSeries(bbLowerSeries); bbLowerSeries = null; }
}

// ══════════════════════════════════════════════════════════
// KEYBOARD SHORTCUTS
// ══════════════════════════════════════════════════════════

function initKeyboard() {
    document.addEventListener("keydown", e => {
        // Ctrl+Z — undo drawing
        if (e.ctrlKey && e.key === "z") {
            e.preventDefault();
            undoDrawing();
            return;
        }
        // Delete — remove selected drawing
        if (e.key === "Delete" && selectedDrawing >= 0) {
            deleteSelectedDrawing();
            return;
        }
        // Escape — cancel tool / deselect
        if (e.key === "Escape") {
            if (activeTool) {
                setActiveTool(null);
                if (bridge) bridge.onToolDeactivated();
            }
            selectedDrawing = -1;
            drawState = null;
            renderDrawings();
            return;
        }
        // +/- zoom
        if (e.key === "+" || e.key === "=") {
            chart.timeScale().scrollToPosition(chart.timeScale().scrollPosition() - 10, false);
        }
        if (e.key === "-") {
            chart.timeScale().scrollToPosition(chart.timeScale().scrollPosition() + 10, false);
        }
        // Home / End
        if (e.key === "Home") {
            chart.timeScale().scrollToPosition(-99999, false);
        }
        if (e.key === "End") {
            chart.timeScale().scrollToRealTime();
        }
        // Space — pause/resume playback
        if (e.key === " " || e.code === "Space") {
            e.preventDefault();
            if (bridge) bridge.onPlaybackPause();
            return;
        }
        // Arrow keys
        if (e.key === "ArrowLeft") {
            chart.timeScale().scrollToPosition(chart.timeScale().scrollPosition() - 5, false);
        }
        if (e.key === "ArrowRight") {
            if (e.shiftKey && bridge) {
                // Shift+Right — step one candle forward
                e.preventDefault();
                bridge.onPlaybackStep();
                return;
            }
            chart.timeScale().scrollToPosition(chart.timeScale().scrollPosition() + 5, false);
        }
    });
}

// ══════════════════════════════════════════════════════════
// SCREENSHOT
// ══════════════════════════════════════════════════════════

function takeScreenshot() {
    chart.takeScreenshot();
}

// ══════════════════════════════════════════════════════════
// CHART INIT & CORE (refactored from original)
// ══════════════════════════════════════════════════════════

function initChart() {
    const container = document.getElementById("chart-container");

    chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight,
        layout: {
            background: { type: "solid", color: theme.background },
            textColor: theme.text,
            fontFamily: "'Segoe UI', sans-serif",
        },
        grid: {
            vertLines: { color: theme.grid },
            horzLines: { color: theme.grid },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: theme.crosshair, width: 1, style: 3 },
            horzLine: { color: theme.crosshair, width: 1, style: 3 },
        },
        rightPriceScale: {
            borderColor: theme.border,
            scaleMargins: { top: 0.05, bottom: 0.2 },
        },
        timeScale: {
            borderColor: theme.border,
            timeVisible: true,
            secondsVisible: false,
            rightOffset: 5,
        },
        handleScroll: { vertTouchDrag: true },
        handleScale: { axisPressedMouseMove: true },
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: theme.upColor, downColor: theme.downColor,
        borderUpColor: theme.upColor, borderDownColor: theme.downColor,
        wickUpColor: theme.upColor, wickDownColor: theme.downColor,
    });

    volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: "volume" }, priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
    });

    emaFastSeries = chart.addLineSeries({
        color: theme.emaFast, lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    emaSlowSeries = chart.addLineSeries({
        color: theme.emaSlow, lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });

    // Resize
    const resizeObserver = new ResizeObserver(entries => {
        for (const entry of entries) {
            const { width, height } = entry.contentRect;
            chart.applyOptions({ width, height });
            resizeDrawCanvas();
        }
    });
    resizeObserver.observe(container);

    // Click — marker detection
    chart.subscribeClick(param => {
        if (!param.point || !param.time) return;
        const clickedMarker = findMarkerAtTime(param.time);
        if (clickedMarker && bridge) {
            bridge.onMarkerClicked(JSON.stringify(clickedMarker));
        }
    });

    // Crosshair move
    chart.subscribeCrosshairMove(param => {
        if (!param.time || !param.seriesData) return;
        const candleData = param.seriesData.get(candleSeries);
        const volumeData = param.seriesData.get(volumeSeries);
        if (candleData && bridge) {
            bridge.onCrosshairMove(JSON.stringify({
                time: param.time,
                open: candleData.open, high: candleData.high,
                low: candleData.low, close: candleData.close,
                volume: volumeData ? volumeData.value : 0,
            }));
        }
        // Re-render drawings on scroll/zoom
        renderDrawings();
    });

    // Re-render drawings on timeScale changes
    chart.timeScale().subscribeVisibleTimeRangeChange(() => {
        renderDrawings();
    });
}

// ── Data Loading ───────────────────────────────────────

function setCandles(jsonData) {
    const data = JSON.parse(jsonData);
    if (!data || data.length === 0) return;
    candleSeries.setData(data.map(d => ({
        time: d.time, open: d.open, high: d.high, low: d.low, close: d.close,
    })));
    volumeSeries.setData(data.map(d => ({
        time: d.time, value: d.volume || 0,
        color: d.close >= d.open ? theme.volumeUp : theme.volumeDown,
    })));
    chart.timeScale().fitContent();
}

function updateCandle(jsonData) {
    const d = JSON.parse(jsonData);
    candleSeries.update({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close });
    volumeSeries.update({ time: d.time, value: d.volume || 0,
        color: d.close >= d.open ? theme.volumeUp : theme.volumeDown });
}

function appendCandles(jsonData) {
    const data = JSON.parse(jsonData);
    for (const d of data) {
        candleSeries.update({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close });
        volumeSeries.update({ time: d.time, value: d.volume || 0,
            color: d.close >= d.open ? theme.volumeUp : theme.volumeDown });
    }
}

// ── EMA ────────────────────────────────────────────────

function setEmaData(jsonFast, jsonSlow) {
    emaFastSeries.setData(JSON.parse(jsonFast));
    emaSlowSeries.setData(JSON.parse(jsonSlow));
}

function toggleEma(visible) {
    emaFastSeries.applyOptions({ visible });
    emaSlowSeries.applyOptions({ visible });
}

// ── Trade Markers ──────────────────────────────────────

function setTradeMarkers(jsonMarkers) {
    tradeMarkers = JSON.parse(jsonMarkers);
    const markers = tradeMarkers.map(m => ({
        time: m.time,
        position: m.direction === "BUY" ? "belowBar" : "aboveBar",
        color: getMarkerColor(m),
        shape: getMarkerShape(m),
        text: getMarkerText(m),
        size: 1.5,
    }));
    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
}

function getMarkerColor(m) {
    if (m.type === "entry") return m.direction === "BUY" ? "#26a69a" : "#ef5350";
    if (m.exit_reason && m.exit_reason.includes("TP")) return "#2962ff";
    if (m.exit_reason && m.exit_reason.includes("SL")) return "#ff9800";
    return "#787b86";
}
function getMarkerShape(m) {
    if (m.type === "entry") return m.direction === "BUY" ? "arrowUp" : "arrowDown";
    return "circle";
}
function getMarkerText(m) {
    if (m.type === "entry") return m.direction === "BUY" ? "B" : "S";
    if (m.pnl !== undefined) { const s = m.pnl >= 0 ? "+" : ""; return s + m.pnl.toFixed(2); }
    return "X";
}
function findMarkerAtTime(time) {
    for (const m of tradeMarkers) { if (Math.abs(m.time - time) < 3600) return m; }
    return null;
}

// ── Theme ──────────────────────────────────────────────

function applyTheme(jsonTheme) {
    const t = JSON.parse(jsonTheme);
    theme = {
        background: t.background, text: t.text, grid: t.grid, border: t.border,
        crosshair: t.crosshair, upColor: t.up_color, downColor: t.down_color,
        volumeUp: t.volume_up, volumeDown: t.volume_down,
        emaFast: t.ema_fast_color, emaSlow: t.ema_slow_color,
    };
    document.body.style.background = theme.background;
    chart.applyOptions({
        layout: { background: { type: "solid", color: theme.background }, textColor: theme.text },
        grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
        rightPriceScale: { borderColor: theme.border },
        timeScale: { borderColor: theme.border },
    });
    candleSeries.applyOptions({
        upColor: theme.upColor, downColor: theme.downColor,
        borderUpColor: theme.upColor, borderDownColor: theme.downColor,
        wickUpColor: theme.upColor, wickDownColor: theme.downColor,
    });
    emaFastSeries.applyOptions({ color: theme.emaFast });
    emaSlowSeries.applyOptions({ color: theme.emaSlow });
}

// ── Watermark / Price Lines / Scroll ───────────────────

function setWatermark(text) {
    document.getElementById("watermark").textContent = text;
}

let priceLines = [];
function setPriceLines(jsonLines) {
    for (const pl of priceLines) candleSeries.removePriceLine(pl);
    priceLines = [];
    const lines = JSON.parse(jsonLines);
    for (const l of lines) {
        priceLines.push(candleSeries.createPriceLine({
            price: l.price, color: l.color || "#787b86",
            lineWidth: 1, lineStyle: l.style || 2,
            axisLabelVisible: true, title: l.title || "",
        }));
    }
}
function clearPriceLines() {
    for (const pl of priceLines) candleSeries.removePriceLine(pl);
    priceLines = [];
}

let userPriceLines = [];
function addHorizontalLine(price, color, title) {
    if (!candleSeries) return;
    userPriceLines.push(candleSeries.createPriceLine({
        price, color: color || "#787b86", lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: title || "",
    }));
}
function removeAllUserLines() {
    for (const pl of userPriceLines) candleSeries.removePriceLine(pl);
    userPriceLines = [];
}
function removeLastUserLine() {
    if (userPriceLines.length > 0) candleSeries.removePriceLine(userPriceLines.pop());
}

function scrollToTime(timestamp) {
    const from = timestamp - 50 * 3600;
    const to = timestamp + 20 * 3600;
    chart.timeScale().setVisibleRange({ from, to });
}
function fitContent() {
    chart.timeScale().fitContent();
}

function setCrosshairMode(mode) {
    chart.applyOptions({
        crosshair: { mode: mode === "magnet"
            ? LightweightCharts.CrosshairMode.Magnet
            : LightweightCharts.CrosshairMode.Normal },
    });
}

function toggleDrawingMode(enabled) {
    // Legacy — now use setActiveTool("hray") or null
    if (enabled) setActiveTool("hray");
    else setActiveTool(null);
}

// ══════════════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════════════

function init() {
    initChart();
    initDrawingCanvas();
    initDrawingEvents();
    initKeyboard();

    if (typeof QWebChannel !== "undefined") {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            bridge = channel.objects.bridge;
            if (bridge) bridge.chartReady();
        });
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
