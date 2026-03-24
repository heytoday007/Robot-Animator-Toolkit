# SPDX-License-Identifier: GPL-3.0-or-later
# Robot Animator Toolkit - Blender Add-on
# 从 URDF 导入的机器人骨架合并、重绑定与动态 CSV 导出

bl_info = {
    "name": "Robot Animator Toolkit",
    "author": "Robot Animator",
    "version": (1, 0, 0),
    "blender": (3, 3, 0),
    "location": "View3D > Sidebar (N) > RobotTools",
    "description": "合并 URDF 骨架、重绑定网格、导出动态 CSV",
    "warning": "",
    "doc_url": "",
    "category": "Animation",
}

import bpy
import csv
import json
import os
from bpy.props import (
    StringProperty,
    IntProperty,
    BoolProperty,
    EnumProperty,
    CollectionProperty,
    FloatProperty,
    PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList


# ============== Phobos 兼容：Scene 属性（4.x 下仅用导入逻辑时缺失会报错） ==============
class _PhobosWireFrameSettingsCompat(PropertyGroup):
    """最小兼容：供 Phobos createRobot 访问 scene.phoboswireframesettings.links"""
    links: BoolProperty(name="links", default=False)


# ============== 属性组：导出的关节项 ==============
class RobotJointExportItem(PropertyGroup):
    """单个要导出的关节配置"""
    bone_name: StringProperty(name="Bone", default="")
    axis: EnumProperty(
        name="Axis",
        items=[
            ("X", "X", "X 轴"),
            ("Y", "Y", "Y 轴"),
            ("Z", "Z", "Z 轴"),
        ],
        default="Y",
    )
    enabled: BoolProperty(name="Export", default=True)


# ============== 关节列表 UI ==============
class ROBOT_UL_JointList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.prop(item, "enabled", text="")
            layout.prop(item, "bone_name", text="", emboss=False)
            layout.prop(item, "axis", text=_tr(context, "Axis", "轴向"))
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.prop(item, "bone_name", text="")


# ============== 智能 Mesh 路径解析（供 URDF 导入自动找文件） ==============
def _robot_toolkit_resolve_mesh_path(filename, start_file_path):
    """
    在 URDF 所在目录及常见结构中自动查找 mesh 文件，兼容 package:// 与相对路径。
    返回第一个存在的绝对路径；若都不存在则返回原逻辑得到的路径（便于报错信息）。
    """
    if start_file_path is None or (filename and os.path.isabs(filename)):
        return filename
    start_file_path = os.path.abspath(start_file_path)
    # URDF 所在目录：若 start_file_path 是文件则取目录
    try:
        from .phobos.defs import IMPORT_TYPES  # noqa: F401
    except Exception:
        IMPORT_TYPES = ("urdf", "smurf", "sdf", "xacro", "xml")
    if start_file_path.split(".")[-1].lower() in [x.lower() for x in IMPORT_TYPES]:
        urdf_dir = os.path.dirname(start_file_path)
    else:
        urdf_dir = start_file_path
    parent_dir = os.path.dirname(urdf_dir)
    candidates = []
    if filename.startswith("package://"):
        # package://package_name/path/to/mesh.dae -> 尝试多种位置
        rest = filename[len("package://"):].replace("/", os.sep)
        after_first = rest.split(os.sep, 1)[-1]  # 去掉第一段（包名），如 meshes/pelvis.dae
        pkg_name = rest.split(os.sep, 1)[0] if os.sep in rest else ""
        candidates = [
            os.path.join(parent_dir, after_first),
            os.path.join(urdf_dir, after_first),
            os.path.join(urdf_dir, rest),
            os.path.join(parent_dir, rest),
            os.path.join(parent_dir, pkg_name, after_first),
            os.path.join(urdf_dir, pkg_name, after_first),
        ]
    else:
        # 相对路径
        candidates = [
            os.path.join(urdf_dir, filename.replace("/", os.sep)),
            os.path.normpath(os.path.join(urdf_dir, filename)),
        ]
    basename = os.path.basename(filename.split("package://")[-1].replace("/", os.sep))
    try:
        for root, _dirs, files in os.walk(urdf_dir):
            if basename in files:
                candidates.append(os.path.join(root, basename))
        if parent_dir != urdf_dir and os.path.isdir(parent_dir):
            for root, _dirs, files in os.walk(parent_dir):
                if basename in files:
                    candidates.append(os.path.join(root, basename))
    except OSError:
        pass
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            return path
    # 保持与 Phobos 原逻辑一致：返回“标准”解析结果（可能不存在，用于报错）
    if filename.startswith("package://"):
        out = os.path.join(parent_dir, filename[len("package://"):].replace("/", os.sep).split(os.sep, 1)[-1])
    else:
        out = os.path.join(urdf_dir, filename.replace("/", os.sep))
    return os.path.normpath(out)


# ============== 0. 使用 Phobos 导入 URDF/SMURF ==============
class ROBOT_OT_ImportURDF(Operator):
    """基于内置 Phobos 库导入 URDF/SMURF 机器人"""

    bl_idname = "robot.import_urdf"
    bl_label = "Import URDF/SMURF (Phobos)"
    bl_description = "使用内置的 Phobos 解析器从 URDF/SMURF 文件创建机器人模型"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(
        default="*.urdf;*.smurf;*.xacro;*.xml",
        options={"HIDDEN"},
    )

    @classmethod
    def poll(cls, context):
        return True

    def invoke(self, context, event):
        self.filepath = ""
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            from .phobos import core, defs as ph_defs  # type: ignore
            from .phobos.blender.io.phobos2blender import createRobot  # type: ignore
            from .phobos.blender.utils import blender as bUtils  # type: ignore
            from .phobos.utils import xml as phobos_xml  # type: ignore
        except Exception as e:
            self.report(
                {"ERROR"},
                "Phobos 子模块不可用，请确认 phobos 文件夹完整: %s" % str(e),
            )
            return {"CANCELLED"}

        if not self.filepath:
            self.report({"WARNING"}, "未选择文件")
            return {"CANCELLED"}

        suffix = self.filepath.split(".")[-1].lower()
        import_types = getattr(ph_defs, "IMPORT_TYPES", ["urdf", "smurf", "xacro", "xml"])
        if suffix not in [s.lower() for s in import_types]:
            self.report({"WARNING"}, f"不支持的导入类型: .{suffix}")
            return {"CANCELLED"}

        # Blender 4.x：仅调用 Phobos 导入逻辑时未注册 Phobos 插件，Object/Scene 上缺少属性会报错，此处补注册
        try:
            from .phobos.blender import defs as phobos_blender_defs  # type: ignore
            if not hasattr(bpy.types.Object, "phobostype"):
                bpy.types.Object.phobostype = bpy.props.EnumProperty(
                    items=phobos_blender_defs.phobostypes,
                    name="type",
                    description="Phobos object type",
                )
        except Exception:
            pass
        if not hasattr(bpy.types.Scene, "phoboswireframesettings"):
            try:
                bpy.types.Scene.phoboswireframesettings = bpy.props.PointerProperty(
                    type=_PhobosWireFrameSettingsCompat,
                    name="Phobos Wire Frame Settings",
                )
            except Exception:
                pass

        # 注入智能路径解析：Phobos 的 representation 模块在 import 时复制了 read_relative_filename，
        # 必须同时 patch utils.xml 和 io.representation，否则 Mesh 创建时仍用旧函数导致 package:// 未被解析
        from .phobos.io import representation as phobos_repr  # type: ignore
        _original_read_relative = getattr(phobos_xml, "read_relative_filename", None)
        # Blender 4.2：bpy.ops.import_mesh.stl 可能不可用（内置插件未启用），为 STL 加载提供 trimesh 备用
        _original_load_mesh = getattr(phobos_repr.Mesh, "load_mesh", None)

        def _stl_load_fallback(self, filepath, unique_name):
            """当 bpy.ops.import_mesh.stl 不可用时，用 trimesh 加载 STL 并创建 Blender 网格"""
            try:
                import trimesh
                tm = trimesh.load_mesh(filepath, maintain_order=True)
                if not hasattr(tm, "vertices") or not hasattr(tm, "faces"):
                    return None
                verts = tm.vertices.tolist()
                faces = tm.faces.tolist()
                mesh = bpy.data.meshes.new(unique_name)
                mesh.from_pydata(verts, [], faces)
                mesh.update()
                obj = bpy.data.objects.new(unique_name, mesh)
                for col in bpy.data.collections:
                    if col.name == "Scene Collection" or not col.children:
                        col.objects.link(obj)
                        break
                else:
                    bpy.context.scene.collection.objects.link(obj)
                bpy.context.view_layer.objects.active = obj
                obj.select_set(True)
                return mesh
            except Exception:
                return None

        def _patched_load_mesh(self, reload=False):
            if self.mesh_object is not None and not reload:
                return self.mesh_object
            if not hasattr(self, "input_file") or not self.input_file or not os.path.isfile(self.input_file):
                raise AssertionError("Mesh with path %s wasn't found!" % getattr(self, "input_file", ""))
            if self.input_type == "file_stl":
                use_fallback = not (hasattr(bpy.ops.import_mesh, "stl") and callable(getattr(bpy.ops.import_mesh, "stl", None)))
                if not use_fallback:
                    try:
                        return _original_load_mesh(self, reload)
                    except (RuntimeError, AttributeError) as err:
                        if "could not be found" in str(err) or "import_mesh" in str(err):
                            use_fallback = True
                        else:
                            raise
                if use_fallback:
                    bpy.ops.object.select_all(action="DESELECT")
                    mesh = _stl_load_fallback(self, self.input_file, self.unique_name)
                    if mesh is not None:
                        self._mesh_object = mesh
                        if bpy.context.view_layer.objects.active:
                            bpy.ops.object.delete()
                        return self.mesh_object
            return _original_load_mesh(self, reload)

        try:
            def _patched_read_relative(filename, start_file_path):
                resolved = _robot_toolkit_resolve_mesh_path(filename, start_file_path)
                if os.path.isfile(resolved):
                    return resolved
                if _original_read_relative:
                    return _original_read_relative(filename, start_file_path)
                return resolved
            phobos_xml.read_relative_filename = _patched_read_relative
            phobos_repr.read_relative_filename = _patched_read_relative
            if _original_load_mesh:
                phobos_repr.Mesh.load_mesh = _patched_load_mesh
            robot = core.Robot(inputfile=self.filepath)
            if robot is None:
                self.report({"ERROR"}, "导入失败：解析返回为空，请检查文件是否为有效 URDF/SDF")
                return {"CANCELLED"}
            if getattr(robot, "links", None) is None:
                self.report({"ERROR"}, "导入失败：文件中未包含有效的 link 定义，请确认格式与内容")
                return {"CANCELLED"}
            if not (hasattr(robot, "links") and len(robot.links) > 0):
                self.report({"ERROR"}, "导入失败：未找到任何 link，请检查 URDF/SDF 文件是否完整")
                return {"CANCELLED"}
            try:
                createRobot(robot)
            except AttributeError as ae:
                if "links" in str(ae):
                    self.report(
                        {"ERROR"},
                        "导入失败：内部对象未就绪（可能与场景/插件状态有关）。请尝试：1) 关闭当前文件后重新打开再导入；2) 禁用本插件后重新启用再试。",
                    )
                    return {"CANCELLED"}
                raise
            for layer in ["link", "inertial", "visual", "collision", "sensor"]:
                try:
                    bUtils.toggleLayer(layer, True)
                except Exception:
                    pass
        except Exception as e:
            self.report({"ERROR"}, "导入 URDF/SMURF 失败: %s" % str(e))
            return {"CANCELLED"}
        finally:
            if _original_read_relative is not None:
                phobos_xml.read_relative_filename = _original_read_relative
                phobos_repr.read_relative_filename = _original_read_relative
            if _original_load_mesh is not None:
                phobos_repr.Mesh.load_mesh = _original_load_mesh

        self.report({"INFO"}, "已通过 Phobos 导入机器人: %s" % self.filepath)
        return {"FINISHED"}


# ============== 1. 准备 URDF 模型 ==============
class ROBOT_OT_PrepareURDF(Operator):
    bl_idname = "robot.prepare_urdf"
    bl_label = "Prepare URDF Model"
    bl_description = "识别选中的 Armature，将 base_link 设为活动对象以便后续合并"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected = [o for o in context.selected_objects if o.type == "ARMATURE"]
        if not selected:
            self.report({"WARNING"}, "请先选中至少一个 Armature（骨架）")
            return {"CANCELLED"}

        # 尝试将 base_link 设为 active
        base = None
        for o in selected:
            if o.name.lower().startswith("base_link") or "base_link" in o.name.lower():
                base = o
                break
        if not base and selected:
            base = selected[0]

        bpy.ops.object.select_all(action="DESELECT")
        for o in selected:
            o.select_set(True)
        context.view_layer.objects.active = base
        # 全选物体，方便下一步直接点「清空并保持变换」
        bpy.ops.object.select_all(action="SELECT")
        context.view_layer.objects.active = base
        self.report({"INFO"}, "已准备 %d 个骨架并全选物体，活动对象: %s" % (len(selected), base.name))
        return {"FINISHED"}


# ============== 1.2 一键执行：准备 → 清空 → 合并 → 绑定 ==============
class ROBOT_OT_OneClickPrepareToBind(Operator):
    bl_idname = "robot.one_click_prepare_to_bind"
    bl_label = "One-Click: Prepare → Bind (4 steps)"
    bl_description = "Run: Prepare URDF → Clear & Keep Transform → Merge & Relink → Bind Meshes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected = [o for o in context.selected_objects if o.type == "ARMATURE"]
        if not selected:
            self.report({"WARNING"}, "Select at least one Armature first")
            return {"CANCELLED"}

        try:
            bpy.ops.robot.prepare_urdf()
            bpy.ops.robot.clear_keep_transform()
            armatures = [o for o in context.scene.objects if o.type == "ARMATURE"]
            if not armatures:
                self.report({"ERROR"}, "No Armature found after clear")
                return {"CANCELLED"}
            base = None
            for o in armatures:
                if "base_link" in o.name.lower():
                    base = o
                    break
            if not base:
                base = armatures[0]
            for o in context.view_layer.objects:
                o.select_set(o in armatures)
            context.view_layer.objects.active = base
            bpy.ops.robot.merge_relink()
            bpy.ops.robot.bind_meshes()
        except RuntimeError as e:
            self.report({"ERROR"}, "One-click failed: %s" % str(e))
            return {"CANCELLED"}

        self.report({"INFO"}, "One-Click: Prepare → Bind done")
        return {"FINISHED"}


# ============== 1.5 清空并保持变换（先剥离再合并的关键一步） ==============
class ROBOT_OT_ClearKeepTransform(Operator):
    bl_idname = "robot.clear_keep_transform"
    bl_label = "Clear and Keep Transformation"
    bl_description = "先记录当前 URDF 骨骼层级，再断开父子关系并保持世界位置（合并时将按此层级重连）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            bpy.ops.object.select_all(action="SELECT")
            selected = list(context.selected_objects)
        if not selected:
            self.report({"WARNING"}, "场景中没有可操作物体")
            return {"CANCELLED"}

        # 在清空父级之前：记录所有 Armature 的父子关系（即 URDF 的 link 层级），供「合并并重连骨架」使用
        armatures = [o for o in selected if o.type == "ARMATURE"]
        arm_set = set(armatures)
        hierarchy_list = []
        for arm in armatures:
            parent_name = None
            if arm.parent and arm.parent.type == "ARMATURE" and arm.parent in arm_set:
                parent_name = arm.parent.name
            hierarchy_list.append([arm.name, parent_name])
        try:
            context.scene.robot_animator.stored_armature_hierarchy = json.dumps(hierarchy_list, ensure_ascii=False)
        except Exception:
            pass

        try:
            bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
        except RuntimeError as e:
            self.report({"ERROR"}, "清空父级失败: %s" % str(e))
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            "已记录 %d 个骨架的 URDF 层级并清空父级保持变换，请只选骨架后点「合并并重连骨架」" % len(armatures),
        )
        return {"FINISHED"}


