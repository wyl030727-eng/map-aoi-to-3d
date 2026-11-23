import argparse
import json
import math
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from geo import MAX_KM2, _tile_frac, build_glb, parse_height, search_places, xy


HTML = r'''<!DOCTYPE html><html><head><meta charset="utf-8"><title>框选地图转 3D</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
body{margin:0;font:14px/1.6 "Microsoft YaHei",sans-serif;background:#0f1720;color:#e6edf3;height:100vh;display:flex;flex-direction:column;overflow:hidden}
#bar{padding:12px 18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;background:#152230;border-bottom:1px solid #1e2c3a}
#bar b{font-size:16px}
#main{flex:1;display:flex;min-height:0}
#m{width:55%;height:100%}
#view{flex:1;height:100%;background:#0b1116;border-left:1px solid #1e2c3a;overflow:hidden;position:relative}
#vhint{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);color:#5c6b7a;font-size:13px;pointer-events:none}
#view canvas{display:block}
button{padding:8px 16px;border:1px solid #d97706;background:#d97706;color:#fff;border-radius:6px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
#status{color:#9fb0c3}
#out a{color:#7cc4ff}
select,input{background:#1d2b3a;color:#e6edf3;border:1px solid #33465a;border-radius:6px;padding:6px 8px;font:13px "Microsoft YaHei",sans-serif}
#q{width:200px}
#find{background:transparent;border-color:#44576b;color:#c8d4e0}
#results{display:none;position:absolute;top:58px;left:18px;width:300px;max-height:320px;overflow:auto;background:#152230;border:1px solid #2a3b4d;border-radius:8px;z-index:9999}
#results div{padding:8px 12px;border-bottom:1px solid #1e2c3a;cursor:pointer}
#results div:hover{background:#1d2b3a}
</style>
<script type="importmap">{"imports":{"three":"https://unpkg.com/three@0.169.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.169.0/examples/jsm/"}}</script>
</head><body>
<div id="bar"><b>地图转 3D</b>
<input id="q" placeholder="搜索地点" autocomplete="off"><button id="find">搜索</button>
<select id="src"><option value="gaode">高德矢量</option><option value="gaode_sat">高德卫星</option><option value="esri">Esri 影像</option><option value="osm">OSM</option></select>
<span id="bbox">未选择</span><button id="go">生成 GLB</button><button id="demo" style="background:transparent;border-color:#44576b;color:#c8d4e0">看示例</button><span id="status"></span><span id="out"></span></div>
<div id="results"></div>
<div id="main">
<div id="m"></div>
<div id="view"><span id="vhint"></span></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script type="module">
import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';

const PI=Math.PI,AXIS=6378245.0,EE=0.00669342162296594323;
function outChina(lon,lat){return !(lon>=72.004&&lon<=137.8347&&lat>=0.82956&&lat<=55.8271);}
function _tLat(x,y){let r=-100+2*x+3*y+0.2*y*y+0.1*x*y+0.2*Math.sqrt(Math.abs(x));r+=(20*Math.sin(6*x*PI)+20*Math.sin(2*x*PI))*2/3;r+=(20*Math.sin(y*PI)+40*Math.sin(y/3*PI))*2/3;r+=(160*Math.sin(y/12*PI)+320*Math.sin(y*PI/30))*2/3;return r;}
function _tLon(x,y){let r=300+x+2*y+0.1*x*x+0.1*x*y+0.1*Math.sqrt(Math.abs(x));r+=(20*Math.sin(6*x*PI)+20*Math.sin(2*x*PI))*2/3;r+=(20*Math.sin(x*PI)+40*Math.sin(x/3*PI))*2/3;r+=(150*Math.sin(x/12*PI)+300*Math.sin(x/30*PI))*2/3;return r;}
function wgs2gcj(lon,lat){if(outChina(lon,lat))return[lon,lat];let dLat=_tLat(lon-105,lat-35),dLon=_tLon(lon-105,lat-35);const rad=lat/180*PI,m=Math.sin(rad),k=1-EE*m*m,sm=Math.sqrt(k);dLat=(dLat*180)/((AXIS*(1-EE))/(k*sm)*PI);dLon=(dLon*180)/(AXIS/sm*Math.cos(rad)*PI);return[lon+dLon,lat+dLat];}
function gcj2wgs(lon,lat){const g=wgs2gcj(lon,lat);return[lon*2-g[0],lat*2-g[1]];}
const SOURCES={
 gaode:{gcj:true,url:'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',sub:'1234',z:18,attr:'&copy; 高德地图'},
 gaode_sat:{gcj:true,url:'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',sub:'1234',z:18,attr:'&copy; 高德地图'},
 esri:{gcj:false,url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',z:19,attr:'&copy; Esri'},
 osm:{gcj:false,url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',z:19,attr:'&copy; OpenStreetMap'}
};
let SRC=SOURCES.gaode,layer=null;
function toMap(lon,lat){return SRC.gcj?wgs2gcj(lon,lat):[lon,lat];}
function fromMap(lon,lat){return SRC.gcj?gcj2wgs(lon,lat):[lon,lat];}
const c0=toMap(117.202,39.134);
const map=L.map('m').setView([c0[1],c0[0]],15);
let start=null,rect=null,sel=null,root=null;
function applySource(key){
  const c=map.getCenter(),w=fromMap(c.lng,c.lat);
  SRC=SOURCES[key]||SOURCES.gaode;
  if(layer){map.removeLayer(layer);layer=null;}
  if(!location.search.includes('notiles'))layer=L.tileLayer(SRC.url,{subdomains:SRC.sub||'abc',maxZoom:SRC.z,attribution:SRC.attr}).addTo(map);
  const n=toMap(w[0],w[1]);
  map.setView([n[1],n[0]],map.getZoom(),{animate:false});
  if(sel){const s=toMap(sel[0],sel[1]),q=toMap(sel[2],sel[3]);if(rect)map.removeLayer(rect);rect=L.rectangle([[s[1],s[0]],[q[1],q[0]]],{color:'#d97706',weight:2}).addTo(map);}
}
applySource('gaode');
document.getElementById('src').onchange=e=>applySource(e.target.value);
const out=document.getElementById('out'),bboxEl=document.getElementById('bbox'),statusEl=document.getElementById('status'),go=document.getElementById('go');
const qEl=document.getElementById('q'),resultsEl=document.getElementById('results');
let searchMark=null;
async function doSearch(){
  const k=qEl.value.trim();
  if(!k){resultsEl.style.display='none';return;}
  const b=map.getBounds(),sw=fromMap(b.getWest(),b.getSouth()),ne=fromMap(b.getEast(),b.getNorth());
  const dx=(ne[0]-sw[0])*0.5,dy=(ne[1]-sw[1])*0.5;
  const box=[sw[0]-dx,sw[1]-dy,ne[0]+dx,ne[1]+dy];
  resultsEl.style.display='block';resultsEl.textContent='搜索中…';
  try{
    const r=await fetch(`/search?q=${encodeURIComponent(k)}&bbox=${box[0]},${box[1]},${box[2]},${box[3]}`);
    if(!r.ok)throw 0;
    const items=await r.json();
    resultsEl.innerHTML='';
    if(!items.length){resultsEl.textContent='无结果';return;}
    items.forEach(it=>{
      const d=document.createElement('div');d.textContent=it.name;
      d.onclick=()=>{
        const g=toMap(it.lon,it.lat);
        map.setView([g[1],g[0]],17);
        if(searchMark)map.removeLayer(searchMark);
        searchMark=L.circleMarker([g[1],g[0]],{radius:7,color:'#d97706',fillColor:'#d97706',fillOpacity:.9}).addTo(map);
        resultsEl.style.display='none';
      };
      resultsEl.appendChild(d);
    });
  }catch(e){resultsEl.textContent='搜索失败';}
}
document.getElementById('find').onclick=doSearch;
qEl.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();doSearch();}});
function locateMe(){
  if(!navigator.geolocation)return;
  navigator.geolocation.getCurrentPosition(p=>{
    const g=toMap(p.coords.longitude,p.coords.latitude);
    map.setView([g[1],g[0]],16);
    L.circleMarker([g[1],g[0]],{radius:7,color:'#0b84ff',fillColor:'#0b84ff',fillOpacity:.9}).addTo(map);
    L.circle([g[1],g[0]],{radius:Math.min(p.coords.accuracy||40,200),color:'#0b84ff',weight:1,fillOpacity:.08}).addTo(map);
  },()=>{}, {enableHighAccuracy:true,timeout:8000,maximumAge:60000});
}
function meters(b){const dy=(b[3]-b[1])*110540,dx=(b[2]-b[0])*111320*Math.cos((b[1]+b[3])/2*Math.PI/180);return [dx,dy];}
map.on('mousedown',e=>{if(!e.originalEvent.shiftKey)return;start=e.latlng;map.dragging.disable();if(rect)map.removeLayer(rect);rect=L.rectangle([start,start],{color:'#d97706',weight:2}).addTo(map);});
map.on('mousemove',e=>{if(start&&rect)rect.setBounds(L.latLngBounds(start,e.latlng));});
map.on('mouseup',()=>{if(!start)return;const b=rect.getBounds();const sw=fromMap(b.getWest(),b.getSouth()),ne=fromMap(b.getEast(),b.getNorth());sel=[sw[0],sw[1],ne[0],ne[1]];const m=meters(sel);bboxEl.textContent=`${m[0].toFixed(0)} × ${m[1].toFixed(0)} m`;start=null;map.dragging.enable();});

const host=document.getElementById('view');
let renderer=null,scene=null,camera=null,controls=null,sun=null;
function initView(){
  if(renderer)return;
  scene=new THREE.Scene();scene.background=new THREE.Color(0x0b1116);
  camera=new THREE.PerspectiveCamera(38,1,1,20000);
  scene.add(new THREE.HemisphereLight(0xdfe9f2,0x6b6a63,0.5));
  scene.add(new THREE.AmbientLight(0xffffff,0.14));
  sun=new THREE.DirectionalLight(0xfff2e0,1.9);sun.position.set(-400,700,500);
  sun.castShadow=true;sun.shadow.mapSize.set(1024,1024);sun.shadow.bias=-0.0004;
  scene.add(sun);scene.add(sun.target);
  renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(devicePixelRatio);
  renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.0;
  host.appendChild(renderer.domElement);
  controls=new OrbitControls(camera,renderer.domElement);
  controls.enableDamping=false;
  controls.screenSpacePanning=true;
  controls.maxPolarAngle=Math.PI*0.495;
  controls.addEventListener('change',()=>renderer.render(scene,camera));
  const fit=()=>{renderer.setSize(host.clientWidth,host.clientHeight,false);camera.aspect=host.clientWidth/host.clientHeight;camera.updateProjectionMatrix();renderer.render(scene,camera);};
  addEventListener('resize',fit);host.__fit=fit;fit();
}
function showGLB(url){
  initView();
  document.getElementById('vhint').style.display='none';
  new GLTFLoader().load(url,g=>{
    if(root){scene.remove(root);}
    root=g.scene;scene.add(root);
    const box=new THREE.Box3().setFromObject(root),c=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3());
    const r=Math.max(size.x,size.z,size.y);
    const radius=0.5*Math.sqrt(size.x*size.x+size.y*size.y+size.z*size.z);
    const span=0.75*Math.max(size.x,size.z)+size.y;
    sun.position.set(c.x-span*0.9,c.y+size.y+span*1.4,c.z+span*1.1);
    sun.target.position.copy(c);sun.target.updateMatrixWorld();
    const sc=sun.shadow.camera;
    sc.left=-span;sc.right=span;sc.top=span;sc.bottom=-span;
    sc.near=1;sc.far=span*6;sc.updateProjectionMatrix();
    sun.shadow.normalBias=Math.max(0.8,span*0.01);
    root.traverse(o=>{if(!o.isMesh)return;o.castShadow=true;o.receiveShadow=true;
      const mm=o.material;if(mm&&mm.map){mm.map.anisotropy=8;mm.map.needsUpdate=true;}});
    const aspect=Math.max(host.clientWidth/host.clientHeight,0.2);
    const vf=camera.fov*Math.PI/180,hf=2*Math.atan(Math.tan(vf/2)*aspect);
    const d=Math.max(radius/Math.sin(vf/2),radius/Math.sin(hf/2))*1.08;
    const dir=new THREE.Vector3(0.85,0.6,1.25).normalize();
    controls.target.copy(c);
    controls.minDistance=r*0.12;controls.maxDistance=r*8;
    camera.position.copy(c).addScaledVector(dir,d);
    controls.update();
    host.__fit();
  },undefined,err=>{const h=document.getElementById('vhint');h.style.display='';h.textContent='加载失败: '+err;});
}
go.onclick=async()=>{
 if(!sel){statusEl.textContent='请框选区域';return;}
 go.disabled=true;out.textContent='';statusEl.textContent='生成中…';
 try{
  const r=await fetch('/build',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bbox:sel})});
  if(!r.ok){statusEl.textContent=await r.text();return;}
  const b=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='map3d.glb';
  a.textContent=`下载 map3d.glb（${(b.size/1048576).toFixed(2)} MB）`;out.appendChild(a);
  const st=r.headers.get('X-Stats');statusEl.textContent='完成'+(st?' · '+st:'');
  showGLB(a.href);
 }catch(err){statusEl.textContent='失败: '+err;}finally{go.disabled=false;}
};
const q=new URLSearchParams(location.search);
const demoBtn=document.getElementById('demo');
demoBtn.onclick=()=>{out.textContent='';statusEl.textContent='示例';showGLB('/sample');};
if(!q.get('bbox'))locateMe();
if(q.get('sample'))demoBtn.click();
if(q.get('bbox')){
  const v=q.get('bbox').split(',').map(Number);
  sel=v;
  const s=toMap(v[0],v[1]),n=toMap(v[2],v[3]);
  const b=L.latLngBounds([[s[1],s[0]],[n[1],n[0]]]);
  if(rect)map.removeLayer(rect);
  rect=L.rectangle(b,{color:'#d97706',weight:2}).addTo(map);
  map.fitBounds(b,{padding:[40,40]});
  const m=meters(v);bboxEl.textContent=`${m[0].toFixed(0)} × ${m[1].toFixed(0)} m`;
  if(q.get('auto'))go.click();
}
</script></body></html>'''


