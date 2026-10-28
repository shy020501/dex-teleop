"""Official nine tasks and their suggested embodiments, without substitutions."""
TASKS = {
 'TwoArmLiftTray': ('Lift tray', ['PandaDexRH','PandaDexLH'], 'Lift the tray and both objects above the table while keeping both objects on the tray.'),
 'TwoArmBoxCleanup': ('Box cleanup', ['PandaDexRH','PandaDexLH'], 'Align the lid with the box and close it.'),
 'TwoArmDrawerCleanup': ('Drawer cleanup', ['PandaDexRH','PandaDexLH'], 'Put the object into the drawer and close the drawer.'),
 'TwoArmCoffee': ('Coffee', ['GR1FixedLowerBody'], 'Insert the coffee pod into its holder and close the machine lid.'),
 'TwoArmPouring': ('Pouring', ['GR1FixedLowerBody'], 'Pour the ball into the bowl and place the bowl upright on its pad.'),
 'TwoArmCanSortRandom': ('Can sorting', ['GR1ArmsOnly'], 'Place the can into the bin matching its color.'),
 'TwoArmThreading': ('Threading', ['Panda','Panda'], 'Thread the needle through the ring using the parallel grippers.'),
 'TwoArmThreePieceAssembly': ('Three-piece assembly', ['Panda','Panda'], 'Assemble the three pieces using the parallel grippers.'),
 'TwoArmTransport': ('Transport', ['Panda','Panda'], 'Place the payload in the target bin and the trash in the trash bin using the parallel grippers.'),
}
DEFAULT_TASK = 'TwoArmLiftTray'
def task_info(task):
 if task not in TASKS: raise ValueError('Unknown DexMimicGen task: '+str(task))
 title,robots,summary=TASKS[task]
 return dict(id=task,title=title,robots=list(robots),summary=summary,dexterous=robots[0]!='Panda')
def task_catalog(assets):
 return [dict(id=k,description=v[0],available=assets.ready(),missing_assets=0 if assets.ready() else 1,
  success_criteria=dict(summary=v[2],details=['Success is evaluated by the pinned upstream task implementation.'],
  evaluation_mode='automatic',automatic_success=True,dwell_time_s=None)) for k,v in TASKS.items()]
