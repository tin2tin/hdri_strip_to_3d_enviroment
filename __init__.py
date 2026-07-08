bl_info = {
    "name": "VSE to 3D Environment",
    "author": "tintwotin",
    "version": (1, 12),
    "blender": (3, 0, 0),
    "location": "Sequencer > Strip > Convert to 3D",
    "description": "Converts strip to 3D Environment or Textured Dome with Shadows (Plays video automatically)",
    "category": "Sequencer",
}

import bpy
import bmesh
import math

# --- Constants for Object Names ---
NAME_DOME_SHELL = "VSE_Dome_Shell"
NAME_DOME_FLOOR = "VSE_Dome_Floor"
NAME_ENV_CATCHER = "VSE_Shadow_Catcher"
NAME_SUN = "VSE_Sun"

DOME_RADIUS = 20.0

def get_sequencer_scene(context):
    """Returns the scene edited in the sequencer (Blender 5.x), falling back to the window scene."""
    return getattr(context, "sequencer_scene", None) or context.scene

def _iter_strips(context):
    """Yields the active strip first, then any selected strips (deduplicated)."""
    scene = get_sequencer_scene(context)
    se = getattr(scene, "sequence_editor", None)
    if se is None:
        return

    seen = set()
    active = getattr(se, "active_strip", None)
    if active is not None:
        seen.add(active.as_pointer())
        yield active

    for strip in getattr(se, "strips", getattr(se, "sequences", [])):
        if strip.select and strip.as_pointer() not in seen:
            seen.add(strip.as_pointer())
            yield strip


def get_active_strip_and_path(context):
    """Gets a usable strip path and the strip object itself.

    Returns (filepath, strip) on success, or (None, reason_string) so the
    caller can report exactly why nothing usable was found.
    """
    scene = get_sequencer_scene(context)
    se = getattr(scene, "sequence_editor", None)
    if se is None:
        return None, "No sequence editor: add a strip in the Video Sequencer first."

    strips = list(_iter_strips(context))
    if not strips:
        return None, "No active or selected strip. Click a strip to select it."

    other_types = []
    for strip in strips:
        if strip.type == 'MOVIE':
            path = strip.filepath
        elif strip.type == 'IMAGE':
            if not strip.elements:
                continue
            path = strip.directory + strip.elements[0].filename
        else:
            other_types.append(strip.type)
            continue

        return bpy.path.abspath(path), strip

    found = ", ".join(sorted(set(other_types))) or "unknown"
    return None, f"Selected strip is not a Movie or Image strip (found: {found})."

def apply_image_to_node(node, filepath, strip):
    """Loads image or video and syncs it with the VSE strip timing to enable playback."""
    try:
        img = bpy.data.images.load(filepath)
        is_video = False
        
        if strip.type == 'MOVIE':
            img.source = 'MOVIE'
            is_video = True
        elif strip.type == 'IMAGE' and len(strip.elements) > 1:
            img.source = 'SEQUENCE'
            is_video = True
            
        node.image = img
        
        # Setup Video Playback Properties
        if is_video and hasattr(node, 'image_user'):
            node.image_user.use_auto_refresh = True # <--- This enables video playback
            
            # Match strip's timing if possible
            if hasattr(strip, "frame_final_duration"):
                node.image_user.frame_duration = strip.frame_final_duration
            else:
                node.image_user.frame_duration = 1048574
                
            if hasattr(strip, "frame_start"):
                node.image_user.frame_start = strip.frame_start
                
            if hasattr(strip, "frame_offset_start"):
                node.image_user.frame_offset = strip.frame_offset_start
    except Exception as e:
        print(f"Error loading image/video: {e}")

def setup_cycles():
    bpy.context.scene.render.engine = 'CYCLES'

def delete_existing_object(name):
    """Checks if an object exists by name and deletes it."""
    if name in bpy.data.objects:
        obj = bpy.data.objects[name]
        bpy.data.objects.remove(obj, do_unlink=True)