# ============== 2. 合并骨架并重建层级 ==============
# 四足：calf -> thigh -> abad/hip -> base_link；人形：ankle_roll -> ankle_pitch -> knee -> hip_pitch -> hip_roll -> pelvis 等
CHAIN_RULES = [
    ("calf", "thigh"),
    ("thigh", "abad"),
    ("thigh", "hip"),
    ("abad", "base_link"),
    ("hip", "base_link"),
    ("trunk", "base_link"),
    ("pelvis", None),  # 根，无父
    ("hip_roll", "pelvis"),
    ("hip_yaw", "pelvis"),
    ("hip_pitch", "hip_roll"),
    ("hip_pitch", "hip_yaw"),
    ("knee", "hip_pitch"),
    ("ankle_pitch", "knee"),
    ("ankle_roll", "ankle_pitch"),
    ("shoulder_roll", "torso"),
    ("shoulder_pitch", "shoulder_roll"),
    ("shoulder_yaw", "shoulder_pitch"),
    ("elbow_pitch", "shoulder_yaw"),
    ("elbow", "shoulder"),
    ("wrist", "elbow"),
]


def _bone_name_to_prefix_and_suffix(bone_name: str):
    """将骨骼名按常见 URDF 命名拆成后缀(关节类型)和侧边(L/R)等，用于找父骨骼。"""
    name = bone_name.strip().lower()
    # 人形: hip_roll_L_link -> (l, hip_roll), ankle_pitch_l_link -> (l, ankle_pitch)
    for rule_child, rule_parent in CHAIN_RULES:
        if rule_parent is None:
            if rule_child in name and ("pelvis" in name or name == "pelvis"):
                return "", "pelvis"
            continue
        if rule_child in name:
            # 提取侧边 L/R（可能为 _l_ 或 _L_ 或 _link 前的段）
            side = ""
            if "_l_link" in name or "_l_" in name or name.endswith("_l"):
                side = "l"
            elif "_r_link" in name or "_r_" in name or name.endswith("_r"):
                side = "r"
            return side, rule_child
    if "base_link" in name:
        return "", "base_link"
    return None, None


