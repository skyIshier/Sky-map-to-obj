#!/usr/bin/env python3
"""
地图可视化导出工具 v14 - 修复 v57+ 地形解析 + 模糊匹配 mesh 文件
"""

import sys
import os
import json
import struct
import math
import io
import importlib.util
import subprocess
import shutil
from collections import OrderedDict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# ============================================================
# 导入
# ============================================================
try:
    import lz4.block
    HAS_LZ4 = True
    print("[OK] lz4")
except ImportError:
    HAS_LZ4 = False

parse_and_split = None
HAS_MESHES = False
for _name in ["Sky-Bstbake.py", "Sky_Bstbake.py", "BstBaked.py"]:
    _p = os.path.join(SCRIPT_DIR, _name)
    if os.path.exists(_p):
        _old_out, _old_err = sys.stdout, sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            _spec = importlib.util.spec_from_file_location("_stb", _p)
            _stb = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_stb)
            parse_and_split = getattr(_stb, 'parse_and_split', None)
            if parse_and_split:
                HAS_MESHES = True
                print(f"[OK] {os.path.basename(_p)}")
        except:
            pass
        finally:
            sys.stdout, sys.stderr = _old_out, _old_err
        break

# 导入 mesh 解析函数
HAS_MESH = False
_mesh_handlers = {}
_process_single_file = None

try:
    from meshtoobj import (
        process_header_17, process_header_1A, process_header_1C,
        process_header_1E, process_header_1F, process_header_20,
        HEADER_VERSION_MAP, process_single_file
    )
    _process_single_file = process_single_file
    _mesh_handlers = {
        b'\x17\x00\x00\x00': process_header_17,
        b'\x1a\x00\x00\x00': process_header_1A,
        b'\x1c\x00\x00\x00': process_header_1C,
        b'\x1e\x00\x00\x00': process_header_1E,
        b'\x1f\x00\x00\x00': process_header_1F,
        b'\x20\x00\x00\x00': process_header_20,
    }
    HAS_MESH = True
    print("[OK] meshtoobj.py")
except ImportError as e:
    print(f"[WARN] meshtoobj.py 导入失败: {e}")

# ============================================================
# 颜色
# ============================================================
CLASS_COLORS = {
    'LevelMesh':(0.7,0.7,0.7), 'Marker':(1.0,0.8,0.2), 'Npc':(0.2,0.8,0.2),
    'MeditationArea':(0.3,0.5,1.0), 'Portal':(1.0,0.3,0.3), 'Checkpoint':(1.0,0.5,0.0),
    'Boundary':(1.0,0.0,0.0), 'Wind':(0.5,0.8,1.0), 'Water':(0.2,0.5,1.0),
    'Timeline':(0.8,0.3,0.8), 'Enable':(0.5,0.5,0.5), 'SpawnMotes':(1.0,1.0,0.5),
    'SoundEmitter':(0.2,0.8,0.8), 'PointLight':(1.0,0.9,0.4), 'Flame':(1.0,0.4,0.1),
}
DEFAULT_COLOR = (0.5,0.5,0.5)

def get_class_color(cls_name):
    for key, color in CLASS_COLORS.items():
        if key in cls_name: return color
    return DEFAULT_COLOR

# ============================================================
# 小球
# ============================================================
def make_sphere_verts(cx,cy,cz,radius=0.5,segments=8):
    verts=[]; faces=[]
    verts.append((cx,cy+radius,cz)); verts.append((cx,cy-radius,cz))
    rings=segments//2
    for i in range(1,rings):
        phi=math.pi*i/rings; y=cy+radius*math.cos(phi); r=radius*math.sin(phi)
        for j in range(segments):
            theta=2*math.pi*j/segments; x=cx+r*math.cos(theta); z=cz+r*math.sin(theta)
            verts.append((x,y,z))
    for j in range(segments): faces.append((0,2+j,2+(j+1)%segments))
    for j in range(segments): faces.append((1,2+(segments-1)*(rings-1)+(j+1)%segments,2+(segments-1)*(rings-1)+j))
    for i in range(rings-2):
        for j in range(segments):
            a=2+i*segments+j; b=2+i*segments+(j+1)%segments
            c=2+(i+1)*segments+j; d=2+(i+1)*segments+(j+1)%segments
            faces.append((a,b,d)); faces.append((a,d,c))
    return verts,faces

