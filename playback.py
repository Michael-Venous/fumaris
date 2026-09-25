"""Selection-independent controls for the existing sequential preview player."""
import bpy
from bpy.props import PointerProperty
from .runtime import active_job, active_mode

_KEYMAPS = []


def shortcut_label(context, operator):
    """Show the user's current binding, including custom remaps or removal."""
    configs = context.window_manager.keyconfigs
    config = configs.user or configs.addon
    if config:
        keymap = config.keymaps.get('Window')
        if keymap:
            for item in keymap.keymap_items:
                if item.idname == operator and item.active:
                    return item.to_string()
    return 'Unassigned'


def preview_description(context, action):
    has_direct_shortcut = action in {"stop", "toggle"}
    job = active_job() if active_mode() == 'previewing' else None
    if action in {'toggle', 'pause'}:
        action = ('resume' if getattr(job, '_pause_requested', False) else 'pause') if job else 'play'
    descriptions = {
        'play': 'Start the live preview from the simulation start frame; record VDB frames when Bake on Preview is enabled.',
        'pause': 'Pause preview and keep simulation memory ready to resume; finish and import recorded frames when Bake on Preview is enabled.',
        'resume': 'Resume preview from its paused simulation state.',
        'stop': 'Stop preview and release simulation memory; finish and import recorded frames when Bake on Preview is enabled.',
    }
    binding = shortcut_label(context, 'fumaris.preview_stop' if action == 'stop' else 'fumaris.preview_toggle')
    # Blender appends the keymap shortcut for directly bound operators.
    return descriptions[action] if has_direct_shortcut else descriptions[action] + '\nShortcut: ' + binding


def is_domain(obj):
    return bool(obj and hasattr(obj, 'fumaris') and obj.fumaris.smoke_object_type == 'domain')


def scene_domains(context):
    return [obj for obj in context.view_layer.objects if is_domain(obj)]


def resolve_domain(context):
    job = active_job()
    if job and active_mode() == 'previewing':
        return bpy.data.objects.get(getattr(job, '_domain_name', ''))
    domains = scene_domains(context)
    chosen = getattr(context.scene, 'fumaris_preview_domain', None)
    if chosen in domains:
        return chosen
    selected = getattr(context, 'object', None)
    if selected in domains:
        return selected
    return domains[0] if len(domains) == 1 else None


def _domain_poll(scene, obj):
    return is_domain(obj) and obj.name in scene.objects


class FUMARIS_OT_preview_toggle(bpy.types.Operator):
    bl_idname = 'fumaris.preview_toggle'
    bl_label = 'Fumaris Play / Pause'
    bl_description = 'Play, pause or resume the Fumaris preview without changing object selection'

    @classmethod
    def description(cls, context, _properties):
        return preview_description(context, 'toggle')

    @classmethod
    def poll(cls, context):
        if active_mode() == 'previewing':
            return True
        if active_job():
            cls.poll_message_set('Wait for the active Fumaris bake to finish')
            return False
        domain = resolve_domain(context)
        if domain is None:
            cls.poll_message_set('Choose a domain in the Fumaris playback menu')
            return False
        if domain.fumaris.simulation_state in {'baking', 'baked'}:
            cls.poll_message_set('Delete the domain bake before starting a live preview')
            return False
        return True

    def execute(self, context):
        if active_mode() == 'previewing':
            return bpy.ops.fumaris.preview_pause()
        domain = resolve_domain(context)
        if domain is None:
            self.report({'WARNING'}, 'Choose a Fumaris playback domain')
            return {'CANCELLED'}
        result = bpy.ops.fumaris.preview_play(domain_name=domain.name)
        # The inner player owns its own modal handler; this dispatcher does not.
        return {'CANCELLED'} if 'CANCELLED' in result else {'FINISHED'}


def draw_controls(layout, context, *, compact=False):
    domain = resolve_domain(context)
    job = active_job()
    running = active_mode() == 'previewing'
    paused = running and bool(getattr(job, '_pause_requested', False))
    row = layout.row(align=True)
    if compact:
        row.popover(panel='FUMARIS_PT_playback', text='Fumaris')
    row.operator('fumaris.preview_toggle', text='' if compact else ('Resume' if paused else 'Pause' if running else 'Play'),
                 icon='PAUSE' if running and not paused else 'PLAY')
    row.operator('fumaris.preview_stop', text='' if compact else 'Stop', icon='SNAP_FACE')
    if not compact:
        layout.label(text='Domain: ' + domain.name if domain else 'Choose a domain', icon='MOD_FLUID')
        if running:
            layout.label(text='Paused' if paused else 'Playing')
        elif active_mode() == 'baking':
            layout.label(text='Baking — live preview unavailable', icon='INFO')


class FUMARIS_PT_playback(bpy.types.Panel):
    bl_label = 'Fumaris Playback'
    bl_idname = 'FUMARIS_PT_playback'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'

    def draw(self, context):
        row = self.layout.row()
        row.enabled = not active_job()
        row.prop(context.scene, 'fumaris_preview_domain', text='Domain')
        draw_controls(self.layout, context)
        self.layout.label(text='Edit shortcuts in Fumaris preferences', icon='PREFERENCES')


def draw_header(self, context):
    if context.area.type == 'DOPESHEET_EDITOR' and context.space_data.mode != 'TIMELINE':
        return
    if not active_job() and not scene_domains(context):
        return
    draw_controls(self.layout, context, compact=True)


def draw_keymaps(layout, context):
    import rna_keymap_ui
    config = context.window_manager.keyconfigs.user
    if not config:
        return
    keymap = config.keymaps.get('Window')
    if keymap:
        layout.label(text='Live preview shortcuts (independent of selection)')
        for item in keymap.keymap_items:
            if item.idname in {'fumaris.preview_toggle', 'fumaris.preview_stop'}:
                rna_keymap_ui.draw_kmi([], config, keymap, item, layout, 0)


def register():
    bpy.types.Scene.fumaris_preview_domain = PointerProperty(
        name='Preview Domain', type=bpy.types.Object, poll=_domain_poll,
        description='Domain controlled while editing other objects. A single available domain is chosen automatically')
    for cls in (FUMARIS_OT_preview_toggle, FUMARIS_PT_playback):
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_HT_header.append(draw_header)
    bpy.types.DOPESHEET_HT_header.append(draw_header)
    config = bpy.context.window_manager.keyconfigs.addon
    if config:
        keymap = config.keymaps.new(name='Window', space_type='EMPTY')
        for operator, key in (('fumaris.preview_toggle', 'SPACE'), ('fumaris.preview_stop', 'X')):
            item = keymap.keymap_items.new(operator, key, 'PRESS', shift=True, alt=True)
            _KEYMAPS.append((keymap, item))


def unregister():
    for keymap, item in _KEYMAPS:
        keymap.keymap_items.remove(item)
    _KEYMAPS.clear()
    bpy.types.DOPESHEET_HT_header.remove(draw_header)
    bpy.types.VIEW3D_HT_header.remove(draw_header)
    for cls in (FUMARIS_PT_playback, FUMARIS_OT_preview_toggle):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.fumaris_preview_domain