def _find_parent_bone_name(bone_names_list, child_bone_name: str):
    """根据 CHAIN_RULES 和命名中的侧边(L/R)找到父骨骼名。"""
    side, suffix = _bone_name_to_prefix_and_suffix(child_bone_name)
    if suffix is None:
        return None
    if suffix == "pelvis" or "pelvis" in child_bone_name.lower().split("_")[0]:
        return None
    bone_names = list(bone_names_list)
    for rule_child, rule_parent in CHAIN_RULES:
        if rule_child != suffix or rule_parent is None:
            continue
        if rule_parent == "base_link" or rule_parent == "pelvis":
            for n in bone_names:
                if rule_parent in n.lower() or (rule_parent == "pelvis" and n.lower() == "pelvis"):
                    return n
            return None
        # 同侧父级，如 hip_pitch_L_link 的父为 hip_roll_L_link
        want = rule_parent + ("_" + side if side else "") + "_link"
        if want in bone_names:
            return want
        want2 = rule_parent + "_" + side + "_link" if side else rule_parent + "_link"
        for n in bone_names:
            nlower = n.lower()
            if rule_parent in nlower and (not side or ("_" + side + "_") in nlower or nlower.endswith("_" + side)):
                return n
    return None


class ROBOT_OT_MergeAndRelink(Operator):
    bl_idname = "robot.merge_relink"
    bl_label = "Merge & Relink Armature"
    bl_description = "合并选中的多个 Armature，保留 URDF 关节名并按层级重连（Keep Offset）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected = [o for o in context.selected_objects if o.type == "ARMATURE"]
        if len(selected) < 1:
            self.report({"WARNING"}, "请选中至少一个 Armature")
            return {"CANCELLED"}

        active = context.view_layer.objects.active
        if not active or active.type != "ARMATURE" or active not in selected:
            for o in selected:
                if "base_link" in o.name.lower() or o.name.lower() == "pelvis":
                    context.view_layer.objects.active = o
                    break
            if context.view_layer.objects.active not in selected:
                context.view_layer.objects.active = selected[0]
            active = context.view_layer.objects.active

        # 合并前：按 Join 顺序记录每个 Armature 的物体名和骨骼名，用于合并后恢复 URDF 命名
        ordered_armatures = [active] + [o for o in selected if o != active]
        desired_bone_names = []
        for arm_obj in ordered_armatures:
            for b in arm_obj.data.bones:
                if len(arm_obj.data.bones) == 1:
                    desired_bone_names.append(arm_obj.name)
                else:
                    desired_bone_names.append(arm_obj.name + "_" + b.name)

        # 执行 Join
        try:
            with context.temp_override(
                active_object=active,
                selected_editable_objects=selected,
            ):
                bpy.ops.object.join()
        except Exception as e:
            self.report({"ERROR"}, "合并失败: %s" % str(e))
            return {"CANCELLED"}

        arm_obj = context.view_layer.objects.active
        if arm_obj.type != "ARMATURE":
            self.report({"ERROR"}, "合并后活动对象不是 Armature")
            return {"CANCELLED"}

        arm = arm_obj.data
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = arm.edit_bones
        merged_bones_ordered = list(edit_bones)

        # 1) 两段重命名：合并后多为 Bone.001…，先改为临时名再改为 URDF 名，避免冲突
        n = min(len(merged_bones_ordered), len(desired_bone_names))
        for i in range(n):
            merged_bones_ordered[i].name = "_tmp_%d" % i
        for i in range(n):
            edit_bones["_tmp_%d" % i].name = desired_bone_names[i]

        bone_names_after_rename = [eb.name for eb in edit_bones]
        bone_name_set = set(bone_names_after_rename)

        # 2) 重建父子关系：优先使用「清空并保持变换」时记录的 URDF 层级，否则用命名规则推断
        stored_hierarchy = getattr(context.scene.robot_animator, "stored_armature_hierarchy", None) or ""
        child_to_parent = {}
        try:
            stored_list = json.loads(stored_hierarchy)
            if isinstance(stored_list, list) and len(stored_list) > 0:
                for item in stored_list:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        child_name, parent_name = item[0], item[1]
                        if parent_name and child_name != parent_name and parent_name in bone_name_set:
                            child_to_parent[str(child_name)] = str(parent_name)
        except (json.JSONDecodeError, TypeError):
            pass

        for eb in edit_bones:
            parent_name = child_to_parent.get(eb.name) if child_to_parent else None
            if not parent_name:
                parent_name = _find_parent_bone_name(bone_names_after_rename, eb.name)
            if not parent_name or parent_name == eb.name:
                continue
            parent_eb = edit_bones.get(parent_name)
            if not parent_eb:
                continue
            eb.parent = parent_eb
            eb.use_connect = False

        bpy.ops.object.mode_set(mode="OBJECT")
        msg = "已合并并重连层级（按记录的 URDF 层级）" if child_to_parent else "已合并并重连层级（按命名规则）"
        self.report({"INFO"}, "%s，骨骼已恢复为 URDF 名称: %s" % (msg, arm_obj.name))
        return {"FINISHED"}


