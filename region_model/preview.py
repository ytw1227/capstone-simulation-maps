"""Offline preview and OBJ export for flat-ground building-footprint models.

Only verified, positive heights produce volumes. Missing heights remain visible
as orange footprints in the preview and are explicitly excluded from OBJ export.
All input geometry is already in the map's local coordinate system, in metres.
"""

from __future__ import annotations

from collections import Counter
from html import escape
import math
from pathlib import Path
import re
from typing import Iterable

import plotly.graph_objects as go
import plotly.io as pio
from shapely import constrained_delaunay_triangles
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient


SOURCE_LABELS = {"gis": "GIS 속성 높이", "ledger": "동일 건물 대장 높이", "missing": "높이 미확인"}
SOURCE_COLORS = {"gis": "#8398b4", "ledger": "#38877f", "missing": "#ed982e"}
BACKGROUND_STYLES = {
    "landuse": ("토지이용", "#e8e5d9", 0.01),
    "park": ("공원·녹지", "#bcd2b5", 0.02),
    "water": ("지표 수계", "#9bc5db", 0.03),
    "road": ("도로", "#b4bcc3", 0.04),
}


def _polygon_parts(geometry) -> Iterable[Polygon]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geometry.geoms:
            yield from _polygon_parts(part)


def _line_parts(geometry):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type in ("LineString", "LinearRing"):
        yield geometry
    elif geometry.geom_type in ("MultiLineString", "GeometryCollection"):
        for part in geometry.geoms:
            yield from _line_parts(part)


def _known_height(row) -> float | None:
    try:
        height = float(row.get("height_m"))
    except (TypeError, ValueError):
        return None
    source = str(row.get("height_source", "missing"))
    if not math.isfinite(height) or height <= 0 or source not in ("gis", "ledger"):
        return None
    return height


def _roof_triangles(polygon: Polygon):
    """Constrained triangles preserve both concave boundaries and courtyard holes."""
    if not polygon.is_valid:
        raise ValueError("Preview requires valid polygons; repair and record geometry upstream.")
    for triangle in constrained_delaunay_triangles(polygon).geoms:
        coordinates = [(float(x), float(y)) for x, y, *_ in list(triangle.exterior.coords)[:3]]
        a, b, c = coordinates
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if cross < 0:
            coordinates[1], coordinates[2] = coordinates[2], coordinates[1]
        if cross != 0:
            yield coordinates


def polygon_mesh(polygon: Polygon, height: float):
    """Return vertices and outward-wound triangular faces of a watertight extrusion.

    Units are metres. This helper accepts one valid Polygon, including interiors;
    callers iterate MultiPolygon parts. Roof and bottom use constrained triangles.
    """
    if not math.isfinite(height) or height <= 0:
        raise ValueError("Extrusion height must be a finite, positive number of metres.")
    if polygon.is_empty:
        return [], []
    polygon = orient(polygon, sign=1.0)
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    indices: dict[tuple[float, float, float], int] = {}

    def vertex(x, y, z):
        key = (float(x), float(y), float(z))
        if key not in indices:
            indices[key] = len(vertices)
            vertices.append(key)
        return indices[key]

    for triangle in _roof_triangles(polygon):
        top = tuple(vertex(x, y, height) for x, y in triangle)
        bottom = tuple(vertex(x, y, 0) for x, y in triangle)
        faces.append(top)
        faces.append((bottom[2], bottom[1], bottom[0]))
    for ring in (polygon.exterior, *polygon.interiors):
        coordinates = list(ring.coords)
        for p, q in zip(coordinates, coordinates[1:]):
            if p[:2] == q[:2]:
                continue
            p0, q0 = vertex(*p[:2], 0), vertex(*q[:2], 0)
            p1, q1 = vertex(*p[:2], height), vertex(*q[:2], height)
            faces.extend(((p0, q0, q1), (p0, q1, p1)))
    return vertices, faces