def redistribute_floor_geometry(obj):
    """
    Flattens the hemisphere to z=0 and redistributes the vertex rings
    (Sine distribution) to a Linear distribution (ArcSin).

    Flattening is done per-vertex instead of via object scale: a zero scale
    makes the object matrix singular, which breaks Object texture
    coordinates in Cycles (the floor renders black).
    """
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh = obj.data

    for v in mesh.vertices:
        v.co.z = 0.0
        # Get current radius (0.0 to 1.0)
        x, y = v.co.x, v.co.y
        current_r = math.sqrt(x*x + y*y)
        
        # Only process if not center and within bounds
        if current_r > 0.0001:
            # Clamp to 1.0 to avoid math domain errors
            curr_r_clamped = min(current_r, 1.0)
            
            # Map Sine distribution to Linear distribution
            angle = math.asin(curr_r_clamped)
            new_r = angle / (math.pi / 2.0)
            
            scale_factor = new_r / current_r
            v.co.x *= scale_factor
            v.co.y *= scale_factor

def create_polar_shader(obj, image_path, strip, horizon_height=0.0, shell_mat=None):
    """
    Creates a material that maps the HDRI floor using Object Coordinates
    (Polar conversion), matching the dome shell's equirectangular mapping
    (u = 0.5 - atan2(y, x) / 2*pi in Cycles).
    """
    obj.data.materials.clear()

    mat = bpy.data.materials.new(name="VSE_Dome_Mat_Floor")
    mat.use_nodes = True
    obj.data.materials.append(mat)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # --- OUTPUT ---
    node_out = nodes.new('ShaderNodeOutputMaterial')
    node_out.location = (800, 0)

    node_mix = nodes.new('ShaderNodeMixShader')
    node_mix.location = (600, 0)
    node_mix.inputs['Fac'].default_value = 0.4 # Mix Shadows

    node_emit = nodes.new('ShaderNodeEmission')
    node_emit.location = (400, 150)

    node_diff = nodes.new('ShaderNodeBsdfDiffuse')
    node_diff.location = (400, -150)

    # --- IMAGE ---
    node_tex = nodes.new('ShaderNodeTexImage')
    node_tex.location = (200, 0)
    apply_image_to_node(node_tex, image_path, strip)
    node_tex.extension = 'CLIP'
    node_tex.interpolation = 'Linear'

    # --- POLAR MATH (Object Coords) ---
    node_coord = nodes.new('ShaderNodeTexCoord')
    node_coord.location = (-1400, 0)

    node_sep = nodes.new('ShaderNodeSeparateXYZ')
    node_sep.location = (-1000, 0)

    # 1. ANGLE (U Coordinate)
    node_atan = nodes.new('ShaderNodeMath')
    node_atan.operation = 'ARCTAN2'
    node_atan.location = (-800, 150)

    # Map [-pi, pi] to [1, 0]: the flipped output range mirrors the angle so
    # the floor matches the shell's environment mapping instead of being
    # mirrored on X and rotated 90 degrees against it.
    node_range_u = nodes.new('ShaderNodeMapRange')
    node_range_u.location = (-600, 150)
    node_range_u.inputs[1].default_value = -math.pi
    node_range_u.inputs[2].default_value = math.pi
    node_range_u.inputs[3].default_value = 1.0
    node_range_u.inputs[4].default_value = 0.0

    # 2. RADIUS (V Coordinate)
    node_len = nodes.new('ShaderNodeVectorMath')
    node_len.operation = 'LENGTH'
    node_len.location = (-800, -150)

    node_mult_v = nodes.new('ShaderNodeMath')
    node_mult_v.operation = 'MULTIPLY'
    node_mult_v.location = (-600, -150)

    # --- HORIZON HEIGHT (follows the shell material via a driver) ---
    # The floor's outer edge must sample the same image row as the dome
    # shell's equator, which the horizon offset moves away from v=0.5:
    # v_edge = 0.5 - atan(height / radius) / pi
    node_height = nodes.new('ShaderNodeValue')
    node_height.name = node_height.label = "Horizon Height Link"
    node_height.location = (-1400, -300)
    node_height.outputs[0].default_value = horizon_height

    node_h_div = nodes.new('ShaderNodeMath')
    node_h_div.operation = 'DIVIDE'
    node_h_div.location = (-1200, -300)
    node_h_div.inputs[1].default_value = DOME_RADIUS

    node_h_atan = nodes.new('ShaderNodeMath')
    node_h_atan.operation = 'ARCTANGENT'
    node_h_atan.location = (-1000, -300)

    node_h_div_pi = nodes.new('ShaderNodeMath')
    node_h_div_pi.operation = 'DIVIDE'
    node_h_div_pi.location = (-800, -300)
    node_h_div_pi.inputs[1].default_value = math.pi

    node_h_sub = nodes.new('ShaderNodeMath')
    node_h_sub.operation = 'SUBTRACT'
    node_h_sub.location = (-600, -300)
    node_h_sub.inputs[0].default_value = 0.5

    if shell_mat is not None:
        # Single source of truth: the shell material's "Horizon Height" node.
        fcurve = mat.node_tree.driver_add(
            'nodes["Horizon Height Link"].outputs[0].default_value')
        driver = fcurve.driver
        driver.type = 'SUM'
        var = driver.variables.new()
        var.name = "height"
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'MATERIAL'
        var.targets[0].id = shell_mat
        var.targets[0].data_path = 'node_tree.nodes["Horizon Height"].outputs[0].default_value'

    # Combine
    node_comb = nodes.new('ShaderNodeCombineXYZ')
    node_comb.location = (-400, 0)

    # --- LINKS ---
    links.new(node_coord.outputs['Object'], node_sep.inputs['Vector'])

    # U Path
    links.new(node_sep.outputs['Y'], node_atan.inputs[0])
    links.new(node_sep.outputs['X'], node_atan.inputs[1])
    links.new(node_atan.outputs['Value'], node_range_u.inputs[0])
    links.new(node_range_u.outputs['Result'], node_comb.inputs['X'])

    # V Path
    links.new(node_coord.outputs['Object'], node_len.inputs[0])
    links.new(node_len.outputs['Value'], node_mult_v.inputs[0])
    links.new(node_height.outputs[0], node_h_div.inputs[0])
    links.new(node_h_div.outputs['Value'], node_h_atan.inputs[0])
    links.new(node_h_atan.outputs['Value'], node_h_div_pi.inputs[0])
    links.new(node_h_div_pi.outputs['Value'], node_h_sub.inputs[1])
    links.new(node_h_sub.outputs['Value'], node_mult_v.inputs[1])
    links.new(node_mult_v.outputs['Value'], node_comb.inputs['Y'])

    # Texture
    links.new(node_comb.outputs['Vector'], node_tex.inputs['Vector'])
    links.new(node_tex.outputs['Color'], node_emit.inputs['Color'])
    links.new(node_tex.outputs['Color'], node_diff.inputs['Color'])

    # Material
    links.new(node_emit.outputs['Emission'], node_mix.inputs[1])
    links.new(node_diff.outputs['BSDF'], node_mix.inputs[2])
    links.new(node_mix.outputs['Shader'], node_out.inputs['Surface'])