# ============== 3. 将网格重绑定到骨骼 ==============
def _match_mesh_to_bone(mesh_name: str, bone_names: list):
    """若 mesh 名包含某骨骼名（如 FL_thigh_visual 含 FL_thigh），返回该骨骼名。取最长匹配。"""
    mesh_lower = mesh_name.lower()
    best = None
    best_len = 0
    for bn in bone_names:
        if bn.lower() in mesh_lower or mesh_lower in bn.lower():
            # 更精确：mesh 名包含骨骼名
            if bn.lower() in mesh_lower and len(bn) > best_len:
                best = bn
                best_len = len(bn)
    return best


# ============== 2.5 一键切换：欧拉角 (XYZ) ↔ 四元数 ==============
class ROBOT_OT_BonesEulerMode(Operator):
    bl_idname = "robot.bones_euler_mode"
    bl_label = "Toggle Euler / Quaternion"
    bl_description = "Switch all bones: Euler (XYZ) or Quaternion"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm_obj = None
        for o in context.scene.objects:
            if o.type == "ARMATURE" and o == context.view_layer.objects.active:
                arm_obj = o
                break
        if not arm_obj:
            for o in context.scene.objects:
                if o.type == "ARMATURE":
                    arm_obj = o
                    break
        if not arm_obj:
            self.report({"WARNING"}, "Select an Armature first")
            return {"CANCELLED"}

        all_euler = all(pb.rotation_mode == "XYZ" for pb in arm_obj.pose.bones)
        target_mode = "QUATERNION" if all_euler else "XYZ"

        prev_mode = arm_obj.mode
        try:
            if arm_obj.mode != "POSE":
                with context.temp_override(active_object=arm_obj):
                    bpy.ops.object.mode_set(mode="POSE")
            for pbone in arm_obj.pose.bones:
                pbone.rotation_mode = target_mode
            if prev_mode != "POSE":
                with context.temp_override(active_object=arm_obj):
                    bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError as e:
            self.report({"ERROR"}, "Toggle failed: %s" % str(e))
            return {"CANCELLED"}

        msg = "All bones → Quaternion" if target_mode == "QUATERNION" else "All bones → Euler (XYZ)"
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class ROBOT_OT_BindMeshes(Operator):
    bl_idname = "robot.bind_meshes"
    bl_label = "Bind Meshes to Bones"
    bl_description = "将场景中的 Mesh 按名称匹配到当前 Armature 的骨骼上，保持世界空间位置"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm_obj = None
        for o in context.scene.objects:
            if o.type == "ARMATURE" and o == context.view_layer.objects.active:
                arm_obj = o
                break
        if not arm_obj:
            for o in context.scene.objects:
                if o.type == "ARMATURE":
                    arm_obj = o
                    break
        if not arm_obj:
            self.report({"WARNING"}, "场景中需要至少一个 Armature")
            return {"CANCELLED"}

        # 与手动绑定一致：用 Blender 的「父级到骨骼并保持变换」，避免位置被重置
        bone_names = [b.name for b in arm_obj.data.bones]
        meshes = [o for o in context.scene.objects if o.type == "MESH"]
        bound = 0
        prev_mode = arm_obj.mode
        try:
            for mesh in meshes:
                if mesh.parent == arm_obj and mesh.parent_type == "BONE":
                    continue
                bone_name = _match_mesh_to_bone(mesh.name, bone_names)
                if not bone_name or bone_name not in arm_obj.data.bones:
                    continue
                # 选区：仅当前 mesh + armature；活动对象 = armature；活动骨骼 = 目标骨骼
                with context.temp_override(
                    active_object=arm_obj,
                    selected_editable_objects=[mesh, arm_obj],
                ):
                    bpy.ops.object.mode_set(mode="OBJECT")
                    mesh.select_set(True)
                    arm_obj.select_set(True)
                    context.view_layer.objects.active = arm_obj
                    bpy.ops.object.mode_set(mode="POSE")
                    arm_obj.data.bones.active = arm_obj.data.bones[bone_name]
                    bpy.ops.object.parent_set(type="BONE", keep_transform=True)
                bound += 1
        finally:
            try:
                with context.temp_override(active_object=arm_obj):
                    bpy.ops.object.mode_set(mode="OBJECT")
            except (RuntimeError, TypeError):
                pass

        self.report({"INFO"}, "已将 %d 个网格绑定到骨骼" % bound)
        return {"FINISHED"}


