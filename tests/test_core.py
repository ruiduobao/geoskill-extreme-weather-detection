"""Core algorithm tests for extreme-weather-detection."""
import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import mod


class TestParseThreshold:
    def test_upper_tail(self):
        pct, upper = mod.parse_threshold("p90")
        assert pct == 90.0
        assert upper is True

    def test_lower_tail(self):
        pct, upper = mod.parse_threshold("p10")
        assert pct == 10.0
        assert upper is False

    def test_p99_upper(self):
        _, upper = mod.parse_threshold("p99")
        assert upper is True

    def test_invalid_raises(self):
        with pytest.raises(mod.UsageError):
            mod.parse_threshold("bad")
        with pytest.raises(mod.UsageError):
            mod.parse_threshold("p150")


class TestPercentileThreshold:
    def test_per_pixel_value(self):
        # 每个像元序列 0..9，P90 = 8.1
        cube = np.tile(np.arange(10, dtype=np.float32)[:, None, None], (1, 4, 5))
        thr = mod.percentile_threshold(cube, 90.0)
        assert thr.shape == (4, 5)
        np.testing.assert_allclose(thr, 8.1, atol=1e-5)

    def test_wrong_ndim_raises(self):
        with pytest.raises(mod.ValidationError):
            mod.percentile_threshold(np.zeros((5, 5)), 90.0)


class TestConsecutiveRuns:
    def test_single_run(self):
        m = np.array([0, 0, 1, 1, 1, 0, 0], dtype=bool)
        assert mod.consecutive_runs(m) == [(2, 3)]

    def test_multiple_runs(self):
        m = np.array([1, 0, 1, 1, 0, 1], dtype=bool)
        assert mod.consecutive_runs(m) == [(0, 1), (2, 2), (5, 1)]

    def test_no_runs(self):
        assert mod.consecutive_runs(np.zeros(5, dtype=bool)) == []

    def test_all_true(self):
        assert mod.consecutive_runs(np.ones(4, dtype=bool)) == [(0, 4)]


class TestDetectEventsExplicitThreshold:
    def _make_cube(self):
        n, h, w = 20, 16, 16
        cube = np.full((n, h, w), 20.0, dtype=np.float32)
        # 注入：第 5-9 天（5 天），区域 [4:10, 4:10] 升温到 40
        cube[5:10, 4:10, 4:10] = 40.0
        return cube

    def test_exact_duration(self):
        """显式阈值 30 → 恰好检出 5 天持续的事件。"""
        cube = self._make_cube()
        res = mod.detect_events(cube, threshold=30.0, upper=True, min_duration=3)
        assert res["n_events"] == 1
        ev = res["events"][0]
        assert ev["duration_days"] == 5
        assert ev["start_day"] == 5
        assert ev["end_day"] == 9
        assert ev["n_pixels"] == 36          # 6x6
        assert ev["peak_intensity"] == pytest.approx(10.0)  # 40-30

    def test_min_duration_filters_short(self):
        """短于 min_duration 的 exceedance 不构成事件。"""
        cube = self._make_cube()
        res = mod.detect_events(cube, threshold=30.0, upper=True, min_duration=6)
        assert res["n_events"] == 0
        # 但 count 栅格仍记录 exceedance 天数
        assert res["count_raster"][5, 5] == 5

    def test_cold_spell_lower_tail(self):
        """下尾（寒潮）：低于阈值的连续低温。"""
        n, h, w = 15, 8, 8
        cube = np.full((n, h, w), 20.0, dtype=np.float32)
        cube[3:7, 2:5, 2:5] = 0.0   # 4 天低温
        res = mod.detect_events(cube, threshold=10.0, upper=False, min_duration=3)
        assert res["n_events"] == 1
        assert res["events"][0]["duration_days"] == 4
        assert res["events"][0]["peak_intensity"] == pytest.approx(10.0)

    def test_count_raster(self):
        cube = self._make_cube()
        res = mod.detect_events(cube, threshold=30.0, upper=True, min_duration=1)
        # 区域内每像元 exceedance 5 天，区域外 0
        assert res["count_raster"][6, 6] == 5
        assert res["count_raster"][0, 0] == 0

    def test_wrong_ndim_raises(self):
        with pytest.raises(mod.ValidationError):
            mod.detect_events(np.zeros((5, 5)), threshold=0.0)


class TestSyntheticDetection:
    def test_heatwave_detected(self):
        """合成热浪应被检出，事件时间窗与注入窗重叠。"""
        cube, info = mod.generate_synthetic_cube(
            [116, 39, 117, 40], variable="temperature", n_dates=30)
        thr = mod.percentile_threshold(cube, 90.0)
        res = mod.detect_events(cube, thr, upper=True, min_duration=3)
        assert res["n_events"] >= 1
        inj = info["injected_event"]
        # 至少一个事件与注入时间窗重叠
        top = res["events"][0]
        assert not (top["end_day"] < inj["start_day"] or
                    top["start_day"] > inj["end_day"])
        assert top["duration_days"] >= 3

    def test_heavy_rain_detected(self):
        cube, info = mod.generate_synthetic_cube(
            [116, 39, 117, 40], variable="precipitation", n_dates=30)
        thr = mod.percentile_threshold(cube, 99.0)
        res = mod.detect_events(cube, thr, upper=True, min_duration=1)
        assert res["n_events"] >= 1
        assert res["total_exceedance_pixel_days"] > 0

    def test_precipitation_nonnegative(self):
        cube, _ = mod.generate_synthetic_cube(
            [116, 39, 117, 40], variable="precipitation", n_dates=20)
        assert cube.min() >= 0.0


class TestGeoTiffIO:
    def test_roundtrip(self, tmp_path):
        cube = np.random.uniform(0, 10, (2, 16, 16)).astype(np.float32)
        bbox = [116.0, 39.0, 117.0, 40.0]
        path = str(tmp_path / "t.tif")
        mod.write_geotiff(path, cube, bbox)
        back, rb = mod.read_geotiff(path)
        assert back.shape == cube.shape
        np.testing.assert_allclose(rb, bbox, atol=1e-6)

    def test_read_missing_raises(self):
        with pytest.raises(mod.UsageError):
            mod.read_geotiff("/nonexistent/x.tif")