# ============================================================
# 变换矩阵
# ============================================================
def apply_transform(verts, raw_floats):
    if len(raw_floats)<16: return verts
    m=[float(x) for x in raw_floats[:16]]; result=[]
    for v in verts:
        x,y,z=v[0],v[1],v[2]
        nx=m[0]*x+m[4]*y+m[8]*z+m[12]; ny=m[1]*x+m[5]*y+m[9]*z+m[13]; nz=m[2]*x+m[6]*y+m[10]*z+m[14]
        result.append((nx,ny,nz))
    return result

# ============================================================
# 模糊匹配 mesh 文件
# ============================================================
def find_mesh_file(mesh_folder, resource_name):
    """查找 mesh 文件，支持模糊匹配"""
    if not os.path.isdir(mesh_folder):
        return None
    
    # 1. 精确匹配 .mesh
    exact_path = os.path.join(mesh_folder, f"{resource_name}.mesh")
    if os.path.exists(exact_path):
        return exact_path
    
    # 2. 搜索包含资源名的文件
    try:
        for f in os.listdir(mesh_folder):
            if f.endswith('.mesh') and resource_name in f:
                return os.path.join(mesh_folder, f)
    except:
        pass
    
    return None

# ============================================================
# .meshes 地形解析（修复 v57+）
# ============================================================
def parse_meshes_to_obj_data(meshes_file):
    if not HAS_MESHES or not HAS_LZ4: return [],[]
    with open(meshes_file,'rb') as f: data=f.read()
    if data[0:4]!=b'LVL0': return [],[]
    file_version=struct.unpack_from('<I',data,0x04)[0]
    lod0_offset=lod0_length=0; geo0_offset=geo0_length=0; metr_offset=metr_length=0
    for i in range(data[0x08]):
        base=0x08+4+i*12
        name=data[base:base+4].rstrip(b'\x00').decode('ascii',errors='ignore')
        seg_offset=struct.unpack_from('<I',data,base+4)[0]; seg_length=struct.unpack_from('<I',data,base+8)[0]
        if name=='LOD0': lod0_offset,lod0_length=seg_offset,seg_length
        elif name=='GEO0': geo0_offset,geo0_length=seg_offset,seg_length
        elif name=='METR': metr_offset,metr_length=seg_offset,seg_length
    if lod0_length==0: return [],[]
    compressed=data[lod0_offset:lod0_offset+lod0_length]
    decompressed=lz4.block.decompress(compressed,uncompressed_size=0xC00000)
    geo_data=data[geo0_offset:geo0_offset+geo0_length] if (file_version>=57 and geo0_length>0) else None
    metr_data=data[metr_offset:metr_offset+metr_length] if (file_version>=55 and metr_length>0) else None
    try: result,segments=parse_and_split(decompressed,file_version,metr_data,geo_data)
    except Exception as e:
        print(f"    解析失败: {e}")
        return [],[]
    
    all_verts=[]; all_faces=[]; v_offset=0
    
    for section in ['terrain','skirts','occluder']:
        for chunk in result.get(section,[]):
            # 检查是否是 v57+ 格式（有 ib_raw 和 patches）
            if chunk.get('ib_raw') and chunk.get('patches'):
                # v57+ 地形解析
                verts = chunk.get('verts', [])
                ib_raw = chunk.get('ib_raw', b'')
                patches = chunk.get('patches', [])
                terrain_patches = [p for p in patches if p['array'] == 'A']
                
                if not verts or not ib_raw or not terrain_patches:
                    continue
                
                base_v = len(all_verts)
                
                # 获取所有顶点范围
                all_verts_needed = []
                vert_indices = {}  # 原始索引 -> 新索引
                new_idx = 0
                
                for patch in terrain_patches:
                    vs = patch['vert_start']
                    ve = patch['vert_end']
                    for vi in range(vs, ve):
                        if vi not in vert_indices:
                            vert_indices[vi] = new_idx
                            pos = verts[vi].get('pos', (0, 0, 0))
                            all_verts_needed.append((pos[0], pos[1], -pos[2]))
                            new_idx += 1
                
                for patch in terrain_patches:
                    ib_start = patch['ib_byte_off']
                    ib_end = ib_start + patch['ib_byte_len']
                    patch_bytes = ib_raw[ib_start:ib_end]
                    tri_count = len(patch_bytes) // 3
                    if tri_count == 0:
                        continue
                    vs = patch['vert_start']
                    for ti in range(tri_count):
                        bo = ti * 3
                        i0 = vert_indices.get(patch_bytes[bo] + vs, -1)
                        i1 = vert_indices.get(patch_bytes[bo + 1] + vs, -1)
                        i2 = vert_indices.get(patch_bytes[bo + 2] + vs, -1)
                        if i0 >= 0 and i1 >= 0 and i2 >= 0:
                            all_faces.append((i0 + base_v, i2 + base_v, i1 + base_v))
                
                all_verts.extend(all_verts_needed)
                v_offset += len(all_verts_needed)
                
            elif chunk.get('verts') and chunk.get('indices'):
                # 旧格式
                verts = chunk.get('verts', [])
                indices = chunk.get('indices', [])
                if not verts or not indices: continue
                base_v = len(all_verts)
                for v in verts:
                    pos=v.get('pos',(0,0,0)); all_verts.append((pos[0],pos[1],-pos[2]))
                for i in range(0,len(indices),3):
                    if i+2<len(indices): all_faces.append((indices[i]+base_v,indices[i+2]+base_v,indices[i+1]+base_v))
                v_offset+=len(verts)
    
    return all_verts,all_faces