# ============== 4. 刷新关节列表（从当前 Armature） ==============
class ROBOT_OT_RefreshJointList(Operator):
    bl_idname = "robot.refresh_joint_list"
    bl_label = "Refresh from Armature"
    bl_description = "从当前选中的 Armature 刷新可导出关节列表"

    def execute(self, context):
        scene = context.scene
        robot = scene.robot_animator
        robot.joint_list.clear()
        arm_obj = context.view_layer.objects.active
        if not arm_obj or arm_obj.type != "ARMATURE":
            self.report({"WARNING"}, "请先选中一个 Armature")
            return {"CANCELLED"}
        for b in arm_obj.data.bones:
            item = robot.joint_list.add()
            item.bone_name = b.name
            item.enabled = True
            item.axis = "Y"
        self.report({"INFO"}, "已添加 %d 个关节" % len(robot.joint_list))
        return {"FINISHED"}


# ============== 4.5 关节列表全选/全不选 ==============
class ROBOT_OT_ToggleJointList(Operator):
    bl_idname = "robot.toggle_joint_list"
    bl_label = "Toggle All Joints"
    bl_description = "切换关节列表中所有勾选：全部勾选或全部取消"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        robot = context.scene.robot_animator
        if not robot.joint_list:
            return {"CANCELLED"}
        all_on = all(item.enabled for item in robot.joint_list)
        for item in robot.joint_list:
            item.enabled = not all_on
        status = _tr(context, "All unchecked", "已全部取消") if all_on else _tr(context, "All checked", "已全部勾选")
        self.report({"INFO"}, status)
        return {"FINISHED"}


