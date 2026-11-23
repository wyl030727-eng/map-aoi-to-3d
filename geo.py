#!/usr/bin/env python3
"""map3d - 框选地图区域，导出带地形的三维模型（GLB）。
CLI:
    python map3d.py --bbox 117.198,39.132,117.204,39.137 -o tianjin.glb
    python map3d.py --serve

数据源: OpenStreetMap（建筑，ODbL）、Mapzen Terrarium DEM（地形）。"""
import argparse
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


_HERE = os.path.dirname(os.path.abspath(__file__))
_LIBS = os.path.join(_HERE, '.pylibs')
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

import numpy as np
import trimesh
from PIL import Image
from shapely.geometry import Polygon, LineString

UA = 'map3d-mvp/0.1 (+local prototype)'


def PBR(rgba, roughness=0.9, double_sided=False):

    return trimesh.visual.material.PBRMaterial(
        baseColorFactor=rgba, metallicFactor=0.0, roughnessFactor=roughness,
        doubleSided=double_sided)


OVERPASS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass.private.coffee/api/interpreter',
]
TILE = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'
SAT = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
DEFAULT_HEIGHT = 9.6
MAX_KM2 = 9.0


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')


def _get(url, data=None, timeout=90, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={'User-Agent': UA})
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as exc:
            last = exc
            if i < tries - 1:
                time.sleep(1 + 2 * i)
    raise last


def _tile_png(z, x, y):
    path = os.path.join(CACHE, f'{z}_{x}_{y}.png')
    if os.path.exists(path):
        return open(path, 'rb').read()
    data = _get(TILE.format(z=z, x=x, y=y), timeout=60)
    os.makedirs(CACHE, exist_ok=True)
    open(path, 'wb').write(data)
    return data


def xy(lon, lat, lon0, lat0):
    """WGS84 -> 以 (lon0, lat0) 为原点的局部米制 ENU。"""
    return ((lon - lon0) * 111320.0 * math.cos(math.radians(lat0)),
            (lat - lat0) * 110540.0)


def parse_height(tags):
    for key in ('height', 'building:height'):
        m = re.search(r'\d+(?:\.\d+)?', str(tags.get(key, '')))
        if m:
            return min(max(float(m.group()), 1.0), 300.0)
    m = re.search(r'\d+(?:\.\d+)?', str(tags.get('building:levels', '')))
    if m:
        return min(max(float(m.group()) * 3.2, 1.0), 300.0)
    return None



HEIGHT_URL = 'https://dataverse.harvard.edu/api/access/datafile/11989540'
HEIGHT_DIR = os.path.join(CACHE, 'heights')
HEIGHT_CITY = 'Tianjin'
_HEIGHT_DS = None
_HEIGHT_FAILED = False


