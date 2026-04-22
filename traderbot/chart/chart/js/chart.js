// ── Chart Analyzer — Lightweight Charts integration ──
"use strict";

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let emaFastSeries = null;
let emaSlowSeries = null;
let bridge = null;

// Marker data stored for click detection
let tradeMarkers = [];

// Current theme colors
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

// ── Initialize ──────────────────────────────────────────

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

    // Candlestick series
    candleSeries = chart.addCandlestickSeries({
        upColor: theme.upColor,
        downColor: theme.downColor,
        borderUpColor: theme.upColor,
        borderDownColor: theme.downColor,
        wickUpColor: theme.upColor,
        wickDownColor: theme.downColor,
    });

    // Volume series (histogram on price pane bottom)
    volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: "volume" },
        priceScaleId: "",
    });
    volumeSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
    });

    // EMA lines (hidden by default)
    emaFastSeries = chart.addLineSeries({
        color: theme.emaFast,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
    });

    emaSlowSeries = chart.addLineSeries({
        color: theme.emaSlow,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
    });

    // Handle resize
    const resizeObserver = new ResizeObserver(entries => {
        for (const entry of entries) {
            const { width, height } = entry.contentRect;
            chart.applyOptions({ width, height });
        }
    });
    resizeObserver.observe(container);

    // Click handler for trade markers
    chart.subscribeClick(param => {
        if (!param.point || !param.time) return;
        const clickedMarker = findMarkerAtTime(param.time);
        if (clickedMarker && bridge) {
            bridge.onMarkerClicked(JSON.stringify(clickedMarker));
        }
    });

    // Crosshair move for OHLCV display
    chart.subscribeCrosshairMove(param => {
        if (!param.time || !param.seriesData) return;
        const candleData = param.seriesData.get(candleSeries);
        const volumeData = param.seriesData.get(volumeSeries);
        if (candleData && bridge) {
            bridge.onCrosshairMove(JSON.stringify({
                time: param.time,
                open: candleData.open,
                high: candleData.high,
                low: candleData.low,
                close: candleData.close,
                volume: volumeData ? volumeData.value : 0,
            }));
        }
    });
}

// ── Data Loading ────────────────────────────────────────

function setCandles(jsonData) {
    const data = JSON.parse(jsonData);
    if (!data || data.length === 0) return;

    candleSeries.setData(data.map(d => ({
        time: d.time,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
    })));

    // Volume with color based on candle direction
    volumeSeries.setData(data.map(d => ({
        time: d.time,
        value: d.volume || 0,
        color: d.close >= d.open ? theme.volumeUp : theme.volumeDown,
    })));

    chart.timeScale().fitContent();
}

function updateCandle(jsonData) {
    const d = JSON.parse(jsonData);
    candleSeries.update({
        time: d.time,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
    });
    volumeSeries.update({
        time: d.time,
        value: d.volume || 0,
        color: d.close >= d.open ? theme.volumeUp : theme.volumeDown,
    });
}

function appendCandles(jsonData) {
    const data = JSON.parse(jsonData);
    for (const d of data) {
        candleSeries.update({
            time: d.time,
            open: d.open,
            high: d.high,
            low: d.low,
            close: d.close,
        });
        volumeSeries.update({
            time: d.time,
            value: d.volume || 0,
            color: d.close >= d.open ? theme.volumeUp : theme.volumeDown,
        });
    }
}

// ── EMA Overlay ─────────────────────────────────────────

function setEmaData(jsonFast, jsonSlow) {
    const fast = JSON.parse(jsonFast);
    const slow = JSON.parse(jsonSlow);
    emaFastSeries.setData(fast);
    emaSlowSeries.setData(slow);
}

function toggleEma(visible) {
    const opts = visible
        ? { lineWidth: 1 }
        : { lineWidth: 0 };
    // Lightweight Charts doesn't have a direct hide, use visible property
    emaFastSeries.applyOptions({
        visible: visible,
    });
    emaSlowSeries.applyOptions({
        visible: visible,
    });
}

// ── Trade Markers ───────────────────────────────────────

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

    // Sort by time (required by Lightweight Charts)
    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
}

function getMarkerColor(m) {
    if (m.type === "entry") {
        return m.direction === "BUY" ? "#26a69a" : "#ef5350";
    }
    // Exit markers
    if (m.exit_reason && m.exit_reason.includes("TP")) return "#2962ff";
    if (m.exit_reason && m.exit_reason.includes("SL")) return "#ff9800";
    return "#787b86";
}

function getMarkerShape(m) {
    if (m.type === "entry") {
        return m.direction === "BUY" ? "arrowUp" : "arrowDown";
    }
    return "circle";
}

function getMarkerText(m) {
    if (m.type === "entry") {
        return m.direction === "BUY" ? "B" : "S";
    }
    if (m.pnl !== undefined) {
        const sign = m.pnl >= 0 ? "+" : "";
        return sign + m.pnl.toFixed(2);
    }
    return "X";
}

function findMarkerAtTime(time) {
    // Find the closest marker within 1 bar
    for (const m of tradeMarkers) {
        if (Math.abs(m.time - time) < 3600) {
            return m;
        }
    }
    return null;
}

// ── Theme ───────────────────────────────────────────────

