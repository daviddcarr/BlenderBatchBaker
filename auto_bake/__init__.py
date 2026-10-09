"""Auto Bake - automates the Final / Materials / Metallic / Alpha texture baking workflow.

Layout expected in the .blend:
    Final       T0_Thing            (export object, gets one generated material)
    Materials   T0_Thing.Mat        (any number of node materials: color/roughness/emission)
    Metallic    T0_Thing.Met        (only "Metallic" / "NonMetallic" materials)
    Alpha       T0_Thing.Alpha      (only "Opaque" / "Transparent" materials)

Textures are written to //Textures/<Object>_<Suffix>.png
"""

import hashlib
import os
from array import array

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from mathutils import kdtree


FINAL_COLL = "Final"
MESH_SUFFIX = ".Mesh"
TEXTURE_DIR = "Textures"
TARGET_NODE = "AutoBake_Target"

# Max distance in UV space (0..1) for a Final UV point to count as present in a source copy.
UV_TOLERANCE = 0.001

# Suffixes from earlier hand-made bakes; disconnected image nodes holding these get adopted.
KNOWN_TEX_SUFFIXES = ("_BaseColor", "_Diffuse", "_Roughness", "_Metallic", "_Emission", "_Alpha")


class SourceLayer:
    """A collection of suffixed copies of the Final objects that textures are baked from.

    flat_mats are (name, color) pairs for the black/white mask materials; the first one is
    what every face gets when Copy Final makes a new copy.
    """

    def __init__(self, key, coll, suffix, flat_mats=()):
        self.key = key
        self.coll = coll
        self.suffix = suffix
        self.flat_mats = flat_mats


LAYER_MAT = SourceLayer("mat", "Materials", ".Mat")
LAYER_MET = SourceLayer("met", "Metallic", ".Met",
                        (("NonMetallic", (0.0, 0.0, 0.0)), ("Metallic", (1.0, 1.0, 1.0))))
LAYER_ALPHA = SourceLayer("alpha", "Alpha", ".Alpha",
                          (("Opaque", (1.0, 1.0, 1.0)), ("Transparent", (0.0, 0.0, 0.0))))
ALL_LAYERS = (LAYER_MAT, LAYER_MET, LAYER_ALPHA)


