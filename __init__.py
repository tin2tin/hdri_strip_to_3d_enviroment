bl_info = {
    "name": "VSE to 3D Environment",
    "author": "tintwotin",
    "version": (1, 9),
    "blender": (3, 0, 0),
    "location": "Sequencer > Strip > Convert to 3D",
    "description": "Converts strip to 3D Environment or Textured Dome with Shadows (Replaces existing)",
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

def get_strip_path(context):
    scene = context.scene
    if not scene.sequence_editor or not scene.sequence_editor.active_strip:
        return None
    
    strip = scene.sequence_editor.active_strip
    
    if strip.type == 'MOVIE':
        path = strip.filepath
    elif strip.type == 'IMAGE':
        path = strip.directory + strip.elements[0].filename
    else:
        return None
        
    return bpy.path.abspath(path)

def setup_cycles():
    bpy.context.scene.render.engine = 'CYCLES'

def delete_existing_object(name):
    """Checks if an object exists by name and deletes it."""
    if name in bpy.data.objects:
        obj = bpy.data.objects[name]
        bpy.data.objects.remove(obj, do_unlink=True)

def redistribute_floor_geometry(obj):
    """
    Redistributes the vertex rings of a flattened sphere (Sine distribution)
    to a Linear distribution (ArcSin). 
    """
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh = obj.data
    
    for v in mesh.vertices:
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

def create_polar_shader(obj, image_path):
    """
    Creates a material that maps the HDRI floor using Object Coordinates (Polar conversion).
    """
    # clear old material if exists
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
    try:
        node_tex.image = bpy.data.images.load(image_path)
        node_tex.extension = 'CLIP'
        node_tex.interpolation = 'Linear'
    except:
        pass

    # --- POLAR MATH (Object Coords) ---
    node_coord = nodes.new('ShaderNodeTexCoord')
    node_coord.location = (-1400, 0)
    
    # Rotation Correction (-90 deg Z)
    node_map_rot = nodes.new('ShaderNodeMapping')
    node_map_rot.location = (-1200, 0)
    node_map_rot.inputs['Rotation'].default_value[2] = math.radians(-90) 
    
    node_sep = nodes.new('ShaderNodeSeparateXYZ')
    node_sep.location = (-1000, 0)
    
    # 1. ANGLE (U Coordinate)
    node_atan = nodes.new('ShaderNodeMath')
    node_atan.operation = 'ARCTAN2'
    node_atan.location = (-800, 150)
    
    node_range_u = nodes.new('ShaderNodeMapRange')
    node_range_u.location = (-600, 150)
    node_range_u.inputs[1].default_value = -math.pi
    node_range_u.inputs[2].default_value = math.pi
    node_range_u.inputs[3].default_value = 0.0
    node_range_u.inputs[4].default_value = 1.0
    
    # 2. RADIUS (V Coordinate)
    node_len = nodes.new('ShaderNodeVectorMath')
    node_len.operation = 'LENGTH'
    node_len.location = (-800, -150)
    
    node_mult_v = nodes.new('ShaderNodeMath')
    node_mult_v.operation = 'MULTIPLY'
    node_mult_v.location = (-600, -150)
    node_mult_v.inputs[1].default_value = 0.5
    
    # Combine
    node_comb = nodes.new('ShaderNodeCombineXYZ')
    node_comb.location = (-400, 0)
    
    # --- LINKS ---
    links.new(node_coord.outputs['Object'], node_map_rot.inputs['Vector'])
    links.new(node_map_rot.outputs['Vector'], node_sep.inputs['Vector'])
    
    # U Path
    links.new(node_sep.outputs['Y'], node_atan.inputs[0])
    links.new(node_sep.outputs['X'], node_atan.inputs[1])
    links.new(node_atan.outputs['Value'], node_range_u.inputs[0])
    links.new(node_range_u.outputs['Result'], node_comb.inputs['X'])
    
    # V Path
    links.new(node_map_rot.outputs['Vector'], node_len.inputs[0])
    links.new(node_len.outputs['Value'], node_mult_v.inputs[0])
    links.new(node_mult_v.outputs['Value'], node_comb.inputs['Y'])
    
    # Texture
    links.new(node_comb.outputs['Vector'], node_tex.inputs['Vector'])
    links.new(node_tex.outputs['Color'], node_emit.inputs['Color'])
    links.new(node_tex.outputs['Color'], node_diff.inputs['Color'])
    
    # Material
    links.new(node_emit.outputs['Emission'], node_mix.inputs[1])
    links.new(node_diff.outputs['BSDF'], node_mix.inputs[2])
    links.new(node_mix.outputs['Shader'], node_out.inputs['Surface'])


def create_dome_shell_mat(obj, image_path):
    obj.data.materials.clear()
    mat = bpy.data.materials.new(name="VSE_Dome_Mat_Shell")
    mat.use_nodes = True
    obj.data.materials.append(mat)
    nodes = mat.node_tree.nodes
    nodes.clear()
    
    tex = nodes.new('ShaderNodeTexEnvironment')
    try: tex.image = bpy.data.images.load(image_path)
    except: pass
    
    coord = nodes.new('ShaderNodeTexCoord')
    emit = nodes.new('ShaderNodeEmission')
    out = nodes.new('ShaderNodeOutputMaterial')
    
    mat.node_tree.links.new(coord.outputs['Object'], tex.inputs['Vector'])
    mat.node_tree.links.new(tex.outputs['Color'], emit.inputs['Color'])
    mat.node_tree.links.new(emit.outputs['Emission'], out.inputs['Surface'])


class VSE_OT_ConvertToEnvironment(bpy.types.Operator):
    bl_idname = "vse.convert_to_environment"
    bl_label = "Environment"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        filepath = get_strip_path(context)
        if not filepath: return {'CANCELLED'}
        setup_cycles()
        
        # Cleanup
        delete_existing_object(NAME_ENV_CATCHER)

        # World
        world = bpy.context.scene.world or bpy.data.worlds.new("VSE_World")
        bpy.context.scene.world = world
        world.use_nodes = True
        nodes = world.node_tree.nodes
        nodes.clear()
        tex = nodes.new('ShaderNodeTexEnvironment')
        try: tex.image = bpy.data.images.load(filepath)
        except: pass
        bg = nodes.new('ShaderNodeBackground')
        out = nodes.new('ShaderNodeOutputWorld')
        world.node_tree.links.new(tex.outputs['Color'], bg.inputs['Color'])
        world.node_tree.links.new(bg.outputs['Background'], out.inputs['Surface'])
        
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

    def execute(self, context):
        filepath = get_strip_path(context)
        if not filepath:
            self.report({'ERROR'}, "Please select a Movie or Image strip.")
            return {'CANCELLED'}

        setup_cycles()
        
        # Cleanup Existing
        delete_existing_object(NAME_DOME_SHELL)
        delete_existing_object(NAME_DOME_FLOOR)
        delete_existing_object(NAME_SUN)

        # Environment Darkening
        if not bpy.context.scene.world: bpy.context.scene.world = bpy.data.worlds.new("Dark")
        bpy.context.scene.world.use_nodes = True
        bg = bpy.context.scene.world.node_tree.nodes.get('Background')
        if bg: bg.inputs[1].default_value = 0.0

        # Geometry
        bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1)
        dome = context.active_object
        dome.name = NAME_DOME_SHELL # Temporary name until separation
        dome.scale = (20, 20, 20)
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
        # selected_objects usually contains [Original(Unselected), New(Selected)]
        # We need to find which is which.
        objects = context.selected_objects
        
        # The object that remains 'dome' is the one we started with. 
        # The new object is the separated part.
        floor = None
        for obj in objects:
            if obj != dome:
                floor = obj
                break
        
        # Rename strictly
        dome.name = NAME_DOME_SHELL
        if floor:
            floor.name = NAME_DOME_FLOOR
        
            # Flatten Floor
            floor.scale.z = 0
            
            # Fix Floor Rings (Linearize)
            redistribute_floor_geometry(floor)
            
            # Apply Materials
            create_polar_shader(floor, filepath)
            floor.visible_shadow = False
        
        create_dome_shell_mat(dome, filepath)
        dome.visible_shadow = False
        
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

classes = (VSE_OT_ConvertToEnvironment, VSE_OT_ConvertToHalfDome, VSE_MT_ConvertTo3DMenu)

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.SEQUENCER_MT_strip.append(menu_func)

def unregister():
    bpy.types.SEQUENCER_MT_strip.remove(menu_func)
    for cls in reversed(classes): bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