# ============================================================
# .mesh 模型解析（修复版）
# ============================================================
def parse_mesh_file(mesh_path):
    """解析 .mesh 文件，返回 (verts, faces)"""
    if not HAS_MESH:
        return [], []
    
    try:
        with open(mesh_path, 'rb') as f:
            data = f.read()
    except Exception as e:
        return [], []
    
    if len(data) < 4:
        return [], []
    
    header = data[:4]
    version = HEADER_VERSION_MAP.get(header)
    if version is None:
        return [], []
    
    handler = _mesh_handlers.get(header)
    if handler is None:
        return [], []
    
    try:
        filename = os.path.basename(mesh_path)
        
        if header == b'\x17\x00\x00\x00':
            result = handler(data, mesh_path, filename, version, False, True)
        elif header == b'\x1a\x00\x00\x00':
            result = handler(data, mesh_path, filename, version, True)
        elif header == b'\x1c\x00\x00\x00':
            result = handler(data, mesh_path, filename, version, True)
        elif header == b'\x1e\x00\x00\x00':
            result = handler(data, mesh_path, filename, version, True)
        elif header == b'\x1f\x00\x00\x00':
            result = handler(data, mesh_path, filename, version, True)
        elif header == b'\x20\x00\x00\x00':
            result = handler(data, mesh_path, filename, version, True)
        else:
            return [], []
        
        if result and len(result) >= 3:
            verts_raw = result[0]
            faces_raw = result[2]
            if verts_raw and faces_raw:
                verts = [(v[0], v[1], v[2]) for v in verts_raw]
                faces = [(f[0], f[1], f[2]) for f in faces_raw]
                return verts, faces
    except Exception as e:
        pass
    
    return [], []

# ============================================================
# 调用单个转换.py 生成 JSON
# ============================================================
def convert_bin_to_json(bin_path, output_dir):
    converter = os.path.join(SCRIPT_DIR, '单个转换.py')
    if not os.path.exists(converter):
        print("❌ 未找到 单个转换.py")
        return None
    json_path = os.path.join(output_dir, os.path.basename(bin_path) + '.json')
    if os.path.exists(json_path):
        print(f"   JSON 已存在，跳过转换")
        return json_path
    print(f"   ⏳ 转换 bin → JSON ...")
    result = subprocess.run(
        [sys.executable, converter, bin_path],
        capture_output=True, text=True, timeout=600,
        cwd=output_dir
    )
    if os.path.exists(json_path):
        print(f"   ✅ JSON 生成完成")
        return json_path
    alt_path = bin_path + '.json'
    if os.path.exists(alt_path):
        shutil.move(alt_path, json_path)
        print(f"   ✅ JSON 已移动")
        return json_path
    print(f"   ❌ JSON 生成失败")
    return None

