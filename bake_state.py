import os


def mark_bake_running(obj):
    obj.fumaris.simulation_state = "baking"


def mark_bake_complete(obj):
    obj.fumaris.simulation_state = "baked"


def mark_bake_cancelled(obj):
    obj.fumaris.simulation_state = "stopped"



def sync_cache_slot(obj):
    """Make the domain reflect the cache slot it now points at.

    Called when Cache Slot changes. The slot's own bake becomes the visible
    one and the status row describes it; other slots keep their files and
    their imported volumes, hidden.
    """
    from .importers import baked_frame_count, show_only_slot_volumes
    from .preview import clear_preview
    from .runtime import active_job
    from .utils import output_directory_for_object

    if active_job():
        return
    props = obj.fumaris
    directory = output_directory_for_object(obj)
    show_only_slot_volumes(os.path.dirname(directory), directory)
    clear_preview(obj)
    frames = baked_frame_count(directory, props.output_prefix or "fumaris_")
    props.simulation_state = "baked" if frames else "idle"
    props.baked_frames = frames
    props.bake_elapsed = 0.0
    props.bake_peak_vram_mb = 0.0
    props.bake_peak_ram_mb = 0.0