# ============== 5. CSV 导出 ==============
class ROBOT_OT_ExportCSV(Operator):
    bl_idname = "robot.export_csv"
    bl_label = "Export Dynamic CSV"
    bl_description = "按当前关节列表与帧范围导出旋转角度到 CSV"

    filepath: StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        scene = context.scene
        robot = scene.robot_animator
        if not robot.export_path:
            self.filepath = bpy.path.abspath("//dynamic_export.csv")
        else:
            self.filepath = bpy.path.abspath(robot.export_path)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        scene = context.scene
        robot = scene.robot_animator
        arm_obj = context.view_layer.objects.active
        if not arm_obj or arm_obj.type != "ARMATURE":
            self.report({"WARNING"}, "请先选中要导出的 Armature")
            return {"CANCELLED"}

        frame_start = scene.frame_start
        frame_end = scene.frame_end
        if robot.use_custom_range:
            frame_start = robot.frame_start
            frame_end = robot.frame_end
        if frame_start > frame_end:
            self.report({"WARNING"}, "起始帧不能大于结束帧，请检查自定义帧范围")
            return {"CANCELLED"}

        items = [i for i in robot.joint_list if i.enabled and i.bone_name]
        if not items:
            self.report({"WARNING"}, "请在关节列表中勾选要导出的关节")
            return {"CANCELLED"}

        # 确定每根骨骼的旋转轴（索引）
        axis_map = {"X": 0, "Y": 1, "Z": 2}
        header = ["frame"] + [i.bone_name for i in items]
        rows = []
        orig_frame = scene.frame_current
        for f in range(frame_start, frame_end + 1):
            scene.frame_set(f)
            row = [f]
            for item in items:
                bone = arm_obj.pose.bones.get(item.bone_name)
                if not bone:
                    row.append(0.0)
                    continue
                # 取局部旋转欧拉角对应轴，按设置输出角度或弧度
                rot = bone.rotation_euler
                idx = axis_map.get(item.axis, 2)
                if robot.export_angle_unit == "DEGREES":
                    val = round(rot[idx] * 180.0 / 3.14159265359, 6)
                else:
                    val = round(rot[idx], 6)
                row.append(val)
            rows.append(row)
        scene.frame_set(orig_frame)

        try:
            with open(self.filepath, "w", newline="", encoding="utf-8") as fp:
                writer = csv.writer(fp)
                writer.writerow(header)
                writer.writerows(rows)
        except Exception as e:
            self.report({"ERROR"}, "写入 CSV 失败: %s" % str(e))
            return {"CANCELLED"}

        robot.export_path = bpy.path.relpath(self.filepath)
        self.report({"INFO"}, "已导出: %s (%d 帧, %d 关节)" % (self.filepath, len(rows), len(items)))
        return {"FINISHED"}