def active_layers(settings):
    layers = [LAYER_MAT]
    if settings.include_metallic:
        layers.append(LAYER_MET)
    if settings.include_alpha:
        layers.append(LAYER_ALPHA)
    return layers


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class AutoBakeSettings(bpy.types.PropertyGroup):
    include_metallic: BoolProperty(
        name="Metallic",
        description="Bake a _Metallic texture from the Metallic collection (.Met objects)",
        default=True,
    )
    include_alpha: BoolProperty(
        name="Alpha",
        description="Bake an _Alpha texture from the Alpha collection (.Alpha objects). "
                    "White (Opaque) = solid, black (Transparent) = see-through",
        default=False,
    )
    pack_alpha: BoolProperty(
        name="Pack into BaseColor",
        description="Store the alpha in the _BaseColor PNG's alpha channel instead of a separate "
                    "_Alpha texture (what the glTF exporter expects)",
        default=False,
    )
    include_emission: BoolProperty(
        name="Emission",
        description="Bake an _Emission texture. Objects whose materials don't emit are skipped",
        default=False,
    )
    resolution: IntProperty(
        name="Resolution",
        description="Width and height of the generated textures",
        default=512, min=16, max=8192,
    )
    samples: IntProperty(
        name="Bake Samples",
        description="Cycles samples used while baking (restored afterwards). "
                    "Color/roughness bakes with lighting off only need a few for anti-aliasing",
        default=16, min=1, max=4096,
    )
    margin: IntProperty(
        name="Margin",
        description="Pixels to extend baked islands by, to hide UV seams",
        default=16, min=0, max=64,
    )
    only_selected: BoolProperty(
        name="Selected Only",
        description="Only bake objects that are selected (a selected copy counts for its Final object)",
        default=False,
    )
    copy_spacing: FloatProperty(
        name="Copy Spacing",
        description="Copy Final shifts .Mat copies by -spacing on X, .Met by -2x and .Alpha by -3x. "
                    "0 keeps the copies in place",
        default=0.0, unit='LENGTH',
    )
    export_dir: StringProperty(
        name="Export Folder",
        description="Last folder Batch Export wrote to",
        subtype='DIR_PATH',
    )
    export_center: BoolProperty(
        name="Center at Origin",
        description="Move each top-level object to 0,0,0 while it's exported (rotation and scale are kept)",
        default=True,
    )
    export_textures: BoolProperty(
        name="Embed Textures",
        description="Embed the baked textures in each .glb. Off exports materials without images",
        default=True,
    )
    validated_hash: StringProperty(
        description="Fingerprint of the scene when Validate last passed",
        options={'HIDDEN'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def show_popup(context, title, lines, icon='INFO'):
    for line in lines:
        print(f"[Auto Bake] {line}")
    if not lines or bpy.app.background or context.window is None:
        return

    def draw(menu, _context):
        for line in lines:
            menu.layout.label(text=line)

    context.window_manager.popup_menu(draw, title=title, icon=icon)


def node_tree_of(mat):
    if mat.node_tree is None:
        mat.use_nodes = True
    return mat.node_tree


def iter_mesh_objects(coll):
    """Mesh objects in coll and any collections nested inside it."""
    return sorted((o for o in coll.all_objects if o.type == 'MESH'), key=lambda o: o.name)


def texture_name(final, suffix):
    """Tomato.Bud + _BaseColor -> Tomato_Bud_BaseColor."""
    return final.name.replace(".", "_") + suffix


def collection_path(root, obj):
    """Nested collections between root and obj, outermost first ([] if obj is directly in root)."""
    if obj.name in root.objects:
        return []
    for child in root.children:
        if obj.name in child.all_objects:
            return [child] + collection_path(child, obj)
    return []


def mirrored_collection(final_coll, final, layer):
    """The layer collection matching final's nesting: Final/Tomato -> Materials/Tomato.Mat."""
    root = target = bpy.data.collections[layer.coll]
    for sub in collection_path(final_coll, final):
        name = sub.name + layer.suffix
        coll = bpy.data.collections.get(name)
        if coll is None:
            coll = bpy.data.collections.new(name)
        if coll not in root.children_recursive:
            target.children.link(coll)
        target = coll
    return target


def parent_depth(obj):
    depth = 0
    while obj.parent is not None:
        obj = obj.parent
        depth += 1
    return depth


def export_groups(final_coll):
    """[(top-level object, [it + its descendants])] for the Final collection.

    Top-level means no parent inside Final. Only descendants that are in Final are included,
    so source copies parented elsewhere never end up in an export.
    """
    members = set(final_coll.all_objects)
    groups = []
    for obj in sorted(members, key=lambda o: o.name):
        if obj.parent in members:
            continue
        groups.append((obj, [obj] + [c for c in obj.children_recursive if c in members]))
    return groups


def base_name(name):
    lower = name.lower()
    for layer in ALL_LAYERS:
        if lower.endswith(layer.suffix.lower()):
            return name[: -len(layer.suffix)]
    return name


def find_source(final, layer, coll):
    """The copy of final in coll; the suffix is matched case-insensitively (.Alpha / .alpha)."""
    name = final.name + layer.suffix
    obj = bpy.data.objects.get(name)
    if obj is not None:
        return obj
    wanted = name.lower()
    for o in coll.all_objects:
        if o.name.lower() == wanted:
            return o
    return None


def tree_emits(tree, seen=None):
    seen = seen if seen is not None else set()
    if tree is None or tree.name in seen:
        return False
    seen.add(tree.name)
    for node in tree.nodes:
        if node.bl_idname == 'ShaderNodeEmission':
            return True
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            strength = node.inputs.get("Emission Strength")
            color = node.inputs.get("Emission Color")
            if strength and color and (strength.is_linked or strength.default_value > 0.0) \
                    and (color.is_linked or any(c > 0.0 for c in color.default_value[:3])):
                return True
        if node.bl_idname == 'ShaderNodeGroup' and tree_emits(node.node_tree, seen):
            return True
    return False


def object_emits(obj):
    return any(s.material and tree_emits(s.material.node_tree) for s in obj.material_slots)


def ensure_flat_material(name, color):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        nt = node_tree_of(mat)
        bsdf = next((n for n in nt.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (*color, 1.0)
        mat.diffuse_color = (*color, 1.0)
    return mat


def texture_specs(settings, emits):
    """(suffix, bake type, BSDF input, is non-color data, source layer key) per texture."""
    specs = [
        ("_BaseColor", 'DIFFUSE', "Base Color", False, "mat"),
        ("_Roughness", 'ROUGHNESS', "Roughness", True, "mat"),
    ]
    if settings.include_metallic:
        specs.append(("_Metallic", 'DIFFUSE', "Metallic", True, "met"))
    if settings.include_alpha:
        # When packed, _Alpha is only a bake intermediate: not wired, not saved.
        specs.append(("_Alpha", 'DIFFUSE', None if settings.pack_alpha else "Alpha", True, "alpha"))
    if settings.include_emission and emits:
        specs.append(("_Emission", 'EMIT', "Emission Color", False, "mat"))
    return specs


def uv_points(obj):
    uv = obj.data.uv_layers.active
    flat = [0.0] * (len(uv.data) * 2)
    uv.data.foreach_get("uv", flat)
    return {(round(flat[i], 5), round(flat[i + 1], 5)) for i in range(0, len(flat), 2)}


def missing_uv_points(final, source):
    """Final UV points with no source UV point within UV_TOLERANCE.

    Extra source points (insets, detail added only for texturing) are fine. UVs alone are
    compared, not vertex positions, so reshaped geometry with the same unwrap still passes.
    """
    src = uv_points(source)
    tree = kdtree.KDTree(len(src))
    for i, (u, v) in enumerate(src):
        tree.insert((u, v, 0.0), i)
    tree.balance()
    points = uv_points(final)
    missing = sum(1 for (u, v) in points if tree.find((u, v, 0.0))[2] > UV_TOLERANCE)
    return missing, len(points)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def fingerprint(context, settings):
    """Cheap summary of everything validation depends on, so a stale validation can be detected.

    Final objects' material slots are left out because baking replaces them.
    """
    parts = [settings.include_metallic, settings.include_alpha, settings.include_emission,
             settings.only_selected, bool(bpy.data.filepath)]
    for coll_name in [FINAL_COLL] + [layer.coll for layer in active_layers(settings)]:
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            parts.append((coll_name, None))
            continue
        for o in sorted(coll.all_objects, key=lambda o: o.name):
            entry = [coll_name, o.name, o.type, o.name in context.view_layer.objects]
            if o.type == 'MESH':
                me = o.data
                entry += [me.name, len(me.vertices), len(me.polygons), [u.name for u in me.uv_layers],
                          me.uv_layers.active.name if me.uv_layers.active else None,
                          o.auto_bake_share.name if o.auto_bake_share else None]
                if coll_name != FINAL_COLL:
                    entry.append([s.material.name if s.material else None for s in o.material_slots])
            parts.append(entry)
    if settings.only_selected:
        parts.append(sorted(o.name for o in context.selected_objects))
    return hashlib.md5(repr(parts).encode()).hexdigest()


def is_validated(context, settings):
    return bool(settings.validated_hash) and settings.validated_hash == fingerprint(context, settings)


class BakeJob:
    def __init__(self, final, sources, emits, sharers=()):
        self.final = final
        self.sources = sources  # layer key -> object
        self.emits = emits
        self.sharers = list(sharers)  # Final objects that get this job's material too


def _check_source(context, final, layer, coll, final_names, errors, warnings):
    obj = find_source(final, layer, coll)
    name = final.name + layer.suffix
    if obj is None:
        errors.append(f"{final.name}: no '{name}' object found")
        return None
    name = obj.name
    if obj.name not in coll.all_objects:
        errors.append(f"{name}: not in the '{coll.name}' collection")
        return None
    if obj.type != 'MESH':
        errors.append(f"{name}: is not a mesh")
        return None
    if obj.data == final.data:
        errors.append(f"{name}: shares mesh data with {final.name} (make it single-user)")
    if not obj.material_slots:
        errors.append(f"{name}: has no materials")
    for i, slot in enumerate(obj.material_slots):
        if slot.material is None:
            errors.append(f"{name}: material slot {i + 1} is empty")
        elif slot.material.name in final_names:
            errors.append(f"{name}: uses the Final material '{slot.material.name}' (would bake into itself)")

    if not obj.data.uv_layers:
        errors.append(f"{name}: has no UV map")
    elif final.data.uv_layers:
        missing, total = missing_uv_points(final, obj)
        if missing:
            warnings.append(f"{name}: {missing} of {total} UV points from {final.name} have no match "
                            f"- UVs differ, the bake may land in the wrong place")
    if len(obj.data.polygons) != len(final.data.polygons):
        warnings.append(f"{name}: face count differs from {final.name} "
                        f"({len(obj.data.polygons)} vs {len(final.data.polygons)})")
    return obj


def validate(context, settings):
    """Returns (errors, warnings, jobs)."""
    errors, warnings, jobs = [], [], []

    if not bpy.data.filepath:
        errors.append("Save the .blend file first (textures are saved next to it)")

    final_coll = bpy.data.collections.get(FINAL_COLL)
    if final_coll is None:
        errors.append(f"Missing '{FINAL_COLL}' collection")
    colls = {}
    for layer in active_layers(settings):
        colls[layer.key] = bpy.data.collections.get(layer.coll)
        if colls[layer.key] is None:
            hint = "" if layer is LAYER_MAT else f" (or untick {layer.coll})"
            errors.append(f"Missing '{layer.coll}' collection{hint}")
    if final_coll is None or colls[LAYER_MAT.key] is None:
        return errors, warnings, jobs

    all_finals = iter_mesh_objects(final_coll)
    if not all_finals:
        errors.append(f"'{FINAL_COLL}' collection has no mesh objects")
    final_names = {o.name for o in all_finals}
    final_set = set(all_finals)

    # Objects that borrow another Final object's textures instead of baking their own.
    sharers = {}
    for obj in all_finals:
        src = obj.auto_bake_share
        if src is None:
            continue
        if src not in final_set:
            errors.append(f"{obj.name}: shares textures with '{src.name}', which isn't a mesh in {FINAL_COLL}")
        elif src.auto_bake_share is not None:
            errors.append(f"{obj.name}: shares textures with {src.name}, which itself shares from "
                          f"{src.auto_bake_share.name} - point it at {src.auto_bake_share.name} instead")
        else:
            sharers.setdefault(src, []).append(obj)
            if obj.data.uv_layers and src.data.uv_layers:
                missing, total = missing_uv_points(obj, src)
                if missing:
                    warnings.append(f"{obj.name}: {missing} of {total} UV points have no match on {src.name} "
                                    f"- shared textures may not line up")
        if not obj.data.uv_layers:
            errors.append(f"{obj.name}: has no UV map")
        for layer in active_layers(settings):
            if colls[layer.key] is not None and find_source(obj, layer, colls[layer.key]) is not None:
                warnings.append(f"{obj.name}{layer.suffix}: ignored, {obj.name} shares textures with {src.name}")

    finals = [o for o in all_finals if o.auto_bake_share is None]
    if settings.only_selected:
        wanted = {base_name(o.name) for o in context.selected_objects}
        # A selected sharing object stands for the source it borrows from.
        wanted |= {o.auto_bake_share.name for o in all_finals if o.name in wanted and o.auto_bake_share}
        finals = [o for o in finals if o.name in wanted]
        if not finals:
            errors.append("Selected Only is on, but nothing selected maps to a Final object")

    for final in finals:
        if not final.data.uv_layers:
            errors.append(f"{final.name}: has no UV map")
        sources = {}
        for layer in active_layers(settings):
            if colls[layer.key] is not None:
                sources[layer.key] = _check_source(context, final, layer, colls[layer.key],
                                                   final_names, errors, warnings)
        mat_obj = sources.get(LAYER_MAT.key)
        jobs.append(BakeJob(final, sources, bool(mat_obj and object_emits(mat_obj)), sharers.get(final, [])))

    # Strays: objects in the source collections that don't map back to a Final object.
    lower_finals = {n.lower() for n in final_names}
    for layer in active_layers(settings):
        coll = colls[layer.key]
        if coll is None:
            continue
        for obj in iter_mesh_objects(coll):
            lower = obj.name.lower()
            if not lower.endswith(layer.suffix.lower()) or lower[: -len(layer.suffix)] not in lower_finals:
                warnings.append(f"{obj.name}: in '{coll.name}' but doesn't match a Final object (ignored)")

    if settings.include_emission and jobs and not any(j.emits for j in jobs):
        warnings.append("Emission is on, but no .Mat materials emit - no emission textures will be made")

    return errors, warnings, jobs


# ---------------------------------------------------------------------------
# Baking
# ---------------------------------------------------------------------------

def make_image(name, size, non_color, alpha=False):
    old = bpy.data.images.get(name)
    if old is not None:
        bpy.data.images.remove(old)
    img = bpy.data.images.new(name, size, size, alpha=alpha)
    img.generated_color = (0.0, 0.0, 0.0, 1.0)
    if alpha:
        # Alpha is a separate mask, not coverage: keep RGB intact where alpha is 0.
        img.alpha_mode = 'CHANNEL_PACKED'
    if non_color:
        img.colorspace_settings.name = 'Non-Color'
    return img


def pack_alpha(base, alpha):
    """Copy the baked alpha mask (red channel) into base's alpha channel."""
    count = len(base.pixels)
    base_px = array('f', bytes(4 * count))
    alpha_px = array('f', bytes(4 * count))
    base.pixels.foreach_get(base_px)
    alpha.pixels.foreach_get(alpha_px)
    base_px[3::4] = alpha_px[0::4]
    base.pixels.foreach_set(base_px)
    base.update()


def build_final_material(final, images, specs):
    mat = bpy.data.materials.get(final.name) or bpy.data.materials.new(final.name)
    nt = node_tree_of(mat)
    nt.nodes.clear()

    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (300, 0)
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    wired = [s for s in specs if s[2] is not None]
    for i, (suffix, _bake_type, socket, _non_color, _layer) in enumerate(wired):
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.name = tex.label = suffix.lstrip("_")
        tex.image = images[suffix]
        tex.location = (-400, 150 - i * 280)
        nt.links.new(tex.outputs["Color"], bsdf.inputs[socket])
        if suffix == "_Emission":
            bsdf.inputs["Emission Strength"].default_value = 1.0
        if suffix == "_BaseColor" and images[suffix].alpha_mode == 'CHANNEL_PACKED':
            # BaseColor alpha -> BSDF Alpha is what the glTF exporter recognizes.
            nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])

    final.data.materials.clear()
    final.data.materials.append(mat)
    return mat


def get_target_node(mat):
    """The unconnected Image Texture node that receives the bake."""
    nt = node_tree_of(mat)
    node = nt.nodes.get(TARGET_NODE)
    if node is None:
        for n in nt.nodes:
            if n.bl_idname == 'ShaderNodeTexImage' and not any(o.is_linked for o in n.outputs) \
                    and (n.image is None or n.image.name.endswith(KNOWN_TEX_SUFFIXES)):
                node = n
                break
    if node is None:
        node = nt.nodes.new('ShaderNodeTexImage')
        xs = [n.location.x for n in nt.nodes if n != node]
        ys = [n.location.y for n in nt.nodes if n != node]
        node.location = (min(xs, default=0) - 350, max(ys, default=0))
    node.name = TARGET_NODE
    node.label = "Bake Target"
    return node


class VisibilityGuard:
    """Temporarily makes objects visible and selectable for baking, then restores them."""

    def __init__(self, view_layer):
        self.view_layer = view_layer
        self.obj_state = {}
        self.lc_state = {}

    @staticmethod
    def _chain(lc, coll):
        if lc.collection == coll:
            return [lc]
        for child in lc.children:
            found = VisibilityGuard._chain(child, coll)
            if found:
                return [lc] + found
        return None

    def reveal(self, obj):
        # Collections first: an object in an excluded collection isn't in the view layer yet.
        for coll in obj.users_collection:
            for lc in (self._chain(self.view_layer.layer_collection, coll) or [])[1:]:
                if lc not in self.lc_state:
                    c = lc.collection
                    self.lc_state[lc] = (lc.exclude, lc.hide_viewport, c.hide_viewport, c.hide_render,
                                         c.hide_select)
                lc.exclude = False
                lc.hide_viewport = False
                lc.collection.hide_viewport = False
                lc.collection.hide_render = False
                lc.collection.hide_select = False
        if obj not in self.obj_state:
            self.obj_state[obj] = (obj.hide_get(), obj.hide_viewport, obj.hide_render, obj.hide_select)
        obj.hide_viewport = False
        obj.hide_render = False
        obj.hide_select = False
        obj.hide_set(False)

    def restore(self):
        # Objects first: hide_set needs the object to still be in the view layer.
        for obj, (hidden, hide_vp, hide_render, hide_select) in self.obj_state.items():
            obj.hide_viewport = hide_vp
            obj.hide_render = hide_render
            obj.hide_select = hide_select
            obj.hide_set(hidden)
        # Innermost collections first, so re-excluding a parent comes last.
        for lc, (excluded, lc_hide, c_hide, c_render, c_select) in reversed(list(self.lc_state.items())):
            lc.hide_viewport = lc_hide
            lc.collection.hide_viewport = c_hide
            lc.collection.hide_render = c_render
            lc.collection.hide_select = c_select
            lc.exclude = excluded


def bake_into(context, guard, obj, image, bake_type, settings):
    for slot in obj.material_slots:
        nt = node_tree_of(slot.material)
        node = get_target_node(slot.material)
        node.image = image
        for n in nt.nodes:
            n.select = False
        node.select = True
        nt.nodes.active = node

    guard.reveal(obj)
    for o in context.view_layer.objects:
        if o.select_get():
            o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj

    kwargs = dict(
        type=bake_type,
        margin=settings.margin,
        use_clear=True,
        use_selected_to_active=False,
        target='IMAGE_TEXTURES',
    )
    if bake_type != 'DIFFUSE':
        bpy.ops.object.bake(**kwargs)
        return

    # Cycles' diffuse color pass is the albedo left over after the Principled specular layer
    # (~4% darker than the authored Base Color). Zero the specular during the bake so the
    # texture holds the exact color; the game engine adds its own specular on top.
    kwargs["pass_filter"] = {'COLOR'}
    tweaked = []
    for mat in {s.material for s in obj.material_slots}:
        for n in mat.node_tree.nodes:
            spec = n.inputs.get("Specular IOR Level") if n.bl_idname == 'ShaderNodeBsdfPrincipled' else None
            if spec is not None and not spec.is_linked:
                tweaked.append((spec, spec.default_value))
                spec.default_value = 0.0
    try:
        bpy.ops.object.bake(**kwargs)
    finally:
        for spec, value in tweaked:
            spec.default_value = value


def save_image(image):
    os.makedirs(bpy.path.abspath(f"//{TEXTURE_DIR}"), exist_ok=True)
    image.filepath_raw = f"//{TEXTURE_DIR}/{image.name}.png"
    image.file_format = 'PNG'
    image.save()


def prepare_scene(scene):
    if scene.render.engine != 'CYCLES':
        scene.render.engine = 'CYCLES'
    bake = scene.render.bake
    bake.use_pass_direct = False
    bake.use_pass_indirect = False
    bake.use_pass_color = True
    bake.use_selected_to_active = False


def run_bake(context, settings, jobs, progress=None):
    """Bakes every job. Returns a list of saved texture names. Raises RuntimeError on bake failure."""
    scene = context.scene
    prepare_scene(scene)

    saved = []
    old_samples = scene.cycles.samples
    old_active = context.view_layer.objects.active
    old_selected = [o for o in context.view_layer.objects if o.select_get()]
    guard = VisibilityGuard(context.view_layer)
    scene.cycles.samples = settings.samples

    total = sum(len(texture_specs(settings, j.emits)) for j in jobs)
    step = 0
    try:
        for job in jobs:
            specs = texture_specs(settings, job.emits)
            packing = settings.include_alpha and settings.pack_alpha
            images = {s[0]: make_image(texture_name(job.final, s[0]), settings.resolution, s[3],
                                       alpha=packing and s[0] == "_BaseColor") for s in specs}
            mat = build_final_material(job.final, images, specs)
            for sharer in job.sharers:
                if sharer.data != job.final.data:
                    sharer.data.materials.clear()
                    sharer.data.materials.append(mat)

            for suffix, bake_type, _socket, _non_color, layer_key in specs:
                source = job.sources[layer_key]
                image = images[suffix]
                print(f"[Auto Bake] {image.name}  <-  {source.name} ({bake_type})")
                try:
                    bake_into(context, guard, source, image, bake_type, settings)
                except RuntimeError as e:
                    raise RuntimeError(f"{image.name} from {source.name}: {e}") from e
                step += 1
                if progress:
                    progress(step, total)

            if packing:
                pack_alpha(images["_BaseColor"], images["_Alpha"])
                bpy.data.images.remove(images.pop("_Alpha"))
            for image in images.values():
                save_image(image)
                saved.append(image.name)
    finally:
        scene.cycles.samples = old_samples
        guard.restore()
        for o in context.view_layer.objects:
            o.select_set(o in old_selected)
        context.view_layer.objects.active = old_active
    return saved


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def ensure_object_mode(context):
    if context.mode != 'OBJECT' and context.view_layer.objects.active is not None:
        bpy.ops.object.mode_set(mode='OBJECT')


class AUTOBAKE_OT_setup_collections(bpy.types.Operator):
    bl_idname = "autobake.setup_collections"
    bl_label = "Setup Collections"
    bl_description = "Create the Final and Materials collections, plus Metallic / Alpha if ticked"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.auto_bake
        names = [FINAL_COLL] + [layer.coll for layer in active_layers(settings)]
        created = []
        for name in names:
            coll = bpy.data.collections.get(name)
            if coll is None:
                coll = bpy.data.collections.new(name)
            if coll not in context.scene.collection.children_recursive:
                context.scene.collection.children.link(coll)
                created.append(name)
        self.report({'INFO'}, f"Created: {', '.join(created)}" if created else "Collections already set up")
        return {'FINISHED'}


class AUTOBAKE_OT_copy_final(bpy.types.Operator):
    bl_idname = "autobake.copy_final"
    bl_label = "Copy Final"
    bl_description = ("Copy every Final object into Materials (.Mat), Metallic (.Met) and Alpha (.Alpha) "
                      "with suffixes applied. Existing copies are left untouched")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.auto_bake
        ensure_object_mode(context)
        final_coll = bpy.data.collections.get(FINAL_COLL)
        if final_coll is None:
            self.report({'ERROR'}, f"No '{FINAL_COLL}' collection - run Setup Collections first")
            return {'CANCELLED'}

        layers = active_layers(settings)
        for layer in layers:
            if bpy.data.collections.get(layer.coll) is None:
                self.report({'ERROR'}, f"No '{layer.coll}' collection - run Setup Collections first")
                return {'CANCELLED'}

        # Parents before children, so a child's copy can be parented to its parent's copy.
        finals = sorted(iter_mesh_objects(final_coll), key=lambda o: (parent_depth(o), o.name))
        final_set = set(finals)
        final_names = {o.name for o in finals}
        made, skipped = 0, 0
        for final in finals:
            if final.auto_bake_share is not None:
                continue  # Borrows another object's textures, so it has nothing to bake from.
            for layer in layers:
                coll = bpy.data.collections[layer.coll]
                if find_source(final, layer, coll) is not None:
                    skipped += 1
                    continue
                name = final.name + layer.suffix
                copy = final.copy()
                copy.data = final.data.copy()
                copy.name = name
                copy.data.name = name + MESH_SUFFIX
                mirrored_collection(final_coll, final, layer).objects.link(copy)

                parent_copy = find_source(final.parent, layer, coll) if final.parent in final_set else None
                if parent_copy is not None:
                    # Same parent-inverse and local transform, so it sits on the parent copy
                    # exactly like the Final child sits on the Final parent (and inherits its spacing).
                    copy.parent = parent_copy
                else:
                    if final.parent is not None:
                        copy.parent = None
                        copy.matrix_world = final.matrix_world.copy()
                    # Spacing slot follows ALL_LAYERS so a layer always lands in the same place.
                    copy.location.x -= settings.copy_spacing * (ALL_LAYERS.index(layer) + 1)

                if layer.flat_mats:
                    copy.data.materials.clear()
                    for mat_name, color in layer.flat_mats:
                        copy.data.materials.append(ensure_flat_material(mat_name, color))
                    for poly in copy.data.polygons:
                        poly.material_index = 0
                elif any(m and m.name in final_names for m in copy.data.materials):
                    # Final already has its baked material; don't carry it into the source copy.
                    copy.data.materials.clear()
                made += 1

        self.report({'INFO'}, f"Made {made} copies, skipped {skipped} that already exist")
        return {'FINISHED'}


class AUTOBAKE_OT_validate(bpy.types.Operator):
    bl_idname = "autobake.validate"
    bl_label = "Validate"
    bl_description = "Check collections, naming and UVs. Baking is unlocked once this passes"

    def execute(self, context):
        settings = context.scene.auto_bake
        ensure_object_mode(context)
        errors, warnings, jobs = validate(context, settings)
        if errors:
            settings.validated_hash = ""
            show_popup(context, f"{len(errors)} problem(s)", errors + warnings, 'ERROR')
            self.report({'ERROR'}, f"{len(errors)} problem(s) - see popup / console")
            return {'CANCELLED'}
        settings.validated_hash = fingerprint(context, settings)
        lines = [f"{len(jobs)} object(s) ready to bake"] + warnings
        title = f"Passed with {len(warnings)} warning(s)" if warnings else "Validation passed"
        show_popup(context, title, lines, 'ERROR' if warnings else 'CHECKMARK')
        self.report({'WARNING'} if warnings else {'INFO'}, lines[0])
        return {'FINISHED'}


class AUTOBAKE_OT_bake(bpy.types.Operator):
    bl_idname = "autobake.bake"
    bl_label = "Bake Textures"
    bl_description = "Build the Final materials, then bake and save every texture"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = context.scene.auto_bake
        if not settings.validated_hash:
            cls.poll_message_set("Run Validate first")
            return False
        if settings.validated_hash != fingerprint(context, settings):
            cls.poll_message_set("Scene changed since validation - run Validate again")
            return False
        return True

    def execute(self, context):
        settings = context.scene.auto_bake
        ensure_object_mode(context)
        # The fingerprint can't see everything (e.g. UVs moved in place), so validate for real too.
        errors, warnings, jobs = validate(context, settings)
        if errors:
            settings.validated_hash = ""
            show_popup(context, f"Can't bake: {len(errors)} problem(s)", errors + warnings, 'ERROR')
            self.report({'ERROR'}, f"{len(errors)} problem(s) - see popup / console")
            return {'CANCELLED'}

        wm = context.window_manager
        wm.progress_begin(0, 1)
        try:
            saved = run_bake(context, settings, jobs, lambda i, n: wm.progress_update(i / n))
        except RuntimeError as e:
            show_popup(context, "Bake failed", [str(e)], 'ERROR')
            self.report({'ERROR'}, f"Bake failed: {e}")
            return {'CANCELLED'}
        finally:
            wm.progress_end()

        settings.validated_hash = fingerprint(context, settings)
        lines = [f"Saved {len(saved)} texture(s) to //{TEXTURE_DIR}/"] + warnings
        show_popup(context, "Bake complete", lines, 'CHECKMARK')
        self.report({'INFO'}, lines[0])
        return {'FINISHED'}


class AUTOBAKE_OT_export(bpy.types.Operator):
    bl_idname = "autobake.export_gltf"
    bl_label = "Batch Export glTF"
    bl_description = ("Export each top-level object in Final, together with its children, "
                      "as <Name>.glb into a chosen folder")

    directory: StringProperty(subtype='DIR_PATH')
    filter_folder: BoolProperty(default=True, options={'HIDDEN'})

    def invoke(self, context, _event):
        settings = context.scene.auto_bake
        self.directory = bpy.path.abspath(settings.export_dir) if settings.export_dir \
            else os.path.dirname(bpy.data.filepath)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        settings = context.scene.auto_bake
        ensure_object_mode(context)
        final_coll = bpy.data.collections.get(FINAL_COLL)
        if final_coll is None:
            self.report({'ERROR'}, f"No '{FINAL_COLL}' collection")
            return {'CANCELLED'}
        if not self.directory:
            self.report({'ERROR'}, "No export folder chosen")
            return {'CANCELLED'}
        os.makedirs(self.directory, exist_ok=True)
        settings.export_dir = self.directory

        groups = export_groups(final_coll)
        if settings.only_selected:
            wanted = {base_name(o.name) for o in context.selected_objects}
            groups = [(top, objs) for top, objs in groups if any(o.name in wanted for o in objs)]
        if not groups:
            self.report({'ERROR'}, "Nothing to export")
            return {'CANCELLED'}

        warnings = [f"{o.name}: has no material yet (not baked?)"
                    for _top, objs in groups for o in objs if o.type == 'MESH' and not o.data.materials]

        old_active = context.view_layer.objects.active
        old_selected = [o for o in context.view_layer.objects if o.select_get()]
        guard = VisibilityGuard(context.view_layer)
        exported = []
        try:
            for top, objs in groups:
                for o in objs:
                    guard.reveal(o)
                for o in context.view_layer.objects:
                    o.select_set(o in objs)
                context.view_layer.objects.active = top

                old_loc = top.location.copy()
                if settings.export_center:
                    top.location = (0.0, 0.0, 0.0)
                path = os.path.join(self.directory, f"{top.name}.glb")
                print(f"[Auto Bake] Export {path} ({len(objs)} object(s))")
                try:
                    bpy.ops.export_scene.gltf(
                        filepath=path,
                        export_format='GLB',
                        use_selection=True,
                        export_apply=True,
                        export_image_format='AUTO' if settings.export_textures else 'NONE',
                    )
                finally:
                    top.location = old_loc
                exported.append(f"{top.name}.glb")
        except RuntimeError as e:
            show_popup(context, "Export failed", [str(e)], 'ERROR')
            self.report({'ERROR'}, f"Export failed: {e}")
            return {'CANCELLED'}
        finally:
            guard.restore()
            for o in context.view_layer.objects:
                o.select_set(o in old_selected)
            context.view_layer.objects.active = old_active

        lines = [f"Exported {len(exported)} file(s) to {self.directory}"] + exported + warnings
        show_popup(context, "Export complete", lines, 'ERROR' if warnings else 'CHECKMARK')
        self.report({'WARNING'} if warnings else {'INFO'}, lines[0])
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Texture sharing
# ---------------------------------------------------------------------------

# Above this many UV loops the panel skips its live UV check (Validate still runs it).
LIVE_UV_CHECK_LIMIT = 20000


def is_final_mesh(obj):
    coll = bpy.data.collections.get(FINAL_COLL)
    return obj is not None and obj.type == 'MESH' and coll is not None and obj.name in coll.all_objects


def _share_poll(owner, candidate):
    return candidate != owner and is_final_mesh(candidate)


def sharers_of(obj):
    coll = bpy.data.collections.get(FINAL_COLL)
    if coll is None:
        return []
    return sorted((o for o in coll.all_objects if o.type == 'MESH' and o.auto_bake_share == obj),
                  key=lambda o: o.name)


class AUTOBAKE_OT_share_from_active(bpy.types.Operator):
    bl_idname = "autobake.share_from_active"
    bl_label = "Share Textures From Active"
    bl_description = ("Make the selected Final objects use the active object's textures instead of baking "
                      "their own (select the variants, then the source last)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        source = context.view_layer.objects.active
        if not is_final_mesh(source):
            self.report({'ERROR'}, f"The active object must be a mesh in '{FINAL_COLL}'")
            return {'CANCELLED'}
        if source.auto_bake_share is not None:
            self.report({'ERROR'}, f"{source.name} itself shares from {source.auto_bake_share.name} - "
                                   f"make that the active object instead")
            return {'CANCELLED'}
        targets = [o for o in context.selected_objects if o != source and is_final_mesh(o)]
        if not targets:
            self.report({'ERROR'}, "Select the objects that should share, then the source last (active)")
            return {'CANCELLED'}
        for obj in targets:
            obj.auto_bake_share = source
            # Anything that borrowed from obj now borrows from source, so there are no chains.
            for borrower in sharers_of(obj):
                borrower.auto_bake_share = source
        self.report({'INFO'}, f"{len(targets)} object(s) now share textures with {source.name}")
        return {'FINISHED'}


class AUTOBAKE_OT_clear_share(bpy.types.Operator):
    bl_idname = "autobake.clear_share"
    bl_label = "Clear Shared Textures"
    bl_description = "Selected Final objects go back to baking their own textures"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        cleared = 0
        for obj in context.selected_objects:
            if obj.type == 'MESH' and obj.auto_bake_share is not None:
                obj.auto_bake_share = None
                cleared += 1
        self.report({'INFO'}, f"Cleared sharing on {cleared} object(s)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

HELP = (
    ("1. Set up", 'OUTLINER_COLLECTION', (
        "Tick what you need under Include, then Setup Collections.",
        "Model in Final, then Copy Final to make the .Mat / .Met / .Alpha copies.",
        "Nested collections and parenting are mirrored into the copies.",
    )),
    ("2. Paint the copies", 'MATERIAL', (
        "Materials (.Mat): any node materials - color, roughness, emission.",
        "Metallic (.Met): assign Metallic (white) / NonMetallic (black) to faces.",
        "Alpha (.Alpha): Opaque (white) / Transparent (black), grey = partial.",
        "Extra geometry is fine, but keep the Final object's UVs in every copy.",
    )),
    ("3. Bake", 'RENDER_STILL', (
        "Validate, then Bake Textures (unlocked while nothing has changed).",
        "Textures save to //Textures/<Name>_<Type>.png next to the .blend.",
        "Each Final object gets one material with the textures wired in.",
        "Selected Only re-bakes just the selected objects.",
    )),
    ("Variants that share textures", 'LINKED', (
        "Select the variant(s), then the source last, and click Share From Active.",
        "The variant uses the source's textures and needs no copies of its own.",
    )),
    ("4. Export", 'EXPORT', (
        "Batch Export glTF writes one .glb per top-level Final object,",
        "including its children, into the folder you pick.",
    )),
)


class AUTOBAKE_OT_help(bpy.types.Operator):
    bl_idname = "autobake.help"
    bl_label = "Auto Bake Help"
    bl_description = "Show a short how-to for the Auto Bake workflow"

    def invoke(self, context, _event):
        return context.window_manager.invoke_popup(self, width=560)

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Auto Bake - how to", icon='QUESTION')
        for heading, icon, lines in HELP:
            box = layout.box()
            col = box.column(align=True)
            col.label(text=heading, icon=icon)
            for line in lines:
                col.label(text=line)
        layout.label(text="Problems and progress are also printed to the system console.", icon='CONSOLE')

    def execute(self, _context):
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class AUTOBAKE_PT_panel(bpy.types.Panel):
    bl_label = "Auto Bake"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Auto Bake"

    def draw_header_preset(self, _context):
        self.layout.operator("autobake.help", text="", icon='QUESTION', emboss=False)

    def draw(self, context):
        settings = context.scene.auto_bake
        layout = self.layout

        col = layout.column(heading="Include")
        col.prop(settings, "include_metallic")
        col.prop(settings, "include_alpha")
        sub = col.row()
        sub.separator(factor=2.0)
        sub.active = settings.include_alpha
        sub.prop(settings, "pack_alpha")
        col.prop(settings, "include_emission")

        layout.separator()
        col = layout.column(align=True)
        col.operator("autobake.setup_collections", icon='OUTLINER_COLLECTION')
        col.operator("autobake.copy_final", icon='DUPLICATE')
        layout.prop(settings, "copy_spacing")

        layout.separator()
        col = layout.column(align=True)
        col.prop(settings, "resolution")
        col.prop(settings, "samples")
        col.prop(settings, "margin")
        layout.prop(settings, "only_selected")

        layout.separator()
        if is_validated(context, settings):
            layout.label(text="Validated", icon='CHECKMARK')
        elif settings.validated_hash:
            layout.label(text="Changed since validation", icon='ERROR')
        else:
            layout.label(text="Validate before baking", icon='INFO')
        layout.operator("autobake.validate", icon='CHECKMARK')
        row = layout.row()
        row.scale_y = 1.5
        row.operator("autobake.bake", icon='RENDER_STILL')

        layout.separator()
        col = layout.column(align=True)
        col.prop(settings, "export_center")
        col.prop(settings, "export_textures")
        layout.operator("autobake.export_gltf", icon='EXPORT')


class AUTOBAKE_PT_active_object(bpy.types.Panel):
    bl_label = "Active Object"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Auto Bake"
    bl_parent_id = "AUTOBAKE_PT_panel"

    @classmethod
    def poll(cls, context):
        return is_final_mesh(context.view_layer.objects.active)

    def draw_header(self, context):
        self.layout.label(text=f": {context.view_layer.objects.active.name}")

    def draw(self, context):
        obj = context.view_layer.objects.active
        layout = self.layout
        layout.prop(obj, "auto_bake_share", text="Shares Textures With")

        src = obj.auto_bake_share
        if src is not None:
            if src.auto_bake_share is not None:
                layout.label(text=f"{src.name} shares too - pick {src.auto_bake_share.name}", icon='ERROR')
            elif not (obj.data.uv_layers and src.data.uv_layers):
                layout.label(text="Missing a UV map", icon='ERROR')
            elif len(obj.data.loops) + len(src.data.loops) > LIVE_UV_CHECK_LIMIT:
                layout.label(text="UVs are checked on Validate", icon='INFO')
            else:
                missing, total = missing_uv_points(obj, src)
                if missing:
                    layout.label(text=f"{missing} of {total} UV points don't match {src.name}", icon='ERROR')
                else:
                    layout.label(text=f"UVs match {src.name}", icon='CHECKMARK')

        borrowers = sharers_of(obj)
        if borrowers:
            col = layout.column(align=True)
            col.label(text="Textures shared by:", icon='LINKED')
            for b in borrowers:
                col.label(text=f"    {b.name}")

        row = layout.row(align=True)
        row.operator("autobake.share_from_active", text="Share From Active", icon='LINKED')
        row.operator("autobake.clear_share", text="", icon='X')


classes = (
    AutoBakeSettings,
    AUTOBAKE_OT_setup_collections,
    AUTOBAKE_OT_copy_final,
    AUTOBAKE_OT_validate,
    AUTOBAKE_OT_bake,
    AUTOBAKE_OT_export,
    AUTOBAKE_OT_share_from_active,
    AUTOBAKE_OT_clear_share,
    AUTOBAKE_OT_help,
    AUTOBAKE_PT_panel,
    AUTOBAKE_PT_active_object,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.auto_bake = PointerProperty(type=AutoBakeSettings)
    bpy.types.Object.auto_bake_share = PointerProperty(
        type=bpy.types.Object,
        name="Shares Textures With",
        description="Use this Final object's baked material and textures instead of baking its own. "
                    "Needs no .Mat/.Met/.Alpha copies; UVs should match the source",
        poll=_share_poll,
    )


def unregister():
    del bpy.types.Object.auto_bake_share
    del bpy.types.Scene.auto_bake
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
