"""Selection-independent controls for the existing sequential preview player."""
import time

import bpy
from bpy.props import PointerProperty
from .runtime import active_job, active_mode, background_job

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
    domain = resolve_domain(context) if hasattr(context, 'view_layer') else None
    cached = bool(domain and getattr(domain.fumaris, 'preview_cache', False))
    descriptions = {
        'play': ('Start the live preview from the simulation start frame. Its frames are cached (Cache Preview), '
                 'so a paused preview can scrub and play them back, and an unchanged loop replays them instead of simulating.'
                 if cached else
                 'Start the live preview from the simulation start frame. It loops over its range; turn on Cache '
                 'Preview to keep its frames for scrubbing and saving.'),
        'pause': 'Pause the preview, keeping its simulation ready to resume; with Cache Preview on, scrub or play the timeline to see the cached frames, or Save Sequence.',
        'resume': 'Resume the preview from its live frame (after the last frame: from the start).',
        'stop': 'Stop the preview, releasing its simulation memory and its cache.',
    }
    binding = shortcut_label(context, 'fumaris.preview_stop' if action == 'stop' else 'fumaris.preview_toggle')
    # Blender appends the keymap shortcut for directly bound operators.
    return descriptions[action] if has_direct_shortcut else descriptions[action] + '\nShortcut: ' + binding


def is_domain(obj):
    return bool(obj and hasattr(obj, 'fumaris') and obj.fumaris.smoke_object_type == 'domain')


# One header redraw asks for the domain list several times (draw plus each
# button's poll). A brief memo turns those into a single pass over the scene.
_DOMAIN_SCAN_SECONDS = 0.05
_domain_scan = (None, 0.0, ())


def scene_domains(context):
    global _domain_scan
    view_layer = context.view_layer
    if not hasattr(view_layer, "as_pointer"):
        return [obj for obj in view_layer.objects if is_domain(obj)]
    key = (view_layer.as_pointer(), len(view_layer.objects))
    now = time.monotonic()
    cached_key, cached_at, names = _domain_scan
    if cached_key == key and now - cached_at < _DOMAIN_SCAN_SECONDS:
        objects = view_layer.objects
        domains = [objects.get(name) for name in names]
        if all(is_domain(obj) for obj in domains):
            return domains
    domains = [obj for obj in view_layer.objects if is_domain(obj)]
    _domain_scan = (key, now, tuple(obj.name for obj in domains))
    return domains


_ROLE_COLLECTIONS = ('emitter_collection', 'collider_collection', 'effector_collection', 'outflow_collection')


def participant_domains(obj, domains):
    """The domains whose participant collections hold obj."""
    found = []
    for domain in domains:
        settings = domain.fumaris
        for attribute in _ROLE_COLLECTIONS:
            collection = getattr(settings, attribute, None)
            if collection is not None and obj.name in collection.all_objects:
                found.append(domain)
                break
    return found


def resolve_domain(context):
    """The domain Play, Bake and the sidebar act on: the one previewing; else
    the one a panel set for its own controls (an emitter's panel: its own
    domain, 2026-11-14 UX test); else the selected domain; else the chosen
    domain (selecting a participant keeps it, so Add Objects adds to it);
    else the domain of the selected participant, or the only one."""
    job = active_job()
    if job and active_mode() == 'previewing':
        return bpy.data.objects.get(getattr(job, '_domain_name', ''))
    domains = scene_domains(context)
    own = getattr(context, 'fumaris_domain', None)
    if own in domains:
        return own
    chosen = getattr(context.scene, 'fumaris_preview_domain', None)
    selected = getattr(context, 'object', None)
    if selected in domains:
        return selected
    if chosen in domains:
        return chosen
    owners = participant_domains(selected, domains) if selected is not None else []
    if owners:
        return owners[0]
    return domains[0] if len(domains) == 1 else None


def own_domain(context, obj):
    """A participant's own domain: the chosen one when it is among the
    domains using obj, else the first of them; None if no domain uses it."""
    owners = participant_domains(obj, scene_domains(context))
    chosen = getattr(context.scene, 'fumaris_preview_domain', None)
    return chosen if chosen in owners else (owners[0] if owners else None)


