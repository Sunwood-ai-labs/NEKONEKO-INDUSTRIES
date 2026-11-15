"""Render measured mesh sections in print-Z coordinates; no model edits."""
from pathlib import Path
import json
import math
import runpy

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

HERE = Path(__file__).resolve().parent
M = runpy.run_path(str(HERE / 'geometry' / 'analyze_mesh_sections.py'))
plt.rcParams['font.family'] = 'Meiryo'


def projected_section(mesh, normal_axis, coordinate):
    origin = np.zeros(3)
    origin[normal_axis] = coordinate
    normal = np.zeros(3)
    normal[normal_axis] = 1
    section = mesh.section(plane_origin=origin, plane_normal=normal)
    axes = [a for a in range(3) if a != normal_axis]
    return [] if section is None else [p[:, axes] for p in section.discrete]


def draw(ax, parts, normal_axis, coordinate):
    for part in parts:
        lines = projected_section(part.mesh, normal_axis, coordinate)
        color = M['PART_COLORS'].get(part.name, '#444444')
        M['fill_material'](ax, lines, color, alpha=0.32)
        for line in lines:
            ax.plot(line[:, 0], line[:, 1], color=color, lw=1.1, zorder=4)
    ax.set_aspect('equal')
    ax.grid(alpha=0.22, lw=0.4)
    ax.set_ylabel('印刷高さ Z [mm]　造形方向 ↑')