# ============================================================
# 核心：resourceName 提取
# ============================================================
def extract_resource_name_from_cls_data(cls_data):
    """从 cls_data 字典中提取 resourceName，支持带中文括号的 key"""
    if not isinstance(cls_data, dict):
        return None
    
    for key, value in cls_data.items():
        key_lower = key.lower()
        if 'resourcename' in key_lower:
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for sub_k, sub_v in value.items():
                    if isinstance(sub_v, str) and sub_v.strip():
                        return sub_v.strip()
    
    for key, value in cls_data.items():
        if isinstance(value, dict):
            result = extract_resource_name_from_cls_data(value)
            if result:
                return result
    
    return None

def extract_transform_from_cls_data(cls_data):
    """从 cls_data 中提取 transform 矩阵和坐标"""
    for key, value in cls_data.items():
        if 'transform' in key.lower():
            if isinstance(value, dict):
                rf = value.get('_raw_floats', [])
                if len(rf) >= 16:
                    coords = (float(rf[12]), float(rf[13]), float(rf[14]))
                    return coords, rf
            elif isinstance(value, list) and len(value) >= 16:
                coords = (float(value[12]), float(value[13]), float(value[14]))
                return coords, value
    return None, None

def find_all_levelmesh_with_resources(json_data):
    """遍历所有 BSTNodes，找到 LevelMesh 类节点并提取 resourceName 和 transform"""
    results = []
    bst_nodes = json_data.get('BSTNodes', {})
    
    for node_name, node_data in bst_nodes.items():
        if not isinstance(node_data, dict):
            continue
        
        for cls_name, cls_data in node_data.items():
            if not isinstance(cls_data, dict):
                continue
            
            if 'LevelMesh' not in cls_name:
                continue
            
            resource_name = extract_resource_name_from_cls_data(cls_data)
            if not resource_name:
                continue
            
            coords, raw_floats = extract_transform_from_cls_data(cls_data)
            if coords is None:
                continue
            
            results.append({
                'node_name': node_name,
                'class_name': cls_name,
                'resource_name': resource_name,
                'coords': coords,
                'raw_floats': raw_floats
            })
    
    return results