def choose_domain(scene, domain):
    """Make domain the scene's chosen domain. Blender keeps a deleted object in
    the file while a property points at it, so a chosen domain deleted with
    Blender's own Delete stayed (with the material made for its bakes) until
    now: once nothing uses it any more, free both."""
    previous = getattr(scene, 'fumaris_preview_domain', None)
    scene.fumaris_preview_domain = domain
    if (not isinstance(previous, bpy.types.Object) or previous == domain or previous.users
            or previous.use_fake_user or previous.library):
        return
    # A scene's tool settings can still name the object it last worked on
    # (Blender clears that itself on removal); a child, constraint or
    # modifier that still uses it keeps it.
    if any(not isinstance(user, bpy.types.Scene) for user in bpy.data.user_map(subset=[previous])[previous]):
        return
    from .importers import GENERATED_MATERIAL_MARKER
    material = previous.fumaris.managed_material if is_domain(previous) else None
    bpy.data.objects.remove(previous)
    if (material is not None and material.get(GENERATED_MATERIAL_MARKER)
            and material.users == 0 and not material.use_fake_user):
        bpy.data.materials.remove(material)


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
        if background_job(domain):
            cls.poll_message_set('This domain is baking in the background')
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


class FUMARIS_OT_preview_restart(bpy.types.Operator):
    bl_idname = 'fumaris.preview_restart'
    bl_label = 'Fumaris Restart Preview'
    bl_description = ('Start the preview again from the simulation start frame '
                      '(starts it when it is not running)')

    @classmethod
    def poll(cls, context):
        # Not running: the same checks as Play.
        return active_mode() == 'previewing' or FUMARIS_OT_preview_toggle.poll(context)

    def execute(self, context):
        domain = resolve_domain(context)
        if domain is None:
            return {'CANCELLED'}
        name = domain.name
        if active_mode() == 'previewing':
            # The running bridge starts over in place when it can (a few ms
            # instead of about 1 s for a new process, 2026-11-14).
            job = active_job()
            restart = getattr(job, 'restart_in_place', None)
            if restart is not None and restart(context):
                return {'FINISHED'}
            bpy.ops.fumaris.preview_stop()
        result = bpy.ops.fumaris.preview_play(domain_name=name)
        return {'CANCELLED'} if 'CANCELLED' in result else {'FINISHED'}


def recorded_volume(domain):
    """The domain's visible imported bake, or None. Its folders all live below
    the domain's output folder."""
    if domain is None:
        return None
    import os
    from .importers import VOLUME_MARKER
    from .utils import peek_output_directory_for_object
    # Drawn every redraw: peek, so a domain that never ran (no cache identity
    # yet) writes nothing; it has no recording either.
    root = peek_output_directory_for_object(domain)
    if root is None:
        return None
    root = os.path.normpath(root)
    found = None
    for obj in bpy.data.objects:
        marker = obj.get(VOLUME_MARKER) if obj.type == 'VOLUME' else None
        if not marker:
            continue
        marker = os.path.normpath(marker)
        if marker != root and not marker.startswith(root + os.sep):
            continue
        if found is None or (found.hide_viewport and not obj.hide_viewport):
            found = obj
    return found


def recorded_range(volume):
    """(first, last) scene frame of a recorded sequence, or None."""
    data = getattr(volume, 'data', None)
    if data is None or not data.is_sequence or data.frame_duration <= 0:
        return None
    first = int(data.frame_start + data.frame_offset)
    return first, first + int(data.frame_duration) - 1


def transport_status(context, domain):
    """'Live 42 / Cached 1-41' (or 'Baked 1-120') for the transport row."""
    parts = []
    job = active_job()
    if active_mode() == 'previewing' and job is not None:
        live = getattr(job, '_frame', None)
        if live:
            parts.append(f'Live {int(live)}')
        cache = getattr(job, '_preview_cache', None) or {}
        if int(cache.get('frames', 0)) > 0:
            parts.append(f"Cached {int(cache['first'])}\u2013{int(cache['last'])}")
    span = recorded_range(recorded_volume(domain))
    if span:
        parts.append(f'Baked {span[0]}\u2013{span[1]}')
    return ' / '.join(parts)


