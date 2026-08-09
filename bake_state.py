def mark_bake_running(obj):
    obj.fumaris.simulation_state = "baking"


def mark_bake_complete(obj):
    obj.fumaris.simulation_state = "baked"


def mark_bake_cancelled(obj):
    obj.fumaris.simulation_state = "stopped"