# ============================================================
# 主导出
# ============================================================
def export_map(map_folder, mesh_folder, export_markers=True):
    map_name = os.path.basename(map_folder.rstrip('/\\'))
    
    # 查找 bin 文件
    bin_file = os.path.join(map_folder, 'Objects.level.bin')
    if not os.path.exists(bin_file):
        bfs = [f for f in os.listdir(map_folder) if f.endswith('.bin') and not f.endswith('.meshes')]
        if bfs: 
            bin_file = os.path.join(map_folder, bfs[0])
        else: 
            print("❌ 未找到 .bin 文件")
            return
    
    # 查找 meshes 文件
    meshes_file = None
    for f in os.listdir(map_folder):
        if f.endswith('.meshes'):
            meshes_file = os.path.join(map_folder, f)
            break
    
    work_dir = os.path.join(map_folder, f"{map_name}_export")
    os.makedirs(work_dir, exist_ok=True)
    
    obj_path = os.path.join(work_dir, f"{map_name}.obj")
    mtl_path = os.path.join(work_dir, f"{map_name}.mtl")
    
    # === 1. bin → JSON ===
    print(f"\n📖 [.bin] {os.path.basename(bin_file)}")
    json_path = convert_bin_to_json(bin_file, work_dir)
    if json_path is None: 
        print("❌ JSON 生成失败")
        return
    
    print(f"   📄 读取 JSON...")
    with open(json_path, 'r', encoding='utf-8') as f:
        json_data = json.load(f, object_pairs_hook=OrderedDict)
    
    # === 2. 提取 LevelMesh 节点 ===
    print(f"   🔍 提取 LevelMesh 节点...")
    level_meshes = find_all_levelmesh_with_resources(json_data)
    
    unique_resources = set(lm['resource_name'] for lm in level_meshes)
    print(f"   找到 {len(level_meshes)} 个 LevelMesh 实例, {len(unique_resources)} 种资源")
    
    if level_meshes:
        for lm in list(level_meshes)[:10]:
            print(f"      {lm['resource_name']} @ ({lm['coords'][0]:.1f}, {lm['coords'][1]:.1f}, {lm['coords'][2]:.1f})")
    
    # === 3. 提取其他标记 ===
    markers = []
    bst_nodes = json_data.get('BSTNodes', {})
    
    for node_name, node_data in bst_nodes.items():
        if not isinstance(node_data, dict):
            continue
        for cls_name, cls_data in node_data.items():
            if not isinstance(cls_data, dict):
                continue
            if 'LevelMesh' in cls_name:
                continue
            
            coords, _ = extract_transform_from_cls_data(cls_data)
            if coords:
                markers.append({
                    'name': node_name,
                    'class': cls_name,
                    'x': coords[0], 'y': coords[1], 'z': coords[2],
                    'color': get_class_color(cls_name)
                })
    
    print(f"   其他标记: {len(markers)}")
    
    # === 4. 地形 .meshes ===
    terrain_verts, terrain_faces = [], []
    if meshes_file and os.path.exists(meshes_file):
        print(f"\n📖 [.meshes] {os.path.basename(meshes_file)}")
        terrain_verts, terrain_faces = parse_meshes_to_obj_data(meshes_file)
        print(f"   地形: {len(terrain_verts)} 顶点, {len(terrain_faces)} 三角形")
    
    # === 5. 模型 .mesh 加载（支持模糊匹配） ===
    mesh_models = []
    loaded_resources = set()
    
    if HAS_MESH and os.path.isdir(mesh_folder) and level_meshes:
        print(f"\n📖 [.mesh] 加载模型 (目录: {mesh_folder})")
        mesh_files_list = [f for f in os.listdir(mesh_folder) if f.endswith('.mesh')]
        print(f"   目录中 .mesh 文件: {len(mesh_files_list)} 个")
        
        success_count = 0
        fail_count = 0
        missing_count = 0
        
        for lm in level_meshes:
            res = lm['resource_name']
            if res in loaded_resources:
                continue
            
            # 使用模糊匹配查找 mesh 文件
            mesh_file = find_mesh_file(mesh_folder, res)
            
            if mesh_file and os.path.exists(mesh_file):
                verts, faces = parse_mesh_file(mesh_file)
                if verts and faces:
                    mesh_models.append({
                        'resource': res,
                        'verts': verts,
                        'faces': faces,
                        'instances': []
                    })
                    loaded_resources.add(res)
                    success_count += 1
                    if success_count <= 20:
                        print(f"   ✅ {res} ({len(verts)}v, {len(faces)}t)")
                else:
                    fail_count += 1
                    if fail_count <= 10:
                        print(f"   ❌ {res} 解析失败")
            else:
                missing_count += 1
                if missing_count <= 10:
                    print(f"   ⚠️ {res}.mesh 不存在")
        
        model_map = {m['resource']: m for m in mesh_models}
        for lm in level_meshes:
            if lm['resource_name'] in model_map:
                model_map[lm['resource_name']]['instances'].append(lm)
        
        total_instances = sum(len(m['instances']) for m in mesh_models)
        print(f"\n   统计: 成功 {success_count} 种模型, {total_instances} 个实例")
        print(f"   解析失败: {fail_count} 种, 文件缺失: {missing_count} 种")
    
    # === 6. 写入 OBJ ===
    print(f"\n📝 导出 OBJ...")
    
    with open(mtl_path, 'w', encoding='utf-8') as mf:
        mf.write("newmtl terrain\nKd 0.45 0.42 0.38\nKa 0.1 0.1 0.1\nKs 0.0 0.0 0.0\nd 1.0\n\n")
        mf.write("newmtl model\nKd 0.75 0.73 0.68\nKa 0.1 0.1 0.1\nKs 0.0 0.0 0.0\nd 1.0\n\n")
        for cls_name, color in CLASS_COLORS.items():
            mf.write(f"newmtl {cls_name}\nKd {color[0]:.4f} {color[1]:.4f} {color[2]:.4f}\nKa 0.1 0.1 0.1\nKs 0.0 0.0 0.0\nd 1.0\n\n")
        mf.write("newmtl default\nKd 0.5 0.5 0.5\nKa 0.1 0.1 0.1\nKs 0.0 0.0 0.0\nd 1.0\n")
    
    global_v = 1
    
    with open(obj_path, 'w', encoding='utf-8') as f:
        f.write(f"# Sky Map: {map_name}\n")
        f.write(f"# Exported by map_exporter v14\n")
        f.write(f"# Export markers: {export_markers}\n")
        f.write(f"mtllib {map_name}.mtl\n\n")
        
        # 地形
        if terrain_verts:
            f.write(f"o Terrain\nusemtl terrain\n")
            for v in terrain_verts:
                f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
            for tri in terrain_faces:
                f.write(f"f {tri[0]+global_v} {tri[1]+global_v} {tri[2]+global_v}\n")
            global_v += len(terrain_verts)
            f.write("\n")
        
        # 模型实例
        for model in mesh_models:
            for inst in model['instances']:
                raw_floats = inst.get('raw_floats')
                if raw_floats:
                    transformed = apply_transform(model['verts'], raw_floats)
                else:
                    transformed = model['verts']
                
                f.write(f"o {model['resource']}\nusemtl model\n")
                for v in transformed:
                    f.write(f"v {v[0]:.6f} {v[1]:.6f} {-v[2]:.6f}\n")
                for tri in model['faces']:
                    f.write(f"f {tri[0]+global_v} {tri[1]+global_v} {tri[2]+global_v}\n")
                global_v += len(transformed)
            f.write("\n")
        
        # 标记小球
        if export_markers:
            class_groups = {}
            for m in markers:
                cls = m['class']
                if cls not in class_groups:
                    class_groups[cls] = []
                class_groups[cls].append(m)
            
            for cls_name, nodes in class_groups.items():
                color_name = cls_name if cls_name in CLASS_COLORS else 'default'
                f.write(f"o {cls_name}\nusemtl {color_name}\n")
                for node in nodes:
                    verts, faces = make_sphere_verts(node['x'], node['y'], -node['z'], 0.5)
                    for v in verts:
                        f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
                    for tri in faces:
                        f.write(f"f {tri[0]+global_v} {tri[1]+global_v} {tri[2]+global_v}\n")
                    global_v += len(verts)
                f.write("\n")
    
    print(f"\n✅ 完成!")
    print(f"   地形: {len(terrain_verts):,} 顶点, {len(terrain_faces):,} 三角形")
    print(f"   模型: {len(mesh_models)} 种, {sum(len(m['instances']) for m in mesh_models)} 实例")
    print(f"   标记: {len(markers)} {'(已导出)' if export_markers else '(已禁用)'}")
    print(f"   OBJ: {obj_path}")
    return work_dir