class _Mesh:
    """Batch disjoint polygon meshes while retaining per-building hover text."""

    def __init__(self):
        self.vertices = []
        self.faces = []
        self.text = []

    def add(self, vertices, faces, label=""):
        offset = len(self.vertices)
        self.vertices.extend(vertices)
        self.faces.extend(tuple(index + offset for index in face) for face in faces)
        self.text.extend([label] * len(vertices))

    def add_flat(self, polygon, z, label=""):
        for triangle in _roof_triangles(polygon):
            self.add([(x, y, z) for x, y in triangle], [(0, 1, 2)], label)

    def trace(self, *, name, color, opacity=1.0, showlegend=True, hover=True):
        x, y, z = zip(*self.vertices)
        i, j, k = zip(*self.faces)
        return go.Mesh3d(
            x=x, y=y, z=z, i=i, j=j, k=k,
            text=self.text, hovertemplate="%{text}<extra></extra>" if hover else None,
            hoverinfo=None if hover else "skip", name=name, color=color,
            flatshading=True, opacity=opacity, showlegend=showlegend,
            lighting={"ambient": 0.72, "diffuse": 0.75, "specular": 0.12, "roughness": 0.9},
            lightposition={"x": -1000, "y": -1200, "z": 2000},
        )


def _outline_add(target, geometry, z):
    for polygon in _polygon_parts(geometry):
        for ring in (polygon.exterior, *polygon.interiors):
            for coordinate in ring.coords:
                target[0].append(float(coordinate[0]))
                target[1].append(float(coordinate[1]))
                target[2].append(float(z))
            for values in target:
                values.append(None)


def _clean_text(value):
    return "" if value is None or str(value) in ("nan", "<NA>", "None") else str(value)


def is_tunnel_background(row):
    """Hide explicitly tagged tunnels from the flat surface, preserving data.

    OSM layer is relative stacking order, so a negative layer alone must not
    be interpreted as a surveyed underground elevation.
    """
    return _clean_text(row.get("tunnel")).strip().lower() not in ("", "no", "false", "0")


def _background_traces(background):
    traces = []
    if background is None or background.empty:
        return traces
    for kind, (label, color, z) in BACKGROUND_STYLES.items():
        mesh = _Mesh()
        lines = ([], [], [])
        subset = background.loc[background["kind"] == kind]
        for _, row in subset.iterrows():
            if is_tunnel_background(row):
                continue
            for polygon in _polygon_parts(row.geometry):
                mesh.add_flat(polygon, z, label)
            for line in _line_parts(row.geometry):
                for coordinate in line.coords:
                    lines[0].append(float(coordinate[0]))
                    lines[1].append(float(coordinate[1]))
                    lines[2].append(z)
                for values in lines:
                    values.append(None)
        if mesh.faces:
            traces.append(mesh.trace(name=label, color=color, showlegend=False, hover=False))
        if lines[0]:
            traces.append(go.Scatter3d(
                x=lines[0], y=lines[1], z=lines[2], mode="lines", name=label,
                line={"color": color, "width": 4 if kind == "road" else 2},
                showlegend=False, hoverinfo="skip",
            ))
    return traces


