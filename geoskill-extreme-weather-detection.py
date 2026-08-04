#!/usr/bin/env python3
"""extreme-weather-detection — 极端天气事件检测

基于**百分位阈值法**（percentile-threshold method）从温度 / 降水时间序列中
检测极端天气事件：

- **热浪**（heatwave）：温度高于高分位阈值（如 P90）且连续 ≥ 3 天。
- **寒潮**（cold spell）：温度低于低分位阈值（如 P10）。
- **暴雨**（heavy rainfall）：降水高于 P95 / P99。

阈值默认逐像元由序列自身的分位数确定；事件通过在 (时间, y, x) 三维 exceedance
体上做连通分量标记（scipy.ndimage.label，时间 + 四邻域连通）提取，逐个统计
持续时间、峰值强度、平均强度与空间范围。

数据源：本地多期 GeoTIFF（每波段 = 一个时间步），或 ``--synthetic`` 生成
内嵌已知极端事件的模拟序列用于离线验证。

隐私声明 / Privacy：
- 默认完全离线运行，``--synthetic`` 模式不读取任何外部数据。
- 所有处理在本地完成，不上传任何用户数据。

Usage:
    python extreme-weather-detection.py --bbox 116 39 117 40 --variable temperature --threshold p90
    python extreme-weather-detection.py --input precip_cube.tif --variable precipitation --threshold p99

License: MIT
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

VERSION = "1.0.0"
SKILL_NAME = "extreme-weather-detection"

# ---- 复用共享核心库（本地 vendored，随脚本目录一起分发）----
try:
    from _geoskill_core.errors import (
        GeoSkillError, UsageError, ValidationError, ProcessError, to_exit_code,
    )
    from _geoskill_core.manifest import OutputManifest, OutputFile
except ImportError:  # pragma: no cover - fallback minimal definitions
    class GeoSkillError(Exception):
        def __init__(self, message: str, code: int = 7, kind: str = "EGeo", **kw):
            super().__init__(message)
            self.message, self.code, self.kind = message, code, kind

    class UsageError(GeoSkillError):
        def __init__(self, m, **k): super().__init__(m, code=2, kind="EUsage", **k)

    class ValidationError(GeoSkillError):
        def __init__(self, m, **k): super().__init__(m, code=6, kind="EValidate", **k)

    class ProcessError(GeoSkillError):
        def __init__(self, m, **k): super().__init__(m, code=7, kind="EProcess", **k)

    def to_exit_code(exc):
        return getattr(exc, "code", 7)

    OutputManifest = None
    OutputFile = None


# 支持的百分位阈值选项
THRESHOLD_CHOICES = ["p05", "p10", "p25", "p75", "p90", "p95", "p99"]


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def validate_bbox(bbox) -> None:
    """Validate a [W, S, E, N] geographic bbox.

    Raises ValidationError (exit 6) on:
      - non-finite values
      - longitude/latitude out of range
      - W >= E (no antimeridian wrap-around)
      - S >= N
      - zero-area bbox
    """
    w, s, e, n = bbox
    if not all(np.isfinite([w, s, e, n])):
        raise ValidationError(
            f"bbox contains non-finite values: W={w} S={s} E={e} N={n}",
            bbox=list(bbox),
        )
    if abs(w) > 180.0 or abs(e) > 180.0:
        raise ValidationError(
            f"bbox longitude out of range: W={w} E={e} (must be in [-180, 180])",
            bbox=list(bbox),
        )
    if abs(s) > 90.0 or abs(n) > 90.0:
        raise ValidationError(
            f"bbox latitude out of range: S={s} N={n} (must be in [-90, 90])",
            bbox=list(bbox),
        )
    if w >= e:
        raise ValidationError(
            f"bbox reversed: W ({w}) must be < E ({e}). "
            f"For antimeridian-crossing bboxes, split into W..180 and -180..E.",
            bbox=list(bbox),
        )
    if s >= n:
        raise ValidationError(
            f"bbox reversed: S ({s}) must be < N ({n})", bbox=list(bbox)
        )
    if (e - w) < 1e-9 or (n - s) < 1e-9:
        raise ValidationError(
            f"bbox has zero area: W={w} S={s} E={e} N={n}", bbox=list(bbox)
        )


def read_geotiff_with_nodata(path: str):
    """Read a multi-band GeoTIFF, replacing NoData with NaN.

    Returns (cube_float32, bbox_WSEN, n_valid_pixel_steps).
    """
    cube, bbox = read_geotiff(path)
    import rasterio
    with rasterio.open(path) as src:
        nodata = src.nodata
    if nodata is not None:
        cube = np.where(cube == nodata, np.nan, cube).astype(np.float32)
    # n_valid_pixel_steps = total number of (t,y,x) entries that are finite
    n_valid = int(np.sum(np.isfinite(cube)))
    return cube, bbox, n_valid


# ---------------------------------------------------------------------------
# 核心算法
# ---------------------------------------------------------------------------
def parse_threshold(name: str) -> Tuple[float, bool]:
    """解析阈值名（如 'p90'）→ (百分位值, 是否上尾)。

    百分位 >= 50 视为上尾（高于阈值 = 极端，如热浪/暴雨）；
    < 50 视为下尾（低于阈值 = 极端，如寒潮）。
    """
    if not (isinstance(name, str) and name.lower().startswith("p")):
        raise UsageError(f"invalid threshold '{name}', expect like 'p90'")
    try:
        pct = float(name[1:])
    except ValueError:
        raise UsageError(f"invalid threshold '{name}', expect like 'p90'")
    if not (0 < pct < 100):
        raise UsageError(f"threshold percentile must be in (0,100), got {pct}")
    return pct, pct >= 50.0


def percentile_threshold(cube: np.ndarray, percentile: float) -> np.ndarray:
    """逐像元计算时间维分位数阈值，返回 (H, W)。"""
    if cube.ndim != 3:
        raise ValidationError(
            f"cube must be 3-D (n_dates, H, W), got {cube.shape}",
            shape=tuple(cube.shape),
        )
    return np.nanpercentile(cube, percentile, axis=0).astype(np.float32)


def exceedance_mask(
    cube: np.ndarray, threshold: np.ndarray, upper: bool = True
) -> np.ndarray:
    """生成 exceedance 布尔立方体 (n, H, W)。

    upper=True：value > threshold（热浪/暴雨）；upper=False：value < threshold（寒潮）。
    threshold 可为标量或 (H, W) 栅格。
    """
    thr = np.asarray(threshold, dtype=np.float32)
    if upper:
        return cube > thr
    return cube < thr


def _connectivity_structure() -> np.ndarray:
    """3D 连通结构：时间轴相邻 + 同时间步四邻域（6-连通）。"""
    s = np.zeros((3, 3, 3), dtype=int)
    s[1, 1, 1] = 1
    s[0, 1, 1] = 1; s[2, 1, 1] = 1   # 时间相邻
    s[1, 0, 1] = 1; s[1, 2, 1] = 1   # y 相邻
    s[1, 1, 0] = 1; s[1, 1, 2] = 1   # x 相邻
    return s


def consecutive_runs(mask_1d: np.ndarray) -> List[Tuple[int, int]]:
    """在一维布尔序列中找连续 True 游程，返回 [(start, length), ...]。"""
    runs: List[Tuple[int, int]] = []
    m = np.asarray(mask_1d, dtype=bool)
    n = m.size
    i = 0
    while i < n:
        if m[i]:
            j = i
            while j < n and m[j]:
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    return runs


def detect_events(
    cube: np.ndarray,
    threshold: Any,
    upper: bool = True,
    min_duration: int = 3,
    min_pixels: int = 1,
) -> Dict[str, Any]:
    """在 (n, H, W) 立方体中检测极端事件。

    参数
    ----
    cube : 值立方体。
    threshold : 标量或 (H, W) 阈值栅格。
    upper : True=上尾极端（>阈值），False=下尾极端（<阈值）。
    min_duration : 事件最短持续天数（不足则不计入事件清单，但仍计入 count 栅格）。
    min_pixels : 事件最小空间像元数。

    返回
    ----
    dict：
        events : 事件列表（start_day/end_day/duration/n_pixels/peak_intensity/...）
        count_raster : (H, W) 每像元 exceedance 天数
        max_intensity : (H, W) 每像元最大异常强度（值−阈值，取绝对量纲）
        n_events : 满足过滤条件的事件数
        total_exceedance_pixel_days : 总 exceedance 像元·天
    """
    from scipy.ndimage import label as _label

    if cube.ndim != 3:
        raise ValidationError(
            f"cube must be 3-D (n_dates, H, W), got {cube.shape}",
            shape=tuple(cube.shape),
        )
    n, h, w = cube.shape
    thr = np.asarray(threshold, dtype=np.float32)
    # NaN-safe exceedance: NaN comparisons produce False, so NoData/NaN pixels
    # are never counted as exceedance.
    if upper:
        exc = cube > thr
    else:
        exc = cube < thr
    # Ensure bool even when cube contains NaN (NaN>thr -> False which is fine).
    exc = np.where(np.isfinite(cube), exc, False).astype(bool)

    count_raster = exc.sum(axis=0).astype(np.float32)

    # 异常强度（正值表示超出阈值的量）；NoData → 0，不计入 max。
    if upper:
        anomaly = cube - thr
    else:
        anomaly = thr - cube
    anomaly = np.where(exc, anomaly, 0.0).astype(np.float32)
    max_intensity = anomaly.max(axis=0).astype(np.float32)

    labeled, n_labels = _label(exc, structure=_connectivity_structure())

    events: List[Dict[str, Any]] = []
    for lab in range(1, n_labels + 1):
        coords = np.argwhere(labeled == lab)      # (M, 3) -> t, y, x
        ts = coords[:, 0]
        ys = coords[:, 1]
        xs = coords[:, 2]
        duration = int(ts.max() - ts.min() + 1)
        unique_pixels = len({(int(y), int(x)) for y, x in zip(ys, xs)})
        if duration < min_duration or unique_pixels < min_pixels:
            continue
        vals = anomaly[ts, ys, xs]
        event = {
            "event_id": len(events) + 1,
            "start_day": int(ts.min()),
            "end_day": int(ts.max()),
            "duration_days": duration,
            "n_pixels": int(unique_pixels),
            "n_pixel_days": int(coords.shape[0]),
            "peak_intensity": float(vals.max()),
            "mean_intensity": float(vals.mean()),
            "centroid_y": float(ys.mean()),
            "centroid_x": float(xs.mean()),
        }
        events.append(event)

    # 按峰值强度降序
    events.sort(key=lambda e: e["peak_intensity"], reverse=True)
    for i, e in enumerate(events):
        e["event_id"] = i + 1

    return {
        "events": events,
        "count_raster": count_raster,
        "max_intensity": max_intensity,
        "n_events": len(events),
        "total_exceedance_pixel_days": int(count_raster.sum()),
        "threshold_kind": "upper" if upper else "lower",
    }


# ---------------------------------------------------------------------------
# 合成数据：内嵌已知极端事件
# ---------------------------------------------------------------------------
def generate_synthetic_cube(
    bbox: List[float],
    variable: str = "temperature",
    n_dates: int = 30,
    width: int = 64,
    height: int = 64,
    seed: int = 42,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """生成含内嵌极端事件的 (n_dates, H, W) 立方体。

    temperature：基线 ~25°C，在第 10–19 天于东南象限注入持续热浪（+9°C）。
    precipitation：基线 ~2mm，在第 8–11 天于中部注入强降水（+25mm）。
    """
    rng = np.random.default_rng(seed)
    cube = np.zeros((n_dates, height, width), dtype=np.float32)
    yy, xx = np.mgrid[0:height, 0:width]
    yy = yy.astype(np.float32) / max(height - 1, 1)
    xx = xx.astype(np.float32) / max(width - 1, 1)

    if variable == "temperature":
        baseline = 25.0 + 3.0 * yy
        for k in range(n_dates):
            cube[k] = baseline + rng.normal(0, 1.0, (height, width)).astype(np.float32)
        # 注入热浪：第 10-19 天，东南象限
        y0, y1 = height // 2, height
        x0, x1 = width // 2, width
        ev_start, ev_end = 10, 19
        cube[ev_start:ev_end + 1, y0:y1, x0:x1] += 9.0
        injected = {
            "type": "heatwave", "start_day": ev_start, "end_day": ev_end,
            "y_range": [y0, y1], "x_range": [x0, x1], "magnitude": 9.0,
        }
    else:  # precipitation
        baseline = 2.0 + 1.0 * xx
        for k in range(n_dates):
            val = baseline + rng.normal(0, 0.8, (height, width)).astype(np.float32)
            cube[k] = np.clip(val, 0.0, None)
        # 注入暴雨：第 8-11 天，中部区域
        y0, y1 = height // 4, 3 * height // 4
        x0, x1 = width // 4, 3 * width // 4
        ev_start, ev_end = 8, 11
        cube[ev_start:ev_end + 1, y0:y1, x0:x1] += 25.0
        injected = {
            "type": "heavy_rain", "start_day": ev_start, "end_day": ev_end,
            "y_range": [y0, y1], "x_range": [x0, x1], "magnitude": 25.0,
        }

    info = {
        "bbox": bbox, "width": width, "height": height,
        "variable": variable, "n_dates": n_dates,
        "injected_event": injected,
    }
    return cube, info


# ---------------------------------------------------------------------------
# GeoTIFF I/O（直接用 rasterio，保证离线可用）
# ---------------------------------------------------------------------------
def write_geotiff(
    path: str,
    cube: np.ndarray,
    bbox: List[float],
    nodata: float = -9999.0,
) -> None:
    import rasterio
    from rasterio.transform import from_bounds

    if cube.ndim == 2:
        cube = cube[np.newaxis, ...]
    nb, h, w = cube.shape
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    profile = {
        "driver": "GTiff", "height": h, "width": w, "count": nb,
        "dtype": "float32", "crs": "EPSG:4326", "transform": transform,
        "nodata": nodata, "compress": "deflate",
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        for b in range(nb):
            dst.write(cube[b].astype("float32"), b + 1)


def read_geotiff(path: str) -> Tuple[np.ndarray, List[float]]:
    import rasterio
    if not os.path.exists(path):
        raise UsageError(f"input raster not found: {path}", path=path)
    with rasterio.open(path) as src:
        cube = src.read().astype(np.float32)
        b = src.bounds
        bbox = [b.left, b.bottom, b.right, b.top]
    return cube, bbox


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def write_manifest(
    output_dir: str,
    args: argparse.Namespace,
    outputs: List[Dict[str, Any]],
    qa: Dict[str, Any],
    started_at: str,
    exit_code: int,
    bbox: List[float],
) -> Optional[str]:
    if OutputManifest is None:
        return None
    cmd = " ".join([SKILL_NAME] + sys.argv[1:])
    man = OutputManifest(
        skill=SKILL_NAME,
        skill_version=VERSION,
        command=cmd,
        started_at=started_at,
        finished_at=_utc_now(),
        exit_code=exit_code,
        inputs={
            "input": getattr(args, "input", None),
            "variable": getattr(args, "variable", None),
            "threshold": getattr(args, "threshold", None),
            "synthetic": bool(getattr(args, "synthetic", False)),
        },
        outputs=[OutputFile(**o) for o in outputs],
        qa=qa,
        software={"python": sys.version.split()[0], "skill": SKILL_NAME},
    )
    path = os.path.join(output_dir, "output-manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(man.to_dict(), f, ensure_ascii=False, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def process(args: argparse.Namespace) -> int:
    started_at = _utc_now()
    output_dir = args.output_dir
    bbox = list(args.bbox) if args.bbox else None
    # bbox shape is validated up front (before any disk I/O or makedirs)
    if bbox is not None:
        validate_bbox(bbox)

    synth_info: Optional[Dict[str, Any]] = None
    n_valid = 0
    if args.input and not args.synthetic:
        if not os.path.exists(args.input):
            raise UsageError(f"input raster not found: {args.input}", path=args.input)
        cube, file_bbox, n_valid = read_geotiff_with_nodata(args.input)
        bbox = bbox if bbox is not None else file_bbox
        validate_bbox(bbox)
        source_note = args.input
    else:
        if bbox is None:
            raise UsageError("provide --bbox (synthetic mode) or --input <raster>")
        cube, synth_info = generate_synthetic_cube(
            bbox, variable=args.variable, n_dates=args.n_dates,
        )
        source_note = "synthetic"
        n_valid = int(np.sum(np.isfinite(cube)))

    if cube.size == 0:
        raise ValidationError("input raster is empty")
    if cube.ndim != 3:
        raise ValidationError(
            f"input must be a time-series cube (n_dates, H, W), got {cube.shape}",
            shape=tuple(cube.shape),
        )
    if n_valid == 0:
        raise ValidationError(
            "input raster has no valid (non-NoData) pixel steps",
            shape=tuple(cube.shape),
        )

    # 阈值与尾向
    pct, upper = parse_threshold(args.threshold)
    thr = percentile_threshold(cube, pct)

    # 最短持续天数：温度（热浪/寒潮）默认 3，降水默认 1
    if args.min_duration is not None:
        min_duration = args.min_duration
    else:
        min_duration = 3 if args.variable == "temperature" else 1

    result = detect_events(
        cube, thr, upper=upper, min_duration=min_duration,
        min_pixels=args.min_pixels,
    )

    # Only create output dir after all validations have passed
    os.makedirs(output_dir, exist_ok=True)

    # 写出产物
    out_tif = os.path.join(output_dir, "extreme_events.tif")
    write_geotiff(out_tif, np.stack([result["count_raster"],
                                     result["max_intensity"]], axis=0), bbox)

    event_list_path = os.path.join(output_dir, "event_list.json")
    payload = {
        "variable": args.variable,
        "source": source_note,
        "threshold": args.threshold,
        "threshold_percentile": pct,
        "threshold_kind": result["threshold_kind"],
        "min_duration": min_duration,
        "n_events": result["n_events"],
        "total_exceedance_pixel_days": result["total_exceedance_pixel_days"],
        "events": result["events"],
    }
    if synth_info is not None:
        payload["synthetic_injected_event"] = synth_info["injected_event"]
    with open(event_list_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    n_total_pixel_steps = int(cube.shape[0] * cube.shape[1] * cube.shape[2])
    qa: Dict[str, Any] = {
        "source": source_note,
        "variable": args.variable,
        "threshold": args.threshold,
        "n_events": result["n_events"],
        "total_exceedance_pixel_days": result["total_exceedance_pixel_days"],
        "min_duration": min_duration,
        "n_valid_pixel_steps": n_valid,
        "n_total_pixel_steps": n_total_pixel_steps,
    }
    if result["events"]:
        qa["strongest_peak_intensity"] = result["events"][0]["peak_intensity"]

    outputs = [
        {"path": out_tif, "kind": "raster", "crs_epsg": 4326,
         "bbox_wgs84": bbox, "band_count": 2},
        {"path": event_list_path, "kind": "json"},
    ]

    man_path = write_manifest(output_dir, args, outputs, qa, started_at, 0, bbox)

    if not args.quiet:
        print(f"[{SKILL_NAME}] source: {source_note}")
        print(f"[{SKILL_NAME}] variable: {args.variable}  threshold: {args.threshold} "
              f"({result['threshold_kind']})")
        print(f"[{SKILL_NAME}] events detected: {result['n_events']}")
        print(f"[{SKILL_NAME}] total exceedance pixel-days: "
              f"{result['total_exceedance_pixel_days']}")
        print(f"[{SKILL_NAME}] output: {out_tif}")
        if man_path:
            print(f"[{SKILL_NAME}] manifest: {man_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Percentile-threshold detection of heatwaves / cold spells / heavy rain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                   help="geographic extent [minLon minLat maxLon maxLat]")
    p.add_argument("--input", help="input multi-band time-series GeoTIFF (band=time step)")
    p.add_argument("--variable", default="temperature",
                   choices=["temperature", "precipitation"],
                   help="climate variable (default: temperature)")
    p.add_argument("--threshold", default="p90", choices=THRESHOLD_CHOICES,
                   help="percentile threshold (default: p90)")
    p.add_argument("--n-dates", type=int, default=30,
                   help="number of time steps for synthetic mode (default: 30)")
    p.add_argument("--min-duration", type=int, default=None,
                   help="min consecutive days for an event "
                        "(default: 3 for temperature, 1 for precipitation)")
    p.add_argument("--min-pixels", type=int, default=1,
                   help="min spatial pixels for an event (default: 1)")
    p.add_argument("--synthetic", action="store_true",
                   help="generate a synthetic series with an injected extreme event")
    p.add_argument("--output-dir", default="./output", help="output directory")
    p.add_argument("--quiet", action="store_true", help="suppress console output")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return process(args)
    except GeoSkillError as exc:
        print(f"[{SKILL_NAME}] ERROR [{exc.kind}] {exc.message}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[{SKILL_NAME}] ERROR {exc}", file=sys.stderr)
        return to_exit_code(exc)


if __name__ == "__main__":
    sys.exit(main())