# ============== 主面板 ==============
class ROBOT_PT_MainPanel(Panel):
    bl_label = "Robot Animator Toolkit"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RobotTools"

    def draw(self, context):
        layout = self.layout
        robot = getattr(context.scene, "robot_animator", None)
        if robot is None:
            layout.label(text="Robot Animator Toolkit")
            return
        layout.row().prop(robot, "language", text=_tr(context, "Language", "语言"))
        layout.separator()
        col = layout.column(align=True)
        col.operator("robot.import_urdf", text=_tr(context, "Import URDF/SMURF (Phobos)", "导入 URDF/SMURF (Phobos)"), icon="IMPORT")
        col.operator("robot.one_click_prepare_to_bind", text=_tr(context, "One-Click: Prepare → Bind (4 steps)", "一键：准备 → 绑定 (4 步)"), icon="PLAY")
        col.operator("robot.prepare_urdf", text=_tr(context, "Prepare URDF Model", "准备 URDF 模型"), icon="OUTLINER_OB_ARMATURE")
        col.operator("robot.clear_keep_transform", text=_tr(context, "Clear and Keep Transform", "清空并保持变换"), icon="UNLINKED")
        col.operator("robot.merge_relink", text=_tr(context, "Merge & Relink Armature", "合并并重连骨架"), icon="CONSTRAINT_BONE")
        col.operator("robot.bind_meshes", text=_tr(context, "Bind Meshes to Bones", "将网格绑定到骨骼"), icon="MESH_DATA")
        col.operator("robot.bones_euler_mode", text=_tr(context, "Toggle Euler / Quaternion", "切换 欧拉角 / 四元数"), icon="DRIVER_ROTATIONAL_DIFFERENCE")