def write_preview(buildings, background, metadata: dict, output_path: Path) -> None:
    """Write a self-contained Korean HTML preview, with no runtime network calls."""
    size = float(metadata["size_m"])
    bounds = metadata.get("local_bounds", [-size / 2, -size / 2, size / 2, size / 2])
    xmin, ymin, xmax, ymax = map(float, bounds)
    region = str(metadata.get("region_name", "지역 모델"))
    is_demo = bool(metadata.get("is_demo", False))
    meshes = {source: _Mesh() for source in SOURCE_LABELS}
    outlines = {source: ([], [], []) for source in SOURCE_LABELS}
    counts = Counter()
    quality_flags = Counter()
    max_height = 0.0
    missing_rows = []

    for _, row in buildings.iterrows():
        geometry = row.geometry
        height = _known_height(row)
        source = str(row.get("height_source")) if height is not None else "missing"
        counts[source] += 1
        building_id = _clean_text(row.get("building_id", "식별자 없음"))
        flags = _clean_text(row.get("quality_flags"))
        for flag in re.split(r"[;|,]", flags):
            if flag.strip():
                quality_flags[flag.strip()] += 1
        height_text = f"{height:,.1f} m" if height is not None else "미확인 · 부피 생성 제외"
        label = (
            f"<b>{escape(building_id)}</b><br>높이: {height_text}"
            f"<br>높이 출처: {SOURCE_LABELS[source]}"
            + (f"<br>품질: {escape(flags)}" if flags else "")
            + ("<br><b>합성 예시 건물 · 실제 GIS 아님</b>" if is_demo else "")
        )
        if height is None:
            missing_rows.append((building_id, flags or "높이 미확인"))
        else:
            max_height = max(max_height, height)
        for polygon in _polygon_parts(geometry):
            if height is None:
                meshes[source].add_flat(polygon, 0.12, label)
            else:
                vertices, faces = polygon_mesh(polygon, height)
                meshes[source].add(vertices, faces, label)
        _outline_add(outlines[source], geometry, height if height is not None else 0.16)

    ground = _Mesh()
    ground.add(
        [(xmin, ymin, 0), (xmax, ymin, 0), (xmax, ymax, 0), (xmin, ymax, 0)],
        [(0, 1, 2), (0, 2, 3)],
    )
    figure = go.Figure([ground.trace(name="평면 지면", color="#f0f1ec", showlegend=False, hover=False)])
    figure.add_traces(_background_traces(background))
    for source, mesh in meshes.items():
        if mesh.faces:
            figure.add_trace(mesh.trace(name=SOURCE_LABELS[source], color=SOURCE_COLORS[source]))
        lines = outlines[source]
        if lines[0]:
            figure.add_trace(go.Scatter3d(
                x=lines[0], y=lines[1], z=lines[2], mode="lines",
                line={"color": "#b96a0c" if source == "missing" else "#52657b", "width": 4 if source == "missing" else 1},
                hoverinfo="skip", showlegend=False, name=SOURCE_LABELS[source] + " 외곽선",
            ))
    figure.add_trace(go.Scatter3d(
        x=[xmin, xmax, xmax, xmin, xmin], y=[ymin, ymin, ymax, ymax, ymin],
        z=[0.2] * 5, mode="lines", line={"color": "#657687", "width": 3, "dash": "dot"},
        hoverinfo="skip", showlegend=False, name="분석 영역",
    ))
    figure.update_layout(
        template="plotly_white", paper_bgcolor="#ffffff", font={"family": "Malgun Gothic, Segoe UI, sans-serif", "color": "#243447"},
        margin={"l": 0, "r": 0, "t": 40, "b": 0}, height=650,
        scene={
            "xaxis": {"title": "동쪽 x (m)", "range": [xmin, xmax], "showbackground": False, "gridcolor": "#e8edf1", "zerolinecolor": "#bbc6d0"},
            "yaxis": {"title": "북쪽 y (m)", "range": [ymin, ymax], "showbackground": False, "gridcolor": "#e8edf1", "zerolinecolor": "#bbc6d0"},
            "zaxis": {"title": "높이 z (m)", "range": [0, max(max_height * 1.08, size * 0.08)], "showbackground": False, "gridcolor": "#e8edf1", "zerolinecolor": "#bbc6d0"},
            "aspectmode": "data", "dragmode": "orbit",
            "camera": {"eye": {"x": 1.2, "y": -1.55, "z": 1.25}, "projection": {"type": "orthographic"}},
        },
        legend={"orientation": "h", "x": 0.02, "y": 1.04, "bgcolor": "rgba(255,255,255,0.9)", "font": {"size": 12}},
        updatemenus=[{
            "type": "buttons", "direction": "left", "x": 1, "xanchor": "right", "y": 1.06, "yanchor": "bottom",
            "showactive": True, "bgcolor": "#f5f7fa", "bordercolor": "#dbe2e9",
            "buttons": [
                {"label": "입체 보기", "method": "relayout", "args": [{"scene.zaxis.visible": True, "scene.camera": {"eye": {"x": 1.2, "y": -1.55, "z": 1.25}, "up": {"x": 0, "y": 0, "z": 1}, "projection": {"type": "orthographic"}}}]},
                {"label": "위에서 보기", "method": "relayout", "args": [{"scene.zaxis.visible": False, "scene.camera": {"eye": {"x": 0, "y": 0, "z": 2.4}, "up": {"x": 0, "y": 1, "z": 0}, "projection": {"type": "orthographic"}}}]},
            ],
        }],
    )
    plot_html = pio.to_html(
        figure, include_plotlyjs=True, full_html=False, div_id="region-map",
        config={"responsive": True, "displaylogo": False, "scrollZoom": True, "toImageButtonOptions": {"filename": "region-model", "scale": 2}},
    )
    center = metadata.get("center_lonlat", [None, None])
    center_text = f"{float(center[1]):.6f}° N, {float(center[0]):.6f}° E" if len(center) >= 2 and center[0] is not None and center[1] is not None else "중심 미지정"
    total = len(buildings)
    known = counts["gis"] + counts["ledger"]
    missing = counts["missing"]
    coverage = f"{known / total * 100:.1f}%" if total else "—"
    source_info = metadata.get("building_source", {})
    source_name = str(source_info.get("source_name", "입력 GIS 자료"))
    source_date = str(source_info.get("snapshot_date", "기준일 미기재"))
    record_dates = source_info.get("record_reference_dates", [])
    record_date_note = " · 건물 속성 기준일 " + escape(", ".join(map(str, record_dates))) if record_dates else ""
    source_url = str(source_info.get("source_url", ""))
    source_link = (
        f'<a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(source_name)}</a>'
        if source_url.startswith(("https://", "http://")) else escape(source_name)
    )
    geometry_description = "실제 외곽선 + 출처가 기록된 높이" if known else "실제 건물 외곽선 · 높이 미확인"
    demo_banner = (
        '<div class="demo"><strong>SYNTHETIC · 합성 데이터 예시</strong>'
        '<span>이 화면의 건물 위치·형태·높이는 작동 확인용입니다. 해당 지역의 실제 건물이나 통신 결과를 나타내지 않습니다.</span></div>'
        if is_demo else f'<div class="real"><strong>{geometry_description}</strong><span>{source_link}<br>배포본 기준일 {escape(source_date)}{record_date_note}<br>관심 영역 경계에서 외곽선 절단 · 높이 임의 추정 없음</span></div>'
    )
    attribution = (
        '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a> · ODbL'
        if str(metadata.get("background_source", "")).lower() == "osm" else ("합성 배경 데이터" if is_demo else "배경 데이터 없음 또는 사용자 제공")
    )
    if not is_demo:
        attribution = f"건물: {source_link} · 배경: {attribution}"
    missing_notice = (
        f'<strong>높이를 확인하지 못한 건물 {missing:,}개가 있습니다.</strong> 주황색 외곽선으로 남겼으며 임의 높이는 부여하지 않았습니다. '
        '이 건물의 차폐 효과가 해결되기 전까지 OBJ만으로 완전한 전파 장애물 장면을 구성할 수 없습니다.'
        if missing else '모든 입력 건물에 유효한 높이가 있습니다. 출처와 식별자 연결의 적정성은 품질 기록을 함께 확인하세요.'
    )
    rows = "".join(f"<tr><td>{escape(identifier)}</td><td>{escape(flags)}</td></tr>" for identifier, flags in missing_rows[:100])
    missing_table = (
        f'<details><summary>높이 미확인 건물 보기 ({missing:,}개)</summary><div class="table-wrap"><table><thead><tr><th>건물 식별자</th><th>품질 기록</th></tr></thead><tbody>{rows}</tbody></table></div>'
        + (f'<p>처음 100개만 표시합니다. 전체 {missing:,}개 기록은 CSV를 확인하세요.</p>' if missing > 100 else "")
        + '</details>' if missing else ""
    )
    flags_html = " · ".join(f"{escape(flag)} {count:,}" for flag, count in quality_flags.most_common(8)) or "기록된 품질 플래그 없음"
    tunnel_count = sum(is_tunnel_background(row) for _, row in background.iterrows()) if background is not None else 0
    background_note = f" 터널 태그가 있는 배경 {tunnel_count}개는 지표면에 그리지 않으며 저장 데이터에는 보존합니다." if tunnel_count else ""
    html = f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(region)} · 지역 블록 모델</title><style>