def validate(bbox):
    if len(bbox) != 4:
        raise ValueError('bbox 需要 4 个值：w,s,e,n')
    w, s, e, n = bbox
    if not (w < e and s < n):
        raise ValueError('bbox 顺序应为 w,s,e,n，且 w<e、s<n')
    dx = (e - w) * 111.32 * math.cos(math.radians((s + n) / 2))
    dy = (n - s) * 110.54
    if dx * dy > MAX_KM2 * 1e6:
        raise ValueError(f'区域过大（{dx * dy / 1e6:.1f} km²），请控制在 {MAX_KM2:.0f} km² 以内')


_CACHE = {}


def build_cached(bbox, texture=False, roads=True, water=True):

    key = (tuple(round(float(x), 6) for x in bbox), bool(texture), bool(roads), bool(water))
    if key in _CACHE:
        print('  cache hit', key, flush=True)
        return _CACHE[key]
    glb, manifest = build_glb(bbox, texture=texture, roads=roads, water=water)
    if len(_CACHE) >= 8:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (glb, manifest)
    return glb, manifest


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == '/search':
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get('q') or [''])[0].strip()
            raw = (qs.get('bbox') or [''])[0]
            try:
                bbox = [float(x) for x in raw.split(',')]
                if len(bbox) != 4 or not q:
                    raise ValueError
                res = search_places(q, bbox)
            except Exception:
                return self._send(400, b'bad search', 'text/plain; charset=utf-8')
            return self._send(200, json.dumps(res, ensure_ascii=False).encode('utf-8'),
                              'application/json; charset=utf-8')
        if path == '/sample':
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out', 'heping2.glb')
            if not os.path.exists(p):
                return self._send(404, b'no sample', 'text/plain')
            return self._send(200, open(p, 'rb').read(), 'model/gltf-binary')
        if path != '/':
            return self._send(404, b'not found', 'text/plain')
        self._send(200, HTML.encode('utf-8'), 'text/html; charset=utf-8')

    def do_POST(self):
        if self.path.split('?')[0] != '/build':
            return self._send(404, b'not found', 'text/plain')
        try:
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))
            bbox = [float(x) for x in data['bbox']]
            validate(bbox)
            glb, manifest = build_cached(bbox, texture=bool(data.get('texture')),
                                         roads=bool(data.get('roads', True)),
                                         water=bool(data.get('water', True)))
            self._send(200, glb, 'model/gltf-binary',
                       {'Content-Disposition': 'attachment; filename="map3d.glb"',
                        'X-Stats': f'{manifest["buildings"]} buildings, '
                                   f'{manifest["height_sources"]["raster"]} real-height, {manifest["triangles"]} tris'})
        except Exception as exc:
            self._send(400, str(exc).encode('utf-8'), 'text/plain; charset=utf-8')

    def log_message(self, fmt, *args):
        print('  http', fmt % args, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bbox', help='w,s,e,n（经纬度）')
    ap.add_argument('-o', '--out', default='map3d.glb')
    ap.add_argument('--serve', action='store_true')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--texture', action='store_true', help='叠加卫星影像（默认白模）')
    ap.add_argument('--no-roads', dest='roads', action='store_false', help='不生成路网（默认生成）')
    ap.add_argument('--no-water', dest='water', action='store_false', help='不生成水面（默认生成）')
    args = ap.parse_args()

    if args.selftest:
        assert parse_height({}) is None
        assert parse_height({'height': '12 m'}) == 12
        assert abs(parse_height({'building:levels': '6'}) - 19.2) < 1e-6
        assert abs(xy(117.203, 39.134, 117.2, 39.13)[0] - 259.1) < 1.0
        assert abs(_tile_frac(117.2, 39.13, 14)[0] - 13526.2) < 1.0
        print('selftest ok')
        return

    if args.serve:
        srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        print(f'http://127.0.0.1:{args.port}', flush=True)
        srv.serve_forever()
        return

    if not args.bbox:
        ap.error('需要 --bbox 或 --serve')
    bbox = [float(x) for x in args.bbox.split(',')]
    validate(bbox)
    t0 = time.time()
    glb, manifest = build_glb(bbox, texture=args.texture, roads=args.roads, water=args.water)
    open(args.out, 'wb').write(glb)
    open(os.path.splitext(args.out)[0] + '.json', 'w', encoding='utf-8').write(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f'{args.out}: {len(glb)/1048576:.2f} MB, {manifest["buildings"]} buildings, '
          f'{manifest["triangles"]} tris, {time.time()-t0:.1f}s', flush=True)


if __name__ == '__main__':
    main()
