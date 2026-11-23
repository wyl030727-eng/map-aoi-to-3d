# 地图选区转 3D 白模

在地图上框选任意区域，拉取 OpenStreetMap 的建筑轮廓，按真实高度挤出为白模，并生成路网和水体，导出 GLB。

默认白模：建筑、道路、水面全部为白色几何体，不贴任何图；地面浅灰。

## 文件

| 文件 | 作用 |
| --- | --- |
| `map3d.py` | Web 服务、前端界面、命令行入口 |
| `geo.py` | 数据抓取（Overpass / DEM / 高度栅格）与几何构建 |
| `requirements.txt` | Python 依赖 |

`map3d.py` 与 `geo.py` 必须放在同一目录。

## 安装

Python 3.10+：

```
pip install -r requirements.txt
```

## 运行

```
python map3d.py --serve
```

打开 http://127.0.0.1:8000 。

* 页面打开时自动请求浏览器定位，地图居中到当前位置（蓝点 + 精度圈）；带 `?bbox=` 深链时改为显示该选区。
* 顶部搜索框按名称检索当前视野及周边（外扩一倍）的 OSM 地点，点击结果跳转。
* 按住 Shift 在地图上拖拽画框，点“生成 GLB”，右侧预览并可下载。
* 示例按钮读取 `out/heping2.glb`，需要先在本地生成该文件。

命令行：

```
python map3d.py --bbox 117.1935,39.1215,117.1985,39.1255 -o heping2.glb
```

`--bbox` 顺序固定为西、南、东、北（WGS84 经纬度），面积上限 9 km²。

可选参数：

```
--texture    叠加卫星影像地面（默认关闭）
--no-roads   不生成路网（默认生成）
--no-water   不生成水面（默认生成）
--selftest   只跑内置自检
```

## 输出

* `<name>.glb`：局部 ENU 米制坐标，原点为选区中心，高程基准为选区内最低点，已转为 glTF Y-up。节点为 `terrain` / `buildings` / `roads` / `water`。
* `<name>.json`：manifest，含 bbox、origin、建筑数、跳过数、三角面数、高度规则、数据来源。

## 高度规则

按优先级取值：

1. OSM `height` / `building:height`
2. OSM `building:levels × 3.2`
3. 10 m 建筑高度栅格在轮廓内的 75 分位（小 footprint 先外扩 1 像素，再查 3×3 邻域）
4. 建筑类型经验值（住宅 15 m、商业 18 m、办公 27 m、学校 15 m、默认 12 m）

栅格来自 Harvard Dataverse 的 *Building Volume Panel of 106 Chinese Cities 2018-2023*（doi:10.7910/DVN/3LTFEW，CC0），取 2022 年波段，覆盖 106 个城市。首次构建会分段并行下载所在城市栅格到 `.cache/heights/`，之后离线复用。

## 实现要点

* 地形：Mapzen Terrarium DEM（z14 瓦片），64×64 网格双线性采样，浅灰纯色。
* 建筑：`shapely` 裁剪 + `trimesh` 挤出，底部贴合 DEM，埋深 0.3 m。
* 路网：按 `highway` 等级取宽度，折线法线偏移成路面带，白色。
* 水体：OSM 水面多边形三角化；无多边形时用 `waterway` 中线按宽度缓冲，白色。
* 坐标：底图高德为 GCJ-02，页面内做 WGS84 纠偏；框选结果按 WGS84 送后端。
* 缓存：瓦片与 Overpass 结果落盘 `.cache/`，同一 AOI 内存缓存最近 8 次。
* 材质：PBR 非金属，GLB 内 `images=0`、`textures=0`，只有 `baseColorFactor`。

## 已知限制

* 建筑只有轮廓拉伸，没有屋顶结构、女儿墙、立面细节。
* 10 m 高度栅格是遥感 + ML 估计（论文全国 RMSE 6.1 m），超高层会被明显压低，高层天际线不可信；需要精确高度只能接商业三维数据或城市测绘成果。
* 栅格只覆盖 106 个城市，其他区域依赖 OSM 标签，缺失时退回类型经验值。
* 超大街区（如把站台、轨道一起圈进来的火车站）会被整体挤出成大平台，这是 OSM 轮廓本身。
* 复杂多边形（带洞、自相交）可能只保留最大的一块。
* DEM 是地表模型，高楼可能被算进地面，导致局部起伏。
* 单进程构建，首次约 10–40 秒（拉 DEM / Overpass），缓存后 1 秒内。
* 公网部署前需补限流与数据合规声明。

## 数据来源

* 建筑轮廓、路网、水体：© OpenStreetMap contributors，ODbL。
* 地形：Mapzen Terrarium（AWS Open Data）。
* 建筑高度：Building Volume Panel of 106 Chinese Cities 2018-2023（Harvard Dataverse，doi:10.7910/DVN/3LTFEW，CC0）。
* 底图：高德瓦片仅用于本地原型展示；对外发布需按其服务条款授权并标注审图号。
* 可选卫星影像：Esri World Imagery，受 Esri 条款约束，仅限原型演示。