:root{{--ink:#213247;--muted:#66768a;--line:#dfe6ed;--accent:#235f79}}
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6f9;color:var(--ink);font-family:"Malgun Gothic","Segoe UI",sans-serif;font-size:14px;line-height:1.65}}
main{{max-width:1480px;margin:auto;padding:34px 38px 30px}}header{{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;margin-bottom:22px}}
.eyebrow{{color:var(--accent);font-size:11px;font-weight:700;letter-spacing:2px;margin-bottom:7px}}h1{{font-size:29px;line-height:1.3;margin:0;font-weight:700;letter-spacing:-1px}}
.subtitle{{color:var(--muted);margin:9px 0 0}}.scope{{text-align:right;white-space:nowrap;color:var(--muted);font-size:12px}}.scope strong{{display:block;color:var(--ink);font-size:18px}}
.demo,.real{{border:1px solid #e9d2a5;background:#fff8ea;padding:13px 17px;border-radius:9px;margin-bottom:18px;display:flex;gap:20px;align-items:center}}.demo strong{{color:#905713;white-space:nowrap}}.demo span,.real span{{font-size:13px}}.real{{border-color:#cedfe6;background:#eef5f7}}.real strong{{white-space:nowrap;color:#286175}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}}.stat{{padding:16px 20px;background:white;border:1px solid var(--line);border-radius:10px}}.stat label{{display:block;color:var(--muted);font-size:12px}}.stat b{{display:block;font-size:27px;font-weight:650;line-height:1.4;margin:3px 0}}.stat small{{font-size:11px;color:var(--muted)}}.stat.warn b{{color:#bc721c}}
.map-card{{background:white;border:1px solid var(--line);border-radius:12px;overflow:hidden}}.map-head{{padding:15px 20px;border-bottom:1px solid #eef1f5;display:flex;justify-content:space-between;gap:12px}}.map-head b{{font-size:14px}}.map-head span{{font-size:12px;color:var(--muted)}}.plot{{padding:22px 7px 0}}.plot .modebar{{top:62px!important}}.map-foot{{display:flex;justify-content:space-between;gap:15px;padding:12px 20px;border-top:1px solid #eef1f5;color:var(--muted);font-size:11px}}a{{color:#315f82}}
.notes{{display:grid;grid-template-columns:1.2fr 1fr;gap:18px;margin-top:18px}}.note{{background:white;border:1px solid var(--line);border-radius:10px;padding:18px 20px}}h2{{font-size:14px;margin:0 0 9px}}.note p{{margin:0;color:#586b80;font-size:12px}}.note.warning{{border-left:4px solid #d89432}}.note.warning strong{{color:#976014}}.quality{{color:#778496;font-size:11px;margin-top:12px;overflow-wrap:anywhere}}
details{{margin-top:14px;font-size:12px}}summary{{cursor:pointer;color:#315f82}}.table-wrap{{overflow:auto;max-height:240px;margin-top:9px}}table{{border-collapse:collapse;width:100%;text-align:left}}th,td{{padding:7px 10px;border-bottom:1px solid #e5eaf0;overflow-wrap:anywhere}}th{{background:#f3f6f9}}footer{{color:#788799;font-size:11px;margin-top:18px}}
@media(max-width:850px){{main{{padding:20px 14px}}header,.demo,.real{{display:block}}.scope{{text-align:left;margin-top:14px}}.stats{{grid-template-columns:repeat(2,1fr)}}.notes{{grid-template-columns:1fr}}.map-head,.map-foot{{display:block}}.map-head span,.map-foot span{{display:block}}.demo span,.real span{{display:block;margin-top:5px}}h1{{font-size:25px}}}}
</style></head><body><main>
<header><div><div class="eyebrow">REGION MODEL / {'SYNTHETIC TEST' if is_demo else 'GIS DATA PREVIEW'}</div><h1>{escape(region)} · 지역 블록 모델</h1><p class="subtitle">건물 외곽선 × 출처가 기록된 높이 · 평면 지면 · 통신 지역 모델링 준비</p></div><div class="scope"><strong>{size:,.0f} m × {size:,.0f} m</strong>{escape(center_text)}<br>중심 원점 (0, 0) · 동쪽 +x / 북쪽 +y</div></header>
{demo_banner}
<section class="stats" aria-label="건물 품질 요약"><div class="stat"><label>전체 건물</label><b>{total:,}</b><small>영역과 겹치는 입력 건물</small></div><div class="stat"><label>부피 생성 건물</label><b>{known:,}</b><small>GIS {counts['gis']:,} · 대장 연결 {counts['ledger']:,}</small></div><div class="stat warn"><label>높이 미확인</label><b>{missing:,}</b><small>주황색 외곽선 · 임의 높이 없음</small></div><div class="stat"><label>높이 확보율</label><b>{coverage}</b><small>건물 개수 기준</small></div></section>
<section class="map-card"><div class="map-head"><b>{'입체 블록 미리보기' if known else '높이 미확인 외곽선 미리보기'}</b><span>회전: 드래그 · 확대: 휠 · 건물: 마우스를 올려 속성 확인</span></div><div class="plot">{plot_html}</div><div class="map-foot"><span>좌표·높이 단위 m · 세 축 동일 축척 · 지면 z = 0 · 도로는 중심선 표시</span><span>{attribution}</span></div></section>
<section class="notes"><article class="note warning"><h2>높이 누락을 확인하세요</h2><p>{missing_notice}</p>{missing_table}</article><article class="note"><h2>이 화면의 범위</h2><p>외곽선을 높이만큼 수직으로 세운 형상 미리보기입니다. 통신 계산·안테나·재료·반사·회절은 포함하지 않습니다. 오목한 외곽선과 내부 빈 공간은 유지합니다. 배경 레이어와 미확인 외곽선의 작은 화면상 오프셋은 표시용이며 지형 고도가 아닙니다.{background_note}</p><div class="quality">품질 플래그: {flags_html}</div></article></section>
<footer>독립 실행 HTML · 파일을 다시 열어도 인터넷 연결 없이 지도를 탐색할 수 있습니다. {'합성 예시 데이터를 실제 관측 결과로 사용하지 마세요.' if is_demo else '최종 통신 장면 구성 전 높이 누락·경계 처리·데이터 기준 시점을 확인하세요.'}</footer>
</main></body></html>'''
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def export_obj(buildings, output_path: Path) -> None:
    """Export known-height building volumes only, in local metres at flat z=0."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    missing = sum(_known_height(row) is None for _, row in buildings.iterrows())
    with output_path.open("w", encoding="utf-8", newline="\n") as stream:
        if buildings.attrs.get("is_demo", False):
            stream.write("# SYNTHETIC DEMO: invented footprints and heights; NOT real GIS buildings.\n")
        stream.write("# Building-footprint extrusions; local coordinates in metres.\n")
        stream.write("# Axes: x east, y north, z up. Flat ground z=0; no vertical exaggeration.\n")
        stream.write(f"# WARNING: {missing} buildings have unresolved height and are NOT included.\n")
        stream.write("# Consult quality records before propagation use; omitted buildings are not free space.\n")
        offset = 1
        for ordinal, (_, row) in enumerate(buildings.iterrows(), start=1):
            height = _known_height(row)
            if height is None:
                continue
            identifier = _clean_text(row.get("building_id", ordinal))
            identifier = re.sub(r"[^\w.-]+", "_", identifier)
            source = str(row.get("height_source"))
            for part_number, polygon in enumerate(_polygon_parts(row.geometry), start=1):
                vertices, faces = polygon_mesh(polygon, height)
                stream.write(f"o building_{ordinal}_{identifier}_part_{part_number}\n")
                stream.write(f"# height_m={height:.9g} height_source={source}\n")
                for x, y, z in vertices:
                    stream.write(f"v {x:.9f} {y:.9f} {z:.9f}\n")
                for i, j, k in faces:
                    stream.write(f"f {i + offset} {j + offset} {k + offset}\n")
                offset += len(vertices)
