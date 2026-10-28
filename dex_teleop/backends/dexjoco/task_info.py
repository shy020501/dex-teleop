"""The eleven official task IDs; success comes from upstream task evaluators."""
TASKS = {
    'click_mouse': 'Move the mouse onto its pad and click the left button.',
    'water_plant': 'Grasp the watering can and water the plant.',
    'hammer_nail': 'Use the hammer to drive the nail into the wooden board.',
    'pick_bucket': 'Place the food into the bucket, then lift the bucket.',
    'pinch_tongs': 'Grasp the tongs and perform three open-close motions.',
    'fold_glasses': 'Fold the glasses and place them in the case.',
    'bimanual_assembly': 'Hold the tray and insert the peg into its hole.',
    'bimanual_hanoi': 'Complete the final two moves of the three-level Tower of Hanoi.',
    'bimanual_microwave_cook': 'Open the microwave, place the food inside, close it, and press start.',
    'bimanual_photograph': 'Hold the camera, align it with the logo, and press the shutter.',
    'bimanual_unlock_ipad': 'Hold the iPad and enter the password 123.',
}

def task_info(task):
    if task not in TASKS:
        raise ValueError('Unknown DexJoCo task: '+str(task))
    return dict(id=task, description=TASKS[task], sides=['right', 'left'] if task.startswith('bimanual_') else ['right'])

def task_catalog(assets):
    return [dict(id=task, description=description, available=assets.ready(), missing_assets=0 if assets.ready() else 1,
                 success_criteria=dict(summary=description, details=['Success is evaluated by the official DexJoCo task.'],
                                       evaluation_mode='automatic', automatic_success=True, dwell_time_s=None))
            for task, description in TASKS.items()]