def create_dome_shell_mat(obj, image_path, strip, horizon_height=0.0):
    obj.data.materials.clear()
    mat = bpy.data.materials.new(name="VSE_Dome_Mat_Shell")
    mat.use_nodes = True
    obj.data.materials.append(mat)
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    tex = nodes.new('ShaderNodeTexEnvironment')
    tex.location = (-200, 0)
    apply_image_to_node(tex, image_path, strip)

    coord = nodes.new('ShaderNodeTexCoord')
    coord.location = (-800, 0)

    # Shifting the lookup direction down moves the horizon line up (meters).
    # Object coordinates are pre-scale, so convert meters to dome units.
    height = nodes.new('ShaderNodeValue')
    height.name = height.label = "Horizon Height"
    height.location = (-800, -250)
    height.outputs[0].default_value = horizon_height

    div = nodes.new('ShaderNodeMath')
    div.operation = 'DIVIDE'
    div.location = (-600, -250)
    div.inputs[1].default_value = DOME_RADIUS

    comb = nodes.new('ShaderNodeCombineXYZ')
    comb.location = (-450, -250)

    sub = nodes.new('ShaderNodeVectorMath')
    sub.operation = 'SUBTRACT'
    sub.location = (-400, 0)

    emit = nodes.new('ShaderNodeEmission')
    emit.location = (100, 0)
    out = nodes.new('ShaderNodeOutputMaterial')
    out.location = (300, 0)

    links.new(coord.outputs['Object'], sub.inputs[0])
    links.new(height.outputs[0], div.inputs[0])
    links.new(div.outputs['Value'], comb.inputs['Z'])
    links.new(comb.outputs['Vector'], sub.inputs[1])
    links.new(sub.outputs['Vector'], tex.inputs['Vector'])
    links.new(tex.outputs['Color'], emit.inputs['Color'])
    links.new(emit.outputs['Emission'], out.inputs['Surface'])
    return mat