def main():
    parts = M['load_instances'](M['INPUT'])
    wheel = next(p for p in parts if p.name == 'Body14')
    center = (wheel.mesh.bounds[0] + wheel.mesh.bounds[1]) / 2
    fig, ax = plt.subplots(figsize=(8, 9), constrained_layout=True)
    draw(ax, parts, 1, float(center[1]))
    ax.set_xlim(center[0] - 9, center[0] + 9)
    ax.set_ylim(-0.6, 32)
    ax.set_xlabel('X [mm]　車長方向')
    ax.set_title(f'車軸を通る実メッシュ断面\nY={center[1]:.3f} mm、XZ平面\n青＝車体、橙＝左右の輪と軸の一体部品')
    ax.axhline(0, color='#222222', lw=2)
    ax.text(center[0]+5.5, 0.4, 'ベッド', ha='center', fontsize=10)
    ax.annotate('上側の車輪\n軸から斜面で広がる', xy=(center[0]+4.0,25),
                xytext=(center[0]+5.2,20.5), ha='center', fontsize=10,
                arrowprops={'arrowstyle':'->','color':'#333333'})
    ax.annotate('縦に育つ軸', xy=(center[0],16), xytext=(center[0]-5.5,18),
                ha='center', fontsize=10, arrowprops={'arrowstyle':'->'})
    fig.savefig(HERE/'wheel-axial-section.png', dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 7), constrained_layout=True)
    for ax, x, label in zip(axes,[90.0,97.0],['座面付近','背もたれ付近']):
        draw(ax,parts,0,x)
        ax.set_xlabel('Y [mm]　車高方向 →')
        ax.set_title(f'{label}　X={x:.1f} mm\nYZ断面、白色はメッシュの空洞')
        ax.set_xlim(73,109)
        ax.set_ylim(-1,32)
        ax.axhline(0,color='#222222',lw=1.5)
        ax.annotate('屋根\n印刷中は縦の壁', xy=(106,16), xytext=(99,20),
                    fontsize=10,ha='center',arrowprops={'arrowstyle':'->'})
    fig.suptitle('横倒しでは、車幅方向へ積み上げる\n外形メッシュの断面。実物断面・25%内部充填の経路図ではありません。',fontsize=13)
    fig.savefig(HERE/'cabin-print-direction-sections.png',dpi=180)
    plt.close(fig)

    from shapely.geometry import LineString
    body_lines=M['section_lines'](parts[0].mesh,float(center[2]))
    wheel_lines=M['section_lines'](wheel.mesh,float(center[2]))
    clearances=[LineString(a).distance(LineString(b)) for a in body_lines for b in wheel_lines]
    vertices=np.asarray(wheel.mesh.vertices)
    radial=np.linalg.norm(vertices[:,:2]-center[:2],axis=1)
    radial_levels=[]
    for level in np.unique(np.round(vertices[:,2],6)):
        values=radial[np.isclose(vertices[:,2],level,atol=1e-6,rtol=0)]
        radial_levels.append({'z_mm':float(level),'radius_min_mm':float(values.min()),'radius_max_mm':float(values.max())})
    cone_pairs=[(a,b) for a,b in zip(radial_levels,radial_levels[1:])
                if a['z_mm']>center[2]+1 and b['radius_max_mm']>a['radius_max_mm']+1
                and b['z_mm']-a['z_mm']>1]
    if len(cone_pairs)!=1:
        raise ValueError(f'Expected one upper expanding cone, found {cone_pairs}')
    lower,upper=cone_pairs[0]
    dz=upper['z_mm']-lower['z_mm']
    dr=upper['radius_max_mm']-lower['radius_max_mm']
    metrics={'source_sha256':M['sha256'](M['INPUT']),
             'wheel_axis_xy_mm':center[:2].tolist(),
             'center_plane_z_mm':float(center[2]),
             'wheel_vertex_radius_levels':radial_levels,
             'upper_cone_start':lower,'upper_cone_end':upper,
             'center_shaft_body_min_contour_gap_mm':min(clearances),
             'cone_height_mm':dz,'cone_radius_growth_mm':dr,
             'cone_dr_dz':dr/dz,'cone_angle_from_vertical_deg':math.degrees(math.atan(dr/dz)),
             'radial_growth_per_0_16mm_layer_mm':0.16*dr/dz,
             'note':'Gap is mesh contour distance in one center section, not physical clearance. Cone values are vertex measurements; actual bead overlap was not measured.'}

    from shapely.geometry import Polygon, GeometryCollection
    paths=json.loads((HERE/'toolpaths/gcode-measurements.json').read_text(encoding='utf8'))
    target=next(r for r in paths['feature_by_layer'] if r['layer']==178 and r['feature']=='Bridge')
    previous_z=max(r['z_height'] for r in paths['feature_by_layer'] if r['layer']==177 and r['z_height'] is not None)
    material=GeometryCollection()
    for part in parts:
        part_material=GeometryCollection()
        for loop in M['section_lines'](part.mesh,previous_z):
            if len(loop)>3 and np.linalg.norm(loop[0]-loop[-1])<1e-5:
                part_material=part_material.symmetric_difference(Polygon(loop))
        material=material.union(part_material)
    path=target['max_g1_line']
    bridge=LineString([path['start'],path['end']])
    empty=bridge.difference(material)
    empty_parts=list(empty.geoms) if hasattr(empty,'geoms') else [empty]
    supported=bridge.intersection(material)
    support_parts=list(supported.geoms) if hasattr(supported,'geoms') else [supported]
    metrics['layer178_longest_straight_bridge']={
        'gcode_line':path['line'],'layer':178,'z_mm':target['z_height'],
        'previous_z_mm':previous_z,'start':path['start'],'end':path['end'],
        'full_line_length_mm':bridge.length,
        'mesh_void_intervals':[{'length_mm':g.length,'endpoints':list(g.coords)} for g in empty_parts],
        'mesh_material_intervals':[{'length_mm':g.length,'endpoints':list(g.coords)} for g in support_parts],
        'scope':'Projection against previous-height source mesh section. Does not resolve actual sparse-infill bead contact, sag or global maximum unsupported span.'}
    parser=runpy.run_path(str(HERE/'toolpaths/analyze_gcode.py'))
    _,moves,*_=parser['parse_gcode'](HERE/'input/studio-cache-192layers.gcode')
    crossings=[]
    for move in moves:
        if move.layer!=177 or move.e_delta<=0 or move.xy_length_mm<=0 or move.feature=='Custom':
            continue
        intersection=LineString(move.points).intersection(bridge)
        if intersection.geom_type=='Point' and 95<intersection.x<100:
            crossings.append({'gcode_line':move.line,'feature':move.feature,'xy':[intersection.x,intersection.y]})
    metrics['layer178_longest_straight_bridge']['previous_layer_backrest_path_crossings']=crossings
    fig,(ax,strip)=plt.subplots(2,1,figsize=(12,8),gridspec_kw={'height_ratios':[4,1]},constrained_layout=True)
    for part in parts:
        lines=M['section_lines'](part.mesh,previous_z)
        M['fill_material'](ax,lines,M['PART_COLORS'].get(part.name,'#444444'),0.24)
        for line in lines:
            ax.plot(line[:,0],line[:,1],color='#38617d',lw=0.7,zorder=4)
    ax.plot(*np.asarray(bridge.coords).T,color='#bd331a',lw=3,zorder=6)
    ax.set_aspect('equal');ax.set_xlim(59,121);ax.set_ylim(71,109)
    ax.set_xlabel('X [mm] 車長');ax.set_ylabel('Y [mm] 車高')
    ax.set_title('前層高さのメッシュ断面に、次層のBridge直線を重ねる\n青＝形状上の材料、白＝空洞、赤＝第178層の最長Bridge直線')
    for g in support_parts:
        xs=np.asarray(g.coords)[:,0];strip.plot(xs,[0]*len(xs),color='#38617d',lw=12,solid_capstyle='butt')
    for g in empty_parts:
        xs=np.asarray(g.coords)[:,0];strip.plot(xs,[0]*len(xs),color='#ed7534',lw=6,solid_capstyle='butt')
        strip.text(float(xs.mean()),0.3,f'空洞 {g.length:.2f} mm',ha='center',fontsize=11)
    strip.set_xlim(83.5,108.3);strip.set_ylim(-0.5,0.8);strip.set_yticks([])
    strip.set_xlabel('X [mm]　青い区間は形状上の足場。中央は背もたれ付近。')
    strip.set_title(f'赤線全長 {bridge.length:.2f} mmの内訳（印刷Z {target["z_height"]:.2f} mm、前層 {previous_z:.2f} mm）',fontsize=11)
    fig.suptitle('実G-codeと形状断面の照合\n材料域内部の25%充填や、実際の樹脂の接触幅・垂れ量は表していません。',fontsize=13)
    fig.savefig(HERE/'bridge-anchor-section.png',dpi=180)
    plt.close(fig)
    (HERE/'explanation-measurements.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(metrics,ensure_ascii=True))


if __name__=='__main__':
    main()