def draw_controls(layout, context, *, compact=False, popover=True, details=True, domain=None):
    if domain is not None and not active_job():
        # The buttons below act on it too (resolve_domain reads this). While
        # a job runs they control that job, so its domain is shown.
        layout.context_pointer_set('fumaris_domain', domain)
    else:
        domain = resolve_domain(context)
    job = active_job()
    running = active_mode() == 'previewing'
    paused = running and bool(getattr(job, '_pause_requested', False))
    row = layout.row(align=True)
    if compact and popover:
        row.popover(panel='FUMARIS_PT_playback', text='Fumaris')
    row.operator('fumaris.preview_toggle', text='' if compact else ('Resume' if paused else 'Pause' if running else 'Play'),
                 icon='PAUSE' if running and not paused else 'PLAY')
    # One button for Bake and Stop: Stop while a preview or bake runs (the
    # two never run together), else Bake for the same domain (the Bake
    # operator acts on context.object).
    if active_mode() == 'baking':
        row.operator('fumaris.stop', text='' if compact else 'Stop Bake', icon='SNAP_FACE')
    elif running:
        row.operator('fumaris.preview_stop', text='' if compact else 'Stop', icon='SNAP_FACE')
        # Small, as in the Simulate panel.
        row.row(align=True).operator('fumaris.preview_restart', text='', icon='FILE_REFRESH')
    else:
        bake = row.row(align=True)
        bake.enabled = domain is not None and not job
        if domain is not None:
            bake.context_pointer_set('object', domain)
        bake.operator('fumaris.bake', text='' if compact else 'Bake', icon='RENDER_ANIMATION')
    # The header shows no status: its changing width would move the buttons.
    if not compact and domain is not None:
        # A failed preview or bake is visible wherever its controls are.
        import textwrap
        from .diagnostics import error_summary, snapshot
        state = snapshot(domain)
        if error_summary(state):
            errors = [line for line in state['log'] if line.lower().startswith('error:')]
            text = errors[-1][len('error:'):].strip() if errors else 'Simulation stopped; see Diagnostics'
            error = layout.column(align=True)
            error.alert = True
            # Wrapped, not clipped: the end of a message is often the useful part.
            for index, line in enumerate(textwrap.wrap(text, 32, max_lines=5, placeholder='…')):
                error.label(text=line, icon='ERROR' if index == 0 else 'BLANK1')
    status = '' if compact or not details else transport_status(context, domain)
    if status:
        layout.label(text=status)
    if not compact and details:
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
        for item in keymap.keymap_items:
            if item.idname in {'fumaris.preview_toggle', 'fumaris.preview_stop', 'fumaris.preview_restart'}:
                rna_keymap_ui.draw_kmi([], config, keymap, item, layout, 0)


def register():
    bpy.types.Scene.fumaris_preview_domain = PointerProperty(
        name='Preview Domain', type=bpy.types.Object, poll=_domain_poll,
        description='Domain controlled while editing other objects. A single available domain is chosen automatically')
    for cls in (FUMARIS_OT_preview_toggle, FUMARIS_OT_preview_restart, FUMARIS_PT_playback):
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_HT_header.append(draw_header)
    bpy.types.DOPESHEET_HT_header.append(draw_header)
    config = bpy.context.window_manager.keyconfigs.addon
    if config:
        keymap = config.keymaps.new(name='Window', space_type='EMPTY')
        for operator, key in (('fumaris.preview_toggle', 'SPACE'), ('fumaris.preview_stop', 'X'),
                              ('fumaris.preview_restart', 'R')):
            item = keymap.keymap_items.new(operator, key, 'PRESS', shift=True, alt=True)
            _KEYMAPS.append((keymap, item))


def unregister():
    for keymap, item in _KEYMAPS:
        keymap.keymap_items.remove(item)
    _KEYMAPS.clear()
    bpy.types.DOPESHEET_HT_header.remove(draw_header)
    bpy.types.VIEW3D_HT_header.remove(draw_header)
    for cls in (FUMARIS_PT_playback, FUMARIS_OT_preview_restart, FUMARIS_OT_preview_toggle):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.fumaris_preview_domain