class VSE_OT_ConvertToEnvironment(bpy.types.Operator):
    bl_idname = "vse.convert_to_environment"
    bl_label = "Environment"
    bl_options = {'REGISTER', 'UNDO'}

    horizon_height: bpy.props.FloatProperty(
        name="Horizon Height",
        description="Shift the texture's horizon line up or down so it matches the shadow catcher floor",
        default=0.0,
        soft_min=-1.0, soft_max=1.0,
        step=1, precision=3,
    )

    def execute(self, context):
        filepath, strip = get_active_strip_and_path(context)
        if not filepath:
            self.report({'ERROR'}, strip or "Please select a Movie or Image strip.")
            return {'CANCELLED'}
        setup_cycles()

        # Cleanup
        delete_existing_object(NAME_ENV_CATCHER)

        # World
        world = bpy.context.scene.world or bpy.data.worlds.new("VSE_World")
        bpy.context.scene.world = world
        world.use_nodes = True
        nodes = world.node_tree.nodes
        links = world.node_tree.links
        nodes.clear()

        tex = nodes.new('ShaderNodeTexEnvironment')
        tex.location = (-200, 0)
        apply_image_to_node(tex, filepath, strip)

        # Shifting the lookup direction down moves the horizon line up.
        coord = nodes.new('ShaderNodeTexCoord')
        coord.location = (-800, 0)

        height = nodes.new('ShaderNodeValue')
        height.name = height.label = "Horizon Height"
        height.location = (-800, -250)
        height.outputs[0].default_value = self.horizon_height

        comb = nodes.new('ShaderNodeCombineXYZ')
        comb.location = (-600, -250)

        sub = nodes.new('ShaderNodeVectorMath')
        sub.operation = 'SUBTRACT'
        sub.location = (-400, 0)

        bg = nodes.new('ShaderNodeBackground')
        bg.location = (100, 0)
        out = nodes.new('ShaderNodeOutputWorld')
        out.location = (300, 0)
        links.new(coord.outputs['Generated'], sub.inputs[0])
        links.new(height.outputs[0], comb.inputs['Z'])
        links.new(comb.outputs['Vector'], sub.inputs[1])
        links.new(sub.outputs['Vector'], tex.inputs['Vector'])
        links.new(tex.outputs['Color'], bg.inputs['Color'])
        links.new(bg.outputs['Background'], out.inputs['Surface'])

        # Create Plane
        bpy.ops.mesh.primitive_plane_add(size=100)
        plane = context.active_object
        plane.name = NAME_ENV_CATCHER
        plane.is_shadow_catcher = True
        return {'FINISHED'}