# ============== 关节列表子面板 ==============
class ROBOT_PT_JointListPanel(Panel):
    bl_label = ""  # 标题由 draw_header 按语言显示（中/英）
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RobotTools"
    bl_parent_id = "ROBOT_PT_MainPanel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.label(text=_tr(context, "Dynamic CSV - Joint List", "动态 CSV - 关节列表"))

    def draw(self, context):
        layout = self.layout
        robot = getattr(context.scene, "robot_animator", None)
        if robot is None:
            return
        row = layout.row(align=True)
        row.operator("robot.refresh_joint_list", text=_tr(context, "Refresh from Armature", "从骨架刷新关节列表"), icon="FILE_REFRESH")
        row.operator("robot.toggle_joint_list", text=_tr(context, "All / None", "全选/全不选"), icon="CHECKBOX_HLT")
        layout.template_list(
            "ROBOT_UL_JointList",
            "joint_list",
            robot,
            "joint_list",
            robot,
            "joint_list_index",
            rows=6,
        )
        layout.prop(robot, "export_angle_unit", text=_tr(context, "Export unit", "导出单位"))
        layout.prop(robot, "use_custom_range", text=_tr(context, "Custom Frame Range", "自定义帧范围"))
        if robot.use_custom_range:
            row = layout.row(align=True)
            row.prop(robot, "frame_start", text="Start")
            row.prop(robot, "frame_end", text="End")
        layout.prop(robot, "export_path", text=_tr(context, "Export Path", "导出路径"))
        layout.separator()
        layout.operator("robot.export_csv", text=_tr(context, "Export Dynamic CSV", "导出动态 CSV"), icon="EXPORT")


# ============== 场景属性 ==============
class RobotAnimatorSettings(PropertyGroup):
    joint_list: CollectionProperty(type=RobotJointExportItem)
    joint_list_index: IntProperty(name="Index", default=0)
    use_custom_range: BoolProperty(name="Custom Frame Range", default=False)
    frame_start: IntProperty(name="Start", default=1)
    frame_end: IntProperty(name="End", default=250)
    export_path: StringProperty(
        name="Export Path",
        default="//dynamic_export.csv",
        subtype="FILE_PATH",
    )
    language: EnumProperty(
        name="Language",
        description="UI language / 界面语言",
        items=[
            ("EN", "English", ""),
            ("ZH", "中文", ""),
        ],
        default="ZH",
    )
    # 「清空并保持变换」时记录的 Armature 父子关系（JSON），供「合并并重连骨架」恢复 URDF 层级
    stored_armature_hierarchy: StringProperty(name="Stored Armature Hierarchy", default="")
    # 导出 CSV 时旋转值的单位：角度 或 弧度（静态枚举避免 Blender 4.2 安装时注册报错）
    export_angle_unit: EnumProperty(
        name="Export Unit",
        description="导出旋转值为角度或弧度",
        items=[
            ("DEGREES", "Degrees", "Export as degrees (°)"),
            ("RADIANS", "Radians", "Export as radians (rad)"),
        ],
        default="DEGREES",
    )


# ============== 中英文文案 ==============
def _tr(context, en, zh):
    if not context or not getattr(context, "scene", None):
        return zh
    robot = getattr(context.scene, "robot_animator", None)
    if robot is None:
        return zh
    return zh if getattr(robot, "language", "ZH") == "ZH" else en


# ============== 注册 / 注销 ==============
classes = (
    _PhobosWireFrameSettingsCompat,
    RobotJointExportItem,
    ROBOT_UL_JointList,
    ROBOT_OT_ImportURDF,
    ROBOT_OT_OneClickPrepareToBind,
    ROBOT_OT_PrepareURDF,
    ROBOT_OT_ClearKeepTransform,
    ROBOT_OT_MergeAndRelink,
    ROBOT_OT_BonesEulerMode,
    ROBOT_OT_BindMeshes,
    ROBOT_OT_RefreshJointList,
    ROBOT_OT_ToggleJointList,
    ROBOT_OT_ExportCSV,
    ROBOT_PT_MainPanel,
    ROBOT_PT_JointListPanel,
    RobotAnimatorSettings,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.robot_animator = PointerProperty(type=RobotAnimatorSettings)
    # Phobos 导入时需要 Scene.phoboswireframesettings，在插件启用时就注册 PointerProperty 即可
    # 注意：此处不能访问 bpy.data.scenes（在安装/启用阶段 data 可能是 RestrictData）
    if not hasattr(bpy.types.Scene, "phoboswireframesettings"):
        bpy.types.Scene.phoboswireframesettings = PointerProperty(
            type=_PhobosWireFrameSettingsCompat,
            name="Phobos Wire Frame Settings",
        )


def unregister():
    if hasattr(bpy.types.Scene, "phoboswireframesettings"):
        try:
            del bpy.types.Scene.phoboswireframesettings
        except Exception:
            pass
    del bpy.types.Scene.robot_animator
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
