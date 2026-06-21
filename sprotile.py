bl_info = {
    "name": "Sprotile",
    "author": "Claude 4.6 Sonnet",
    "version": (3, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > UV Mapping | UV > Atlas Texture Picker",
    "description": "Map selected faces to a specific tile in a texture atlas with visual picker",
    "category": "UV",
}

import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
from bpy.props import IntProperty, FloatProperty, EnumProperty
import math

try:
    import gpu.state as gpu_state
    USE_GPU_STATE = True
except:
    import bgl
    USE_GPU_STATE = False

# Global variable to store the modal operator instance
_modal_operator = None


class MESH_OT_map_to_atlas_tile(bpy.types.Operator):
    """Map selected faces to a specific atlas tile"""
    bl_idname = "mesh.map_to_atlas_tile"
    bl_label = "Map to Atlas Tile"
    bl_options = {'REGISTER', 'UNDO'}

    tile_width: IntProperty(
        name="Tile Width",
        description="Width of the tile in pixels",
        default=64,
        min=1
    )

    tile_height: IntProperty(
        name="Tile Height",
        description="Height of the tile in pixels",
        default=64,
        min=1
    )

    column: IntProperty(
        name="Column",
        description="Column index (0-based)",
        default=0,
        min=0
    )

    row: IntProperty(
        name="Row",
        description="Row index (0-based)",
        default=0,
        min=0
    )

    def execute(self, context):
        obj = context.active_object

        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Active object is not a mesh")
            return {'CANCELLED'}

        if obj.mode != 'EDIT':
            self.report({'ERROR'}, "Must be in Edit Mode")
            return {'CANCELLED'}

        if not obj.data.materials:
            self.report({'ERROR'}, "Object has no materials")
            return {'CANCELLED'}

        mat = obj.active_material
        if mat is None:
            self.report({'ERROR'}, "No active material")
            return {'CANCELLED'}

        atlas_width = 1024
        atlas_height = 512

        if mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    atlas_width = node.image.size[0]
                    atlas_height = node.image.size[1]
                    break

        tile_u = self.tile_width / atlas_width
        tile_v = self.tile_height / atlas_height

        u_start = self.column * tile_u

        rows_total = atlas_height / self.tile_height
        v_start = (rows_total - 1 - self.row) * tile_v

        me = obj.data
        bm = bmesh.from_edit_mesh(me)

        uv_layer = bm.loops.layers.uv.verify()

        selected_faces = [f for f in bm.faces if f.select]

        if not selected_faces:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        for face in selected_faces:
            num_verts = len(face.loops)

            for i, loop in enumerate(face.loops):
                if num_verts == 3:
                    uvs = [(0, 0), (1, 0), (0, 1)]
                elif num_verts == 4:
                    uvs = [(0, 0), (1, 0), (1, 1), (0, 1)]
                else:
                    angle = (i / num_verts) * 6.28318
                    u_local = 0.5 + 0.5 * math.cos(angle)
                    v_local = 0.5 + 0.5 * math.sin(angle)
                    loop[uv_layer].uv = (
                        u_start + u_local * tile_u,
                        v_start + v_local * tile_v
                    )
                    continue

                u_local, v_local = uvs[i % len(uvs)]
                loop[uv_layer].uv = (
                    u_start + u_local * tile_u,
                    v_start + v_local * tile_v
                )

        bmesh.update_edit_mesh(me)

        self.report(
            {'INFO'},
            f"Mapped {len(selected_faces)} faces to tile ({self.column}, {self.row}) "
            f"[{self.tile_width}×{self.tile_height}px]"
        )
        return {'FINISHED'}


class MESH_OT_atlas_texture_picker(bpy.types.Operator):
    """Interactive texture atlas picker - Click on tiles to map UV coordinates"""
    bl_idname = "mesh.atlas_texture_picker"
    bl_label = "Atlas Texture Picker"
    bl_options = {'REGISTER', 'UNDO'}

    tile_size: IntProperty(
        name="Tile Size",
        description="Base size of a full square tile in pixels",
        default=64,
        min=1
    )

    tile_mode: EnumProperty(
        name="Tile Mode",
        description="Shape of the tile region to select",
        items=[
            ('FULL',      "Full Tile",        "Full square tile (e.g. 64×64)"),
            ('HALF_WIDE', "Half Tile – Wide", "Landscape half tile (e.g. 64×32)"),
            ('HALF_TALL', "Half Tile – Tall", "Portrait half tile (e.g. 32×64)"),
        ],
        default='FULL'
    )

    zoom: FloatProperty(default=1.0)
    offset_x: FloatProperty(default=0.0)
    offset_y: FloatProperty(default=0.0)
    hover_col: IntProperty(default=-1)
    hover_row: IntProperty(default=-1)

    # ------------------------------------------------------------------ helpers

    def get_tile_dims(self):
        """Return (tile_w_px, tile_h_px) for the current tile_mode."""
        s = self.tile_size
        if self.tile_mode == 'HALF_WIDE':
            return s, s // 2          # e.g. 64 × 32  (wider than tall)
        elif self.tile_mode == 'HALF_TALL':
            return s // 2, s          # e.g. 32 × 64  (taller than wide)
        else:
            return s, s               # e.g. 64 × 64

    def get_atlas_image(self, context):
        obj = context.active_object
        if obj and obj.type == 'MESH' and obj.active_material:
            mat = obj.active_material
            if mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        return node.image
        return None

    # ------------------------------------------------------------------ modal

    def modal(self, context, event):
        context.area.tag_redraw()

        if event.type == 'MOUSEMOVE':
            if self.is_panning:
                delta_x = event.mouse_x - self.pan_start_x
                delta_y = event.mouse_y - self.pan_start_y
                self.offset_x += delta_x
                self.offset_y += delta_y
                self.pan_start_x = event.mouse_x
                self.pan_start_y = event.mouse_y
            else:
                self.update_hover(context, event)

        elif event.type in {'MIDDLEMOUSE', 'RIGHTMOUSE'}:
            if event.value == 'PRESS':
                self.is_panning = True
                self.pan_start_x = event.mouse_x
                self.pan_start_y = event.mouse_y
            elif event.value == 'RELEASE':
                self.is_panning = False
            return {'RUNNING_MODAL'}

        elif event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            region = context.region
            mouse_x = event.mouse_region_x
            mouse_y = event.mouse_region_y

            aspect = self.atlas_width / self.atlas_height
            old_display_height = min(region.height * 0.8, 800) * self.zoom
            old_display_width  = old_display_height * aspect
            old_start_x = (region.width  - old_display_width)  / 2 + self.offset_x
            old_start_y = (region.height - old_display_height) / 2 + self.offset_y

            if old_display_width > 0 and old_display_height > 0:
                mouse_rel_x_norm = (mouse_x - old_start_x) / old_display_width
                mouse_rel_y_norm = (mouse_y - old_start_y) / old_display_height
            else:
                mouse_rel_x_norm = 0.5
                mouse_rel_y_norm = 0.5

            zoom_factor = 1.1 if event.type == 'WHEELUPMOUSE' else 0.9
            self.zoom *= zoom_factor

            new_display_height = min(region.height * 0.8, 800) * self.zoom
            new_display_width  = new_display_height * aspect

            new_start_x_unadjusted = (region.width  - new_display_width)  / 2 + self.offset_x
            new_start_y_unadjusted = (region.height - new_display_height) / 2 + self.offset_y

            new_mouse_abs_x = new_start_x_unadjusted + (mouse_rel_x_norm * new_display_width)
            new_mouse_abs_y = new_start_y_unadjusted + (mouse_rel_y_norm * new_display_height)

            self.offset_x -= (new_mouse_abs_x - mouse_x)
            self.offset_y -= (new_mouse_abs_y - mouse_y)

            return {'RUNNING_MODAL'}

        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if self.hover_col >= 0 and self.hover_row >= 0:
                tile_w_px, tile_h_px = self.get_tile_dims()
                bpy.ops.mesh.map_to_atlas_tile(
                    tile_width=tile_w_px,
                    tile_height=tile_h_px,
                    column=self.hover_col,
                    row=self.hover_row
                )
                self.finish(context)
                return {'FINISHED'}

        # H key: cycle tile mode without closing the picker
        elif event.type == 'H' and event.value == 'PRESS':
            modes = ['FULL', 'HALF_WIDE', 'HALF_TALL']
            self.tile_mode = modes[(modes.index(self.tile_mode) + 1) % len(modes)]
            # Reset hover so the highlight recomputes with the new grid
            self.hover_col = -1
            self.hover_row = -1
            return {'RUNNING_MODAL'}

        elif event.type in {'BUTTON4MOUSE', 'ESC'}:
            self.finish(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ------------------------------------------------------------------ hover

    def update_hover(self, context, event):
        if not self.image:
            return

        mouse_x = event.mouse_region_x
        mouse_y = event.mouse_region_y

        region = context.region

        aspect = self.atlas_width / self.atlas_height
        display_height = min(region.height * 0.8, 800) * self.zoom
        display_width  = display_height * aspect

        start_x = (region.width  - display_width)  / 2 + self.offset_x
        start_y = (region.height - display_height) / 2 + self.offset_y

        if (mouse_x < start_x or mouse_x > start_x + display_width or
                mouse_y < start_y or mouse_y > start_y + display_height):
            self.hover_col = -1
            self.hover_row = -1
            return

        rel_x = (mouse_x - start_x) / display_width
        rel_y = (mouse_y - start_y) / display_height

        tile_w_px, tile_h_px = self.get_tile_dims()
        cols = int(self.atlas_width  / tile_w_px)
        rows = int(self.atlas_height / tile_h_px)

        self.hover_col = int(rel_x * cols)
        self.hover_row = int((1.0 - rel_y) * rows)

        self.hover_col = max(0, min(cols - 1, self.hover_col))
        self.hover_row = max(0, min(rows - 1, self.hover_row))

    # ------------------------------------------------------------------ invoke

    def invoke(self, context, event):
        global _modal_operator

        self.draw_handle = None
        self.image       = None
        self.atlas_width  = 1024
        self.atlas_height = 512
        self.is_panning   = False
        self.pan_start_x  = 0
        self.pan_start_y  = 0

        # Read persistent settings from the scene
        scene = context.scene
        self.tile_size = scene.sprotile_tile_size
        self.tile_mode = scene.sprotile_tile_mode

        obj = context.active_object

        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Active object is not a mesh")
            return {'CANCELLED'}

        if obj.mode != 'EDIT':
            self.report({'ERROR'}, "Must be in Edit Mode")
            return {'CANCELLED'}

        self.image = self.get_atlas_image(context)
        if not self.image:
            self.report({'ERROR'}, "No texture image found in active material")
            return {'CANCELLED'}

        self.atlas_width  = self.image.size[0]
        self.atlas_height = self.image.size[1]

        self.draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self.draw_callback, (context,), 'WINDOW', 'POST_PIXEL'
        )

        _modal_operator = self
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # ------------------------------------------------------------------ finish

    def finish(self, context):
        global _modal_operator
        if self.draw_handle:
            bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle, 'WINDOW')
        _modal_operator = None
        context.area.tag_redraw()

    # ------------------------------------------------------------------ draw

    def draw_callback(self, context):
        if not self.image:
            return

        region = context.region

        aspect = self.atlas_width / self.atlas_height
        display_height = min(region.height * 0.8, 800) * self.zoom
        display_width  = display_height * aspect

        start_x = (region.width  - display_width)  / 2 + self.offset_x
        start_y = (region.height - display_height) / 2 + self.offset_y

        if USE_GPU_STATE:
            gpu.state.blend_set('ALPHA')
        else:
            bgl.glEnable(bgl.GL_BLEND)

        # --- dark background
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind()
        shader.uniform_float("color", (0.0, 0.0, 0.0, 0.8))
        batch = batch_for_shader(shader, 'TRI_FAN', {"pos": (
            (0, 0), (region.width, 0),
            (region.width, region.height), (0, region.height)
        )})
        batch.draw(shader)

        # --- atlas texture
        try:
            if hasattr(gpu.texture, 'from_image'):
                texture = gpu.texture.from_image(self.image)
                try:
                    shader = gpu.shader.from_builtin('IMAGE_SCENE_LINEAR_TO_REC709_SRGB')
                except ValueError:
                    shader = gpu.shader.from_builtin('IMAGE')
                shader.bind()
                shader.uniform_sampler("image", texture)
                batch = batch_for_shader(shader, 'TRI_FAN', {
                    "pos": (
                        (start_x, start_y),
                        (start_x + display_width, start_y),
                        (start_x + display_width, start_y + display_height),
                        (start_x, start_y + display_height)
                    ),
                    "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))
                })
                batch.draw(shader)
            else:
                self.image.gl_load()
                if not USE_GPU_STATE:
                    bgl.glActiveTexture(bgl.GL_TEXTURE0)
                    bgl.glBindTexture(bgl.GL_TEXTURE_2D, self.image.bindcode)
                shader = gpu.shader.from_builtin('IMAGE')
                shader.bind()
                shader.uniform_int("image", 0)
                batch = batch_for_shader(shader, 'TRI_FAN', {
                    "pos": (
                        (start_x, start_y),
                        (start_x + display_width, start_y),
                        (start_x + display_width, start_y + display_height),
                        (start_x, start_y + display_height)
                    ),
                    "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))
                })
                batch.draw(shader)
        except Exception as e:
            print(f"Texture drawing error: {e}")

        # --- tile grid (uses tile_w_px / tile_h_px, not always square)
        tile_w_px, tile_h_px = self.get_tile_dims()
        cols = int(self.atlas_width  / tile_w_px)
        rows = int(self.atlas_height / tile_h_px)

        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        shader.bind()
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.3))

        grid_vertices = []
        for i in range(cols + 1):
            x = start_x + (i / cols) * display_width
            grid_vertices.extend([(x, start_y), (x, start_y + display_height)])
        for i in range(rows + 1):
            y = start_y + (i / rows) * display_height
            grid_vertices.extend([(start_x, y), (start_x + display_width, y)])

        batch_for_shader(shader, 'LINES', {"pos": grid_vertices}).draw(shader)

        # --- hover highlight
        if self.hover_col >= 0 and self.hover_row >= 0:
            tile_w_disp = display_width  / cols
            tile_h_disp = display_height / rows

            tile_x = start_x + self.hover_col * tile_w_disp
            tile_y = start_y + (rows - 1 - self.hover_row) * tile_h_disp

            shader.uniform_float("color", (1.0, 1.0, 0.0, 0.5))
            batch_for_shader(shader, 'TRI_FAN', {"pos": (
                (tile_x,              tile_y),
                (tile_x + tile_w_disp, tile_y),
                (tile_x + tile_w_disp, tile_y + tile_h_disp),
                (tile_x,              tile_y + tile_h_disp)
            )}).draw(shader)

        # --- HUD text
        import blf
        font_id = 0
        blf.size(font_id, 16)
        blf.color(font_id, 1.0, 1.0, 1.0, 1.0)

        mode_label = {'FULL': 'Full', 'HALF_WIDE': 'Half Wide', 'HALF_TALL': 'Half Tall'}
        dims_str = f"{tile_w_px}×{tile_h_px}px"
        hint = (
            f"          [H] Mode: {mode_label.get(self.tile_mode, self.tile_mode)} ({dims_str})  |  "
            "Left Click: Select  |  ESC: Cancel  |  Wheel: Zoom  |  Middle or Right Mouse: Pan"
        )
        blf.position(font_id, 20, region.height - 80, 0)
        blf.draw(font_id, hint)

        if self.hover_col >= 0 and self.hover_row >= 0:
            blf.position(font_id, 20, region.height - 100, 0)
            blf.draw(font_id, f"          Tile: Col {self.hover_col}, Row {self.hover_row}  ({dims_str})")

        if USE_GPU_STATE:
            gpu.state.blend_set('NONE')
        else:
            bgl.glDisable(bgl.GL_BLEND)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_atlas_mapper_panel(bpy.types.Panel):
    """Panel for Atlas Texture Mapper"""
    bl_label = "Atlas UV Mapper"
    bl_idname = "VIEW3D_PT_atlas_mapper"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'UV Mapping'

    def draw(self, context):
        layout = self.layout
        scene  = context.scene

        obj = context.active_object
        if obj and obj.type == 'MESH' and obj.mode == 'EDIT':
            col = layout.column(align=True)
            col.prop(scene, "sprotile_tile_size")
            col.prop(scene, "sprotile_tile_mode")
            layout.separator()
            layout.operator("mesh.atlas_texture_picker", text="Open Texture Picker", icon='IMAGE_DATA')

            layout.separator()
            layout.label(text="Manual Entry:")
            layout.operator("mesh.map_to_atlas_tile", text="Map to Tile", icon='UV')
        else:
            layout.label(text="Select mesh in Edit Mode", icon='INFO')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def menu_func(self, context):
    self.layout.separator()
    self.layout.operator(MESH_OT_atlas_texture_picker.bl_idname, icon='IMAGE_DATA')
    self.layout.operator(MESH_OT_map_to_atlas_tile.bl_idname, icon='UV')


classes = (
    MESH_OT_map_to_atlas_tile,
    MESH_OT_atlas_texture_picker,
    VIEW3D_PT_atlas_mapper_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_uv_map.append(menu_func)

    bpy.types.Scene.sprotile_tile_size = IntProperty(
        name="Tile Size",
        description="Base size of a full square tile in pixels",
        default=64,
        min=1,
    )
    bpy.types.Scene.sprotile_tile_mode = EnumProperty(
        name="Tile Mode",
        description="Shape of tile to pick in the atlas",
        items=[
            ('FULL',      "Full Tile",        "Full square tile (e.g. 64×64)"),
            ('HALF_WIDE', "Half Tile – Wide", "Landscape half tile (e.g. 64×32)"),
            ('HALF_TALL', "Half Tile – Tall", "Portrait half tile (e.g. 32×64)"),
        ],
        default='FULL',
    )


def unregister():
    global _modal_operator
    if _modal_operator:
        _modal_operator.finish(bpy.context)

    bpy.types.VIEW3D_MT_uv_map.remove(menu_func)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.sprotile_tile_size
    del bpy.types.Scene.sprotile_tile_mode


if __name__ == "__main__":
    register()