function applyTheme(jsonTheme) {
    const t = JSON.parse(jsonTheme);
    theme = {
        background: t.background,
        text: t.text,
        grid: t.grid,
        border: t.border,
        crosshair: t.crosshair,
        upColor: t.up_color,
        downColor: t.down_color,
        volumeUp: t.volume_up,
        volumeDown: t.volume_down,
        emaFast: t.ema_fast_color,
        emaSlow: t.ema_slow_color,
    };

    document.body.style.background = theme.background;

    chart.applyOptions({
        layout: {
            background: { type: "solid", color: theme.background },
            textColor: theme.text,
        },
        grid: {
            vertLines: { color: theme.grid },
            horzLines: { color: theme.grid },
        },
        rightPriceScale: { borderColor: theme.border },
        timeScale: { borderColor: theme.border },
    });

    candleSeries.applyOptions({
        upColor: theme.upColor,
        downColor: theme.downColor,
        borderUpColor: theme.upColor,
        borderDownColor: theme.downColor,
        wickUpColor: theme.upColor,
        wickDownColor: theme.downColor,
    });

    emaFastSeries.applyOptions({ color: theme.emaFast });
    emaSlowSeries.applyOptions({ color: theme.emaSlow });
}

// ── Watermark ───────────────────────────────────────────

function setWatermark(text) {
    document.getElementById("watermark").textContent = text;
}

// ── Price Lines (SL/TP levels) ──────────────────────────

let priceLines = [];

function setPriceLines(jsonLines) {
    // Remove old lines
    for (const pl of priceLines) {
        candleSeries.removePriceLine(pl);
    }
    priceLines = [];

    const lines = JSON.parse(jsonLines);
    for (const l of lines) {
        const pl = candleSeries.createPriceLine({
            price: l.price,
            color: l.color || "#787b86",
            lineWidth: 1,
            lineStyle: l.style || 2, // dashed
            axisLabelVisible: true,
            title: l.title || "",
        });
        priceLines.push(pl);
    }
}

function clearPriceLines() {
    for (const pl of priceLines) {
        candleSeries.removePriceLine(pl);
    }
    priceLines = [];
}

// ── Scroll to time ──────────────────────────────────────

function scrollToTime(timestamp) {
    chart.timeScale().scrollToPosition(-10, false);
    // Use setVisibleRange if we have a specific time
    const from = timestamp - 50 * 3600;
    const to = timestamp + 20 * 3600;
    chart.timeScale().setVisibleRange({ from, to });
}

function fitContent() {
    chart.timeScale().fitContent();
}

// ── Drawing tools ──────────────────────────────────────

let userPriceLines = [];
let isDrawingMode = false;
let measureStart = null;

function setCrosshairMode(mode) {
    // mode: "normal" or "magnet"
    if (mode === "magnet") {
        chart.applyOptions({
            crosshair: { mode: LightweightCharts.CrosshairMode.Magnet },
        });
    } else {
        chart.applyOptions({
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        });
    }
}

function toggleDrawingMode(enabled) {
    isDrawingMode = enabled;
    document.getElementById("chart-container").style.cursor = enabled ? "crosshair" : "default";
}

function addHorizontalLine(price, color, title) {
    if (!candleSeries) return;
    const pl = candleSeries.createPriceLine({
        price: price,
        color: color || "#787b86",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: title || "",
    });
    userPriceLines.push(pl);
}

function removeAllUserLines() {
    for (const pl of userPriceLines) {
        candleSeries.removePriceLine(pl);
    }
    userPriceLines = [];
}

function removeLastUserLine() {
    if (userPriceLines.length > 0) {
        const pl = userPriceLines.pop();
        candleSeries.removePriceLine(pl);
    }
}

// Measure tool state
let measureLine = null;

function startMeasure(price1, time1) {
    measureStart = { price: price1, time: time1 };
}

function showMeasureResult(price1, price2, time1, time2) {
    const diff = price2 - price1;
    const pct = ((diff / price1) * 100).toFixed(2);
    const sign = diff >= 0 ? "+" : "";
    const text = sign + diff.toFixed(2) + " (" + sign + pct + "%)";

    // Show as watermark temporarily
    const el = document.getElementById("measure-info");
    if (el) {
        el.textContent = text;
        el.style.display = "block";
        setTimeout(() => { el.style.display = "none"; }, 5000);
    }
}

// ── Init with QWebChannel bridge ────────────────────────

function init() {
    initChart();

    // Drawing mode: click to add horizontal line at price
    chart.subscribeClick(param => {
        if (!param.point || !param.time) return;

        if (isDrawingMode && param.seriesData) {
            const candleData = param.seriesData.get(candleSeries);
            if (candleData) {
                const price = candleData.close;
                addHorizontalLine(price, "#787b86", price.toFixed(2));
                if (bridge) bridge.onLineDrawn(JSON.stringify({price: price}));
            }
        }

        // Trade marker click (original behavior)
        const clickedMarker = findMarkerAtTime(param.time);
        if (clickedMarker && bridge) {
            bridge.onMarkerClicked(JSON.stringify(clickedMarker));
        }
    });

    if (typeof QWebChannel !== "undefined") {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            bridge = channel.objects.bridge;
            if (bridge) {
                bridge.chartReady();
            }
        });
    }
}

// Start when DOM is ready
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