class VSE_OT_ConvertToHalfDome(bpy.types.Operator):
    bl_idname = "vse.convert_to_halfdome"
    bl_label = "Half Dome"
    bl_options = {'REGISTER', 'UNDO'}

    horizon_height: bpy.props.FloatProperty(
        name="Horizon Height",
        description="Shift the texture's horizon line up or down so it matches the dome floor",
        default=0.0,
        subtype='DISTANCE',
        soft_min=-DOME_RADIUS / 2, soft_max=DOME_RADIUS / 2,
    )

    def execute(self, context):
        filepath, strip = get_active_strip_and_path(context)
        if not filepath:
            self.report({'ERROR'}, strip or "Please select a Movie or Image strip.")
            return {'CANCELLED'}

        setup_cycles()
        
        # Cleanup Existing
        delete_existing_object(NAME_DOME_SHELL)
        delete_existing_object(NAME_DOME_FLOOR)
        delete_existing_object(NAME_SUN)

        # Keep the shadow catcher, but lift it a hair above the dome floor:
        # coplanar at z=0 they z-fight (white broken polygons). The catcher
        # only renders shadows, so the offset is invisible.
        catcher = bpy.data.objects.get(NAME_ENV_CATCHER)
        if catcher and abs(catcher.location.z) < 0.001:
            catcher.location.z = 0.01

        # Environment Darkening
        if not bpy.context.scene.world: bpy.context.scene.world = bpy.data.worlds.new("Dark")
        bpy.context.scene.world.use_nodes = True
        bg = bpy.context.scene.world.node_tree.nodes.get('Background')
        if bg: bg.inputs[1].default_value = 0.0

        # Geometry
        bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1)
        dome = context.active_object
        dome.name = NAME_DOME_SHELL # Temporary name until separation
        dome.scale = (DOME_RADIUS, DOME_RADIUS, DOME_RADIUS)
        bpy.ops.object.shade_smooth()
        
        # Separate Floor
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bm = bmesh.from_edit_mesh(dome.data)
        # Select bottom half
        for f in bm.faces:
            if f.calc_center_median().z <= 0.001: f.select = True
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Handle Objects after separation
        objects = context.selected_objects
        
        floor = None
        for obj in objects:
            if obj != dome:
                floor = obj
                break
        
        # Rename strictly
        dome.name = NAME_DOME_SHELL

        # The dome is viewed from the inside: flip the shell's outward-facing
        # normals inward so front faces point at the camera.
        dome.data.flip_normals()

        shell_mat = create_dome_shell_mat(dome, filepath, strip, self.horizon_height)
        dome.visible_shadow = False

        if floor:
            floor.name = NAME_DOME_FLOOR

            # Flatten + Fix Floor Rings (Linearize). Flattening happens on the
            # vertices, not object scale: scale.z=0 breaks Object texture
            # coordinates in Cycles (floor rendered black).
            redistribute_floor_geometry(floor)

            # Bottom-hemisphere normals point down; flip them up so the floor
            # is lit and shadowed from above.
            floor.data.flip_normals()

            # Apply Materials (horizon height follows the shell material)
            create_polar_shader(floor, filepath, strip, self.horizon_height, shell_mat)
            floor.visible_shadow = False
        
        # Add Sun
        bpy.ops.object.light_add(type='SUN', location=(0, 0, 10))
        sun = context.active_object
        sun.name = NAME_SUN
        sun.data.energy = 3.0
        sun.rotation_euler = (math.radians(45), math.radians(15), 0)
        
        self.report({'INFO'}, "Half Dome Setup Complete")
        return {'FINISHED'}

class VSE_MT_ConvertTo3DMenu(bpy.types.Menu):
    bl_label = "Convert to 3D"
    bl_idname = "VSE_MT_convert_to_3d"
    def draw(self, context):
        self.layout.operator("vse.convert_to_environment")
        self.layout.operator("vse.convert_to_halfdome")

def menu_func(self, context): self.layout.menu("VSE_MT_convert_to_3d")

# --- Surface panel integration ---

def _horizon_node(id_block):
    """Returns the "Horizon Height" Value node of a world/material, if any."""
    if id_block and getattr(id_block, "use_nodes", False) and id_block.node_tree:
        node = id_block.node_tree.nodes.get("Horizon Height")
        if node and node.type == 'VALUE':
            return node
    return None

def draw_world_horizon(self, context):
    """Appended to the World > Surface panel (worlds set up by this add-on)."""
    node = _horizon_node(getattr(context, "world", None))
    if node:
        self.layout.prop(node.outputs[0], "default_value", text="Horizon Height")

def draw_material_horizon(self, context):
    """Appended to the Material > Surface panel (dome shell material)."""
    node = _horizon_node(getattr(context, "material", None))
    if node:
        self.layout.prop(node.outputs[0], "default_value", text="Horizon Height")

_PANEL_DRAWS = (
    ("CYCLES_WORLD_PT_surface", draw_world_horizon),
    ("CYCLES_MATERIAL_PT_surface", draw_material_horizon),
    ("EEVEE_WORLD_PT_surface", draw_world_horizon),
    ("EEVEE_MATERIAL_PT_surface", draw_material_horizon),
)
_appended_panels = []

classes = (VSE_OT_ConvertToEnvironment, VSE_OT_ConvertToHalfDome, VSE_MT_ConvertTo3DMenu)

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.SEQUENCER_MT_strip.append(menu_func)
    for panel_name, draw_fn in _PANEL_DRAWS:
        panel = getattr(bpy.types, panel_name, None)
        if panel is not None:
            panel.append(draw_fn)
            _appended_panels.append((panel, draw_fn))

def unregister():
    for panel, draw_fn in _appended_panels:
        try:
            panel.remove(draw_fn)
        except Exception:
            pass
    _appended_panels.clear()
    bpy.types.SEQUENCER_MT_strip.remove(menu_func)
    for cls in reversed(classes): bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