def _download_ranges(url, path, parts=8):
    """分段并行下载：Dataverse 单连接只有 ~20 KB/s，44 MB 串行要 40 分钟。"""
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Range': 'bytes=0-0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers['Content-Range'].split('/')[-1])
    tmp = path + '.parts'
    os.makedirs(tmp, exist_ok=True)
    chunk = (total + parts - 1) // parts

    def fetch(i):
        start, end = i * chunk, min(total - 1, (i + 1) * chunk - 1)
        part = os.path.join(tmp, f'{i:02d}')
        for _ in range(4):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            s = start + have
            if s > end:
                return
            rq = urllib.request.Request(url, headers={'User-Agent': UA, 'Range': f'bytes={s}-{end}'})
            with urllib.request.urlopen(rq, timeout=120) as r, open(part, 'ab') as f:
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    f.write(b)
            if os.path.getsize(part) >= end - start + 1:
                return
        raise IOError(f'part {i} incomplete')

    ts = [threading.Thread(target=fetch, args=(i,)) for i in range(parts)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    sizes = [os.path.getsize(os.path.join(tmp, f'{i:02d}')) for i in range(parts)]
    if sum(sizes) != total:
        raise IOError(f'height raster size mismatch {sum(sizes)} != {total}')
    with open(path, 'wb') as out:
        for i in range(parts):
            with open(os.path.join(tmp, f'{i:02d}'), 'rb') as g:
                out.write(g.read())
    for i in range(parts):
        os.remove(os.path.join(tmp, f'{i:02d}'))
    os.rmdir(tmp)


def _height_dataset():
    global _HEIGHT_DS, _HEIGHT_FAILED
    if _HEIGHT_DS is not None:
        return _HEIGHT_DS
    if _HEIGHT_FAILED:
        return None
    path = os.path.join(HEIGHT_DIR, HEIGHT_CITY + '.tiff')
    if not os.path.exists(path):
        try:
            os.makedirs(HEIGHT_DIR, exist_ok=True)
            print('  downloading 10m height raster (44 MB, one-off)...', flush=True)
            _download_ranges(HEIGHT_URL, path)
            print('  height raster ready', flush=True)
        except Exception as exc:
            _HEIGHT_FAILED = True
            print(f'  height raster unavailable: {exc}', flush=True)
            return None
    try:
        import rasterio
        _HEIGHT_DS = rasterio.open(path)
    except Exception as exc:
        _HEIGHT_FAILED = True
        print(f'  rasterio missing ({exc}), fallback to type heights', flush=True)
        return None
    return _HEIGHT_DS


def height_sampler(bbox):
    """返回 (poly_wgs84)->高度米 的函数；栅格不覆盖该 AOI 时返回 None。"""

    hb = (116.894, 38.959, 117.470, 39.342)
    if not (hb[0] <= bbox[2] and hb[2] >= bbox[0] and hb[1] <= bbox[3] and hb[3] >= bbox[1]):
        return None
    ds = _height_dataset()
    if ds is None:
        return None
    b = ds.bounds
    if not (b.left <= bbox[2] and b.right >= bbox[0] and b.bottom <= bbox[3] and b.top >= bbox[1]):
        return None
    band = ds.count
    from rasterio.windows import from_bounds
    from rasterio.features import geometry_mask
    from shapely.geometry import mapping
    stats = {'n': 0}

    def sample(poly):
        win = from_bounds(*poly.bounds, ds.transform)
        win = win.round_offsets().round_lengths()
        if win.width < 1 or win.height < 1:
            row, col = ds.index(poly.centroid.x, poly.centroid.y)
            if not (0 <= row < ds.height and 0 <= col < ds.width):
                return None
            win, a = ((row, row + 1), (col, col + 1)), ds.read(band, window=((row, row + 1), (col, col + 1)))
        else:
            if win.col_off < 0 or win.row_off < 0 or win.col_off + win.width > ds.width or win.row_off + win.height > ds.height:
                return None
            a = ds.read(band, window=win)
        if a.size == 0:
            return None
        try:
            m = geometry_mask([mapping(poly)], out_shape=a.shape,
                              transform=ds.window_transform(win), invert=True)
        except Exception:
            return None
        v = a[m & np.isfinite(a) & (a > 0.3)]
        if v.size == 0:

            try:
                m2 = geometry_mask([mapping(poly.buffer(abs(ds.transform.a) * 1.2))], out_shape=a.shape,
                                   transform=ds.window_transform(win), invert=True)
                v = a[m2 & np.isfinite(a) & (a > 0.3)]
            except Exception:
                pass
        if v.size == 0:
            row, col = ds.index(poly.centroid.x, poly.centroid.y)
            if 0 <= row < ds.height and 0 <= col < ds.width:
                patch = ds.read(band, window=((max(0, row - 1), min(ds.height, row + 2)),
                                              (max(0, col - 1), min(ds.width, col + 2))))
                pv = patch[np.isfinite(patch) & (patch > 0.3)]
                if pv.size:
                    v = pv
        if v.size == 0:
            return None
        h = float(np.percentile(v, 75)) if v.size >= 3 else float(v.max())
        if h <= 0.3:
            return None
        stats['n'] += 1
        return min(max(h, 1.0), 300.0)

    sample.stats = stats
    return sample


ROAD_WIDTH = {'motorway': 26, 'trunk': 22, 'primary': 18, 'secondary': 14, 'tertiary': 12,
              'residential': 9, 'unclassified': 9, 'living_street': 8, 'service': 5,
              'pedestrian': 6, 'footway': 3, 'path': 2.5, 'cycleway': 3, 'steps': 2, 'track': 4}


def fetch_osm(bbox, timeout=120):
    w, s, e, n = bbox
    q = ('[out:json][timeout:90];('
         f'way["building"]({s},{w},{n},{e});'
         f'way["highway"]({s},{w},{n},{e});'
         f'way["natural"="water"]({s},{w},{n},{e});'
         f'way["waterway"~"river|canal"]({s},{w},{n},{e});'
         f'relation["natural"="water"]({s},{w},{n},{e});'
         ');out geom;')
    key = hashlib.md5(f'{w:.6f},{s:.6f},{e:.6f},{n:.6f}'.encode()).hexdigest()[:10]
    path = os.path.join(CACHE, f'osm_{key}.json')
    if os.path.exists(path):
        raw = open(path, 'rb').read()
        print('  overpass cache hit', flush=True)
    else:
        body = urllib.parse.urlencode({'data': q}).encode()
        last = None
        for url in OVERPASS:
            try:
                raw = _get(url, body, timeout)
                data = json.loads(raw.decode('utf-8'))
                os.makedirs(CACHE, exist_ok=True)
                open(path, 'wb').write(raw)
                print(f'  overpass {url.split("/")[2]}: {len(data.get("elements", []))} elements', flush=True)
                break
            except Exception as exc:
                last = exc
                print(f'  overpass {url.split("/")[2]} failed: {exc}', flush=True)
        else:
            raise RuntimeError(f'Overpass 全部失败: {last}')

    data = json.loads(raw.decode('utf-8'))
    blds, roads, waters, rivers = [], [], [], []
    for el in data.get('elements', []):
        if el.get('type') != 'way':
            continue
        tags = el.get('tags') or {}
        geom = el.get('geometry') or []
        if len(geom) < 2:
            continue
        pts = [(g['lon'], g['lat']) for g in geom]
        if tags.get('building') and tags.get('building') != 'no' and len(pts) >= 3:
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            blds.append((pts, parse_height(tags), tags.get('name', ''), tags))
        elif tags.get('highway'):
            roads.append((pts, tags))
        elif tags.get('natural') == 'water' and len(pts) >= 3:
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            waters.append(pts)
        elif tags.get('waterway') in ('river', 'canal'):
            rivers.append((pts, tags))
    print(f'  osm: {len(blds)} buildings / {len(roads)} roads / {len(waters)} water / {len(rivers)} waterways', flush=True)
    return blds, roads, waters, rivers


def _tile_frac(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def fetch_dem(bbox, z=14, n=64):
    """返回 (elev[n,n], zmin, sampler(lon,lat)->米)。"""
    w, s, e, n_ = bbox
    xw, _ = _tile_frac(w, 0, z)
    xe, _ = _tile_frac(e, 0, z)
    _, yt = _tile_frac(0, n_, z)
    _, yb = _tile_frac(0, s, z)
    x0, x1 = int(math.floor(xw)), int(math.floor(xe))
    y0, y1 = int(math.floor(yt)), int(math.floor(yb))
    rows, cols = y1 - y0 + 1, x1 - x0 + 1
    mosaic = np.zeros((rows * 256, cols * 256), dtype=np.float32)
    for ty in range(rows):
        for tx in range(cols):
            img = Image.open(io.BytesIO(_tile_png(z, x0 + tx, y0 + ty))).convert('RGB')
            a = np.asarray(img, dtype=np.float32)
            mosaic[ty * 256:(ty + 1) * 256, tx * 256:(tx + 1) * 256] = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768

    H, W = mosaic.shape

    def sampler(lon, lat):
        fx, fy = _tile_frac(lon, lat, z)
        px = min(max((fx - x0) * 256.0, 0.0), W - 1.001)
        py = min(max((fy - y0) * 256.0, 0.0), H - 1.001)
        ix, iy = int(px), int(py)
        dx, dy = px - ix, py - iy
        p = mosaic[iy:iy + 2, ix:ix + 2]
        return float(p[0, 0] * (1 - dx) * (1 - dy) + p[0, 1] * dx * (1 - dy) +
                     p[1, 0] * (1 - dx) * dy + p[1, 1] * dx * dy)

    lon0, lat0 = (w + e) / 2, (s + n_) / 2
    elev = np.empty((n, n), dtype=np.float32)
    for j in range(n):
        lat = s + (n_ - s) * j / (n - 1)
        for i in range(n):
            elev[j, i] = sampler(w + (e - w) * i / (n - 1), lat)
    print(f'  dem z{z}: {rows}x{cols} tiles, {float(elev.min()):.1f}~{float(elev.max()):.1f} m', flush=True)
    return elev, float(elev.min()), sampler


def _sat_tile(z, x, y):
    p = os.path.join(CACHE, f'sat_{z}_{x}_{y}.jpg')
    if os.path.exists(p):
        return open(p, 'rb').read()
    data = _get(SAT.format(z=z, y=y, x=x), timeout=45, tries=2)
    os.makedirs(CACHE, exist_ok=True)
    open(p, 'wb').write(data)
    return data


def fetch_sat(bbox, max_tiles=36, target_px=2000):
    """下载 Esri 卫星影像并拼成覆盖 bbox 的一张贴图。"""
    w, s, e, n_ = bbox
    cos = math.cos(math.radians((s + n_) / 2))
    width_m = (e - w) * 111320.0 * cos
    z = int(round(math.log2(target_px * 156543.03392 * cos / max(width_m, 1e-6))))
    z = max(12, min(z, 19))
    while True:
        fx0, fy0 = _tile_frac(w, n_, z)
        fx1, fy1 = _tile_frac(e, s, z)
        x0, x1 = int(math.floor(fx0)), int(math.floor(fx1))
        y0, y1 = int(math.floor(fy0)), int(math.floor(fy1))
        cols, rows = x1 - x0 + 1, y1 - y0 + 1
        if cols * rows <= max_tiles or z <= 13:
            break
        z -= 1
    mosaic = Image.new('RGB', (cols * 256, rows * 256))
    for ty in range(rows):
        for tx in range(cols):
            img = Image.open(io.BytesIO(_sat_tile(z, x0 + tx, y0 + ty))).convert('RGB')
            mosaic.paste(img, (tx * 256, ty * 256))
    left = int(round((fx0 - x0) * 256))
    top = int(round((fy0 - y0) * 256))
    right = max(int(round((fx1 - x0) * 256)), left + 8)
    bottom = max(int(round((fy1 - y0) * 256)), top + 8)
    crop = mosaic.crop((left, top, right, bottom))
    print(f'  sat z{z}: {cols}x{rows} tiles -> {crop.size[0]}x{crop.size[1]}', flush=True)
    return crop


def terrain_mesh(elev, bbox, zmin, n=64):
    w, s, e, n_ = bbox
    lon0, lat0 = (w + e) / 2, (s + n_) / 2
    verts = []
    for j in range(n):
        lat = s + (n_ - s) * j / (n - 1)
        for i in range(n):
            lon = w + (e - w) * i / (n - 1)
            x, y = xy(lon, lat, lon0, lat0)
            verts.append([x, y, float(elev[j, i]) - zmin])
    faces = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            b, c, d = a + 1, a + n + 1, a + n
            faces.append([a, b, c])
            faces.append([a, c, d])
    return trimesh.Trimesh(vertices=np.array(verts), faces=np.array(faces), process=False)


def _lonlat(x, y, lon0, lat0):
    return (lon0 + x / (111320.0 * math.cos(math.radians(lat0))), lat0 + y / 110540.0)


def _ribbon(pts, width, sampler, zmin, lon0, lat0, clip):
    """折线按法线向两侧偏移成路面带。"""
    line = LineString([xy(lon, lat, lon0, lat0) for lon, lat in pts])
    inter = line.intersection(clip)
    segs = [inter] if inter.geom_type == 'LineString' else list(getattr(inter, 'geoms', []))
    verts, faces = [], []
    for seg in segs:
        if seg.geom_type != 'LineString':
            continue
        co = np.asarray(seg.coords, dtype=float)
        if len(co) < 2:
            continue
        d = np.diff(co, axis=0)
        nrm = np.stack([-d[:, 1], d[:, 0]], axis=1)
        ln = np.linalg.norm(nrm, axis=1)
        ln[ln == 0] = 1
        nrm = nrm / ln[:, None]
        vn = np.zeros_like(co)
        vn[:-1] += nrm
        vn[1:] += nrm
        ln2 = np.linalg.norm(vn, axis=1)
        ln2[ln2 == 0] = 1
        vn = vn / ln2[:, None]
        base = len(verts)
        hw = width / 2.0
        for p, nv in zip(co, vn):
            lon, lat = _lonlat(p[0], p[1], lon0, lat0)
            z = sampler(lon, lat) - zmin + 0.12
            verts.append([p[0] + nv[0] * hw, p[1] + nv[1] * hw, z])
            verts.append([p[0] - nv[0] * hw, p[1] - nv[1] * hw, z])
        for i in range(len(co) - 1):
            a = base + i * 2
            faces.append([a, a + 1, a + 2])
            faces.append([a + 1, a + 3, a + 2])
    return verts, faces


def _water_meshes(waters, rivers, sampler, zmin, lon0, lat0, clip):
    """水面：优先 OSM 水面多边形；没有多边形时用河道中线缓冲。"""
    polys = []
    for pts in waters:
        polys.append(Polygon([xy(lon, lat, lon0, lat0) for lon, lat in pts]).buffer(0))
    if not waters:
        for pts, tags in rivers:
            wdt = 24.0
            m = re.search(r'\d+(?:\.\d+)?', str(tags.get('width', '')))
            if m:
                wdt = min(max(float(m.group()), 6.0), 200.0)
            polys.append(LineString([xy(lon, lat, lon0, lat0) for lon, lat in pts]).buffer(wdt / 2.0))
    verts, faces, areas = [], [], 0
    for poly in polys:
        try:
            poly = poly.intersection(clip)
            if poly.is_empty or poly.area < 40:
                continue
            if poly.geom_type == 'MultiPolygon':
                poly = max(poly.geoms, key=lambda g: g.area)
            if poly.geom_type != 'Polygon':
                continue
            v2, f = trimesh.creation.triangulate_polygon(poly)
            ext = list(poly.exterior.coords)
            step = max(1, len(ext) // 12)
            zs = []
            for x, y in ext[::step]:
                lon, lat = _lonlat(x, y, lon0, lat0)
                zs.append(sampler(lon, lat) - zmin)
            wz = float(np.median(zs)) + 0.06
            base = len(verts)
            for x, y in v2:
                verts.append([float(x), float(y), wz])
            for tri in f:
                faces.append([int(t) + base for t in tri])
            areas += 1
        except Exception:
            continue
    return verts, faces, areas


def search_places(query, bbox, limit=20):
    w, s, e, n = bbox
    key = re.sub(r'["\\\n\r]', ' ', query).strip()[:40]
    if not key:
        return []
    q = ('[out:json][timeout:30];('
         f'node["name"~"{key}",i]({s},{w},{n},{e});'
         f'way["name"~"{key}",i]({s},{w},{n},{e});'
         f'relation["name"~"{key}",i]({s},{w},{n},{e});'
         f');out center {limit * 2};')
    body = urllib.parse.urlencode({'data': q}).encode()
    req = urllib.request.Request('https://overpass-api.de/api/interpreter', data=body,
                                 headers={'User-Agent': UA})
    raw = json.loads(urllib.request.urlopen(req, timeout=45).read().decode('utf-8'))
    out, seen = [], set()
    for el in raw.get('elements', []):
        tags = el.get('tags') or {}
        name = tags.get('name')
        if not name:
            continue
        lon = el.get('lon') if el.get('type') == 'node' else (el.get('center') or {}).get('lon')
        lat = el.get('lat') if el.get('type') == 'node' else (el.get('center') or {}).get('lat')
        if lon is None or lat is None:
            continue
        k = (name, round(lon, 4), round(lat, 4))
        if k in seen:
            continue
        seen.add(k)
        out.append({'name': name, 'lon': lon, 'lat': lat,
                    'kind': tags.get('place') or tags.get('highway') or tags.get('amenity') or ''})
    return out[:limit]


def build_scene(bbox, texture=False, roads=True, water=True):
    w, s, e, n_ = bbox
    lon0, lat0 = (w + e) / 2, (s + n_) / 2
    elev, zmin, sampler = fetch_dem(bbox)
    terrain = terrain_mesh(elev, bbox, zmin)
    half_w = (e - w) * 111320.0 * math.cos(math.radians(lat0)) / 2
    half_h = (n_ - s) * 110540.0 / 2
    clip = Polygon([(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)])

    blds, roads_raw, waters, rivers = fetch_osm(bbox)

    hs = height_sampler(bbox)
    h_src = {'osm': 0, 'raster': 0, 'type': 0}
    heights = []
    parts, skipped = [], 0
    for pts, h, name, tags in blds:
        try:
            poly_ll = Polygon([(lon, lat) for lon, lat in pts]).buffer(0)
            poly = Polygon([xy(lon, lat, lon0, lat0) for lon, lat in pts]).buffer(0)
            if poly.is_empty:
                skipped += 1
                continue
            poly = poly.intersection(clip)
            if poly.is_empty or poly.area < 4:
                skipped += 1
                continue
            if poly.geom_type == 'MultiPolygon':
                poly = max(poly.geoms, key=lambda g: g.area)
            if poly.geom_type != 'Polygon':
                skipped += 1
                continue
            c = poly.centroid
            bh = h
            if bh is not None:
                h_src['osm'] += 1
            elif hs is not None and not poly_ll.is_empty:
                bh = hs(poly_ll)
                if bh is not None:
                    h_src['raster'] += 1
            if bh is None:

                h_src['type'] += 1
                bh = {'apartments': 21.0, 'residential': 15.0, 'house': 9.0, 'commercial': 18.0,
                      'office': 27.0, 'retail': 12.0, 'industrial': 9.0, 'warehouse': 9.0,
                      'school': 15.0, 'hospital': 24.0}.get(tags.get('building'), 12.0)
            lon, lat = _lonlat(c.x, c.y, lon0, lat0)
            ground = sampler(lon, lat) - zmin - 0.3
            m = trimesh.creation.extrude_polygon(poly, bh)
            m.apply_translation([0, 0, ground])
            parts.append(m)
            heights.append(round(float(bh), 1))
        except Exception:
            skipped += 1

    buildings = trimesh.util.concatenate(parts) if parts else None
    if buildings is not None:

        buildings.visual = trimesh.visual.ColorVisuals(
            mesh=buildings, vertex_colors=np.full((len(buildings.vertices), 4), 255, dtype=np.uint8))
        buildings.visual.material = PBR([255, 255, 255, 255], roughness=0.9)

    road_v, road_f, road_km = [], [], 0.0
    if roads:
        for pts, tags in roads_raw:
            wdt = ROAD_WIDTH.get(tags.get('highway'), 8) * 0.7
            v, f = _ribbon(pts, wdt, sampler, zmin, lon0, lat0, clip)
            base = len(road_v)
            road_v.extend(v)
            road_f.extend([[i + base for i in t] for t in f])
            road_km += LineString([xy(lon, lat, lon0, lat0) for lon, lat in pts]).length
    roads_m = trimesh.Trimesh(np.array(road_v), np.array(road_f), process=False) if road_f else None

    water_v, water_f, water_n = ([], [], 0)
    if water:
        water_v, water_f, water_n = _water_meshes(waters, rivers, sampler, zmin, lon0, lat0, clip)
    water_m = trimesh.Trimesh(np.array(water_v), np.array(water_f), process=False) if water_f else None

    scene = trimesh.Scene()
    n_grid = elev.shape[0]
    uv = np.array([[i / (n_grid - 1), 1.0 - j / (n_grid - 1)]
                   for j in range(n_grid) for i in range(n_grid)], dtype=np.float64)
    tex = None
    if texture:
        try:
            tex = fetch_sat(bbox)
        except Exception as exc:
            print(f'  sat failed: {exc}', flush=True)
    if tex is not None:
        mat = PBR([255, 255, 255, 255], roughness=1.0)
        mat.baseColorTexture = tex
        terrain.visual = trimesh.visual.texture.TextureVisuals(uv=uv, material=mat)
    else:
        terrain.visual = trimesh.visual.ColorVisuals(mesh=terrain)
        terrain.visual.material = PBR([190, 191, 189, 255], roughness=1.0)
    scene.add_geometry(terrain, node_name='terrain', geom_name='terrain')
    if buildings is not None:
        scene.add_geometry(buildings, node_name='buildings', geom_name='buildings')
    if water_m is not None:
        water_m.visual = trimesh.visual.ColorVisuals(mesh=water_m)
        water_m.visual.material = PBR([255, 255, 255, 255], roughness=0.35, double_sided=True)
        scene.add_geometry(water_m, node_name='water', geom_name='water')
    if roads_m is not None:
        roads_m.visual = trimesh.visual.ColorVisuals(mesh=roads_m)
        roads_m.visual.material = PBR([255, 255, 255, 255], roughness=0.95, double_sided=True)
        scene.add_geometry(roads_m, node_name='roads', geom_name='roads')

    tris = len(terrain.faces)
    tris += 0 if buildings is None else len(buildings.faces)
    tris += 0 if water_m is None else len(water_m.faces)
    tris += 0 if roads_m is None else len(roads_m.faces)
    manifest = {
        'bbox': {'west': w, 'south': s, 'east': e, 'north': n_},
        'crs': 'EPSG:4326',
        'origin': {'lon': lon0, 'lat': lat0, 'elev': zmin},
        'up_axis': 'glTF Y-up',
        'buildings': 0 if buildings is None else len(parts),
        'skipped': skipped,
        'roads': len(roads_raw) if roads_m is not None else 0,
        'road_km': round(road_km / 1000.0, 2),
        'water_areas': water_n,
        'triangles': int(tris),
        'style': 'white massing + roads + water',
        'roads_enabled': roads,
        'water_enabled': water,
        'height_rule': 'OSM height/levels -> 10m 建筑高度栅格（Harvard Dataverse, 2022）-> 建筑类型固定经验值',
        'height_sources': h_src,
        'height_stats': {'min': min(heights), 'median': float(np.median(heights)), 'max': max(heights)} if heights else None,
        'height_dataset': ('Building Volume Panel of 106 Chinese Cities 2018-2023, '
                           'Harvard Dataverse doi:10.7910/DVN/3LTFEW (CC0)') if h_src['raster'] else None,
        'texture': 'Esri World Imagery' if tex is not None else None,
        'sources': ['OpenStreetMap (ODbL)', 'Mapzen Terrarium DEM']
                   + (['Harvard Dataverse 106-city building height (CC0)'] if h_src['raster'] else [])
                   + (['Esri World Imagery'] if tex is not None else []),
        'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    return scene, manifest


def build_glb(bbox, texture=False, roads=True, water=True):
    scene, manifest = build_scene(bbox, texture=texture, roads=roads, water=water)

    scene.apply_transform(trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0]))
    data = scene.export(file_type='glb')
    return data, manifest