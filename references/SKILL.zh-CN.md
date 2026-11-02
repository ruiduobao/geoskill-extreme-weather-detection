# 极端天气事件检测 | Extreme Weather Detection

基于**百分位阈值法**从温度 / 降水时间序列中检测极端天气事件，并逐个统计
其强度、持续时间与空间范围。适用于热浪 / 寒潮 / 暴雨事件编目、极端气候
风险筛查与灾害快速评估。

检测规则：

- **热浪**（heatwave）：温度高于高分位阈值（如 P90）且连续 ≥ 3 天。
- **寒潮**（cold spell）：温度低于低分位阈值（如 P10）。
- **暴雨**（heavy rainfall）：降水高于 P95 / P99。

阈值默认逐像元由序列自身的分位数确定（百分位 ≥ 50 为上尾极端，< 50 为
下尾极端）。事件通过在 (时间, y, x) 三维 exceedance 体上做连通分量标记
（scipy.ndimage.label，时间相邻 + 四邻域空间连通）提取，逐个统计起止时间、
持续天数、峰值 / 平均强度、空间像元数与质心。

内置 `--synthetic` 模式生成内嵌已知极端事件（持续热浪 / 强降水）的模拟
序列，用于离线验证检测正确性。

## 依赖

```bash
pip install numpy rasterio scipy
```

## 使用方法

### 基本用法（合成数据，离线）

```bash
python geoskill-extreme-weather-detection.py --bbox 116.0 39.0 117.0 40.0 --output-dir ./output
```

### 示例 1：热浪检测（温度 P90，连续 ≥3 天）

```bash
python geoskill-extreme-weather-detection.py --bbox 116 39 117 40 --variable temperature --threshold p90 --n-dates 30 --output-dir ./heatwave
```

### 示例 2：暴雨检测（降水 P99）

```bash
python geoskill-extreme-weather-detection.py --bbox 121 31 122 32 --variable precipitation --threshold p99 --output-dir ./heavy_rain
```

### 示例 3：寒潮检测（温度 P10 下尾）

```bash
python geoskill-extreme-weather-detection.py --bbox 116 39 117 40 --variable temperature --threshold p10 --output-dir ./cold_spell
```

### 示例 4：真实多期栅格 + 自定义最短持续天数

```bash
python geoskill-extreme-weather-detection.py --input temp_daily.tif --variable temperature --threshold p95 --min-duration 5 --output-dir ./real_events
```

### 示例 5：仅 bbox 自动合成 + 静默

```bash
python geoskill-extreme-weather-detection.py --bbox 110 30 111 31 --variable precipitation --threshold p95 --output-dir ./auto --quiet
```

## 输出

| 文件 | 格式 | 说明 |
|---|---|---|
| `extreme_events.tif` | GeoTIFF (float32, 2 band) | band1=每像元 exceedance 天数，band2=最大异常强度，EPSG:4326 |
| `event_list.json` | JSON | 事件清单（起止/持续/强度/空间范围）+ 汇总 |
| `output-manifest.json` | JSON | 运行清单（输入/输出/QA/软件版本） |

## 数据源 / Source

- **输入模式**：本地多期 GeoTIFF（每波段 = 一个时间步）。
- **合成模式**：本地生成，内嵌已知极端事件，无外部数据源。

## 隐私声明 / Privacy

- 默认完全离线运行，不发起任何网络请求。
- `--synthetic` 模式不读取任何外部数据。
- 所有计算在本地完成，不上传用户数据。

## License

MIT