def main():
    print("=" * 55)
    print("   ☁️ 地图可视化导出 v14 (模糊匹配 mesh)")
    print("=" * 55)
    print()
    print("标记小球说明：")
    print("  - 小球是代替 NPC、传送门、冥想区等交互点的标记")
    print("  - 导出小球可以帮助定位这些交互点的位置")
    print("  - 如果只需要地形和模型，可以选择不导出小球")
    print()
    
    while True:
        path = input("地图文件夹: ").strip().strip('"').strip("'")
        if path and os.path.isdir(path):
            break
        print("❌ 无效，请重新输入")
    
    mesh_dir = input("mesh 文件夹路径 (默认: 脚本同目录/mesh): ").strip().strip('"').strip("'")
    if not mesh_dir:
        mesh_dir = os.path.join(SCRIPT_DIR, 'mesh')
    
    if not os.path.isdir(mesh_dir):
        print(f"⚠️ mesh 目录不存在: {mesh_dir}")
        confirm = input("是否继续? (y/n): ")
        if confirm.lower() != 'y':
            return
    
    export_markers_input = input("是否导出标记小球? (y/n, 默认 y): ").strip().lower()
    export_markers = export_markers_input != 'n'
    
    export_map(path, mesh_dir, export_markers)
    input("\n按回车退出...")


if __name__ == '__main__':
    main()