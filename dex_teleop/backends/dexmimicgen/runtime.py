"""DexMimicGen's real robosuite environments behind the common dashboard."""
import argparse,copy,os,queue,signal,time
from pathlib import Path
import numpy as np
from dex_teleop.paths import WORKSPACE
from dex_teleop.core.config import load_server_config,resolve_server_config
from dex_teleop.core.step_timing import RollingStepTiming
from dex_teleop.dashboard import state
from dex_teleop.dashboard.http import LatestFrame,serve_viewer
from .paths import DATA_ROOT
from .assets import AssetDownloads
from .task_info import DEFAULT_TASK,task_info,task_catalog
from .scene import create_environment,camera_mapping,reset_environment
from .control import Bridge
from .recording import RecordingSession
from .rendering import Renderer,FramePublisher
from .replay import ReplayJobs

class Backend:
    name='dexmimicgen';default_task=DEFAULT_TASK
    def __init__(self,root=DATA_ROOT):
        self.data_root=Path(root).resolve();self.data_root.mkdir(parents=True,exist_ok=True);self.assets=AssetDownloads()
    def tasks(self):return task_catalog(self.assets)

class StepLimit(Exception):pass

class Runtime:
    def __init__(self,args,config):
        self.args=args;self.config=config;self.stopping=False
        self.env=self.renderer=self.bridge=self.recorder=self.publisher=self.server=None
        self.step=self.completed_steps=self.control_steps=0;self.replay_paused=False
        self.sim=dict(physics_dt=.002,control_hz=20,sample_hz=20,preview_hz=20,max_episode_frames=1800,max_buffer_mb=6144)
        self.sim.update(config.get('simulation',{}));self.render_config=dict(config.get('render',{}))
        control=float(self.sim['control_hz'])
        if not control.is_integer() or control<=0 or not 0<self.sim['sample_hz']<=control or not 0<self.sim['preview_hz']<=control:
            raise ValueError('Sample/preview rates must be positive and no greater than control_hz')
        self.sample_stride=round(control/self.sim['sample_hz'])
        self.preview_stride=round(control/self.sim['preview_hz'])
        if not np.isclose(self.sample_stride,control/self.sim['sample_hz']) or not np.isclose(self.preview_stride,control/self.sim['preview_hz']):
            raise ValueError('Sample and preview rates must divide control_hz')
        substeps=1/(control*float(self.sim['physics_dt']))
        if not np.isfinite(substeps) or substeps<1 or not np.isclose(substeps,round(substeps),rtol=0,atol=1e-9):
            raise ValueError('Physics rate must be an integer multiple of control_hz')
        self.ui=state.install_dashboard(state.Dashboard(backend=Backend(args.data_root),state_file=args.output_dir/'settings.json',debug_mode=args.debug),args.task)
        self.ui.update(simulator='dexmimicgen',success_mode='automatic',execution_mode='serial',enabled_sides=['right','left'],pose_reference='end_effector',max_episode_frames=self.sim['max_episode_frames'],sample_hz=self.sim['sample_hz'])
        self.frames=LatestFrame();self.timing=RollingStepTiming(args.timing_window)
    def start(self):
        self.server=serve_viewer(self.frames,self.args.port)
        self.publisher=FramePublisher(self.frames,self.ui.previews,self.render_config.get('jpeg_quality',75))
        self.load(self.ui.snapshot()['task'])
        print(f'[DexMimicGen] Browser http://127.0.0.1:{self.args.port}; input {self.config["network"]["bind"]}',flush=True)
    def load(self,task):
        task_info(task);next_env=create_environment(task,self.config)
        try:
            model=next_env.sim.model._model
            if not np.isclose(model.opt.timestep,float(self.sim['physics_dt'])):raise ValueError('Configured physics_dt differs from the upstream model timestep')
        except Exception:next_env.close();raise
        self.ui.update(ready=False,phase='loading')
        for resource in (self.ui.replays,self.recorder,self.bridge,self.renderer,self.env):
            if resource is not None:
                (resource.stop if resource is self.bridge else resource.close)()
        self.env=next_env;self.model=model;self.data=self.env.sim.data._data
        self.step=self.control_steps=0
        self.mapping=camera_mapping(self.env)
        requested=self.render_config.get('main_camera','agentview')
        if requested not in self.env.sim.model.camera_names:raise ValueError('Unknown main camera '+str(requested))
        self.mapping['cam_task']=requested
        self.renderer=Renderer(self.model,self.render_config,self.mapping)
        self.bridge=Bridge(self.env,self.config);self.bridge.apply_workspace(self.ui.snapshot()['workspace'])
        if self.args.mode=='teleop':self.bridge.start()
        self.bridge.set_enabled(self.args.mode=='teleop' and not self.args.debug)
        launch=dict(task=task,render=self.render_config,camera_mapping=self.mapping,robots=task_info(task)['robots'],
                    policy_action_dim=self.env.action_dim,policy_action_schema=self.bridge.policy_schema(),
                    physics_step_mode='forward_then_step',control_hz=self.sim['control_hz'])
        self.recorder=RecordingSession(self.ui,self.model,task,launch,sample_hz=self.sim['sample_hz'],
            max_frames=self.sim['max_episode_frames'],max_buffer_mb=self.sim['max_buffer_mb'])
        self._native_step=self.env.sim.step
        self.env.sim.step=self.physics_step
        self.ui.replays=ReplayJobs(self.ui.store,self.args.output_dir/'replay-cache')
        self.ui._pose_receiver=self.bridge.receiver
        self.ui.previews.configure([k for k in self.mapping if k!='cam_task'])
        self.ui.update(task=task,ready=True,phase='debug' if self.args.debug else 'idle',recording=False,frames=0,sim_seconds=0.,success=False,
            workspace_ready=not self.args.debug,workspace_pending=False,workspace_effective=self.bridge.workspace_status(),action_spec=self.bridge.action_spec,
            enabled_sides=self.bridge.sides)
        self.ui.persist();self.publisher.reset();self.capture();self.reset_schedule()
    def physics_step(self,*args,**kwargs):
        if self.stopping or (self.args.steps and self.completed_steps>=self.args.steps):raise StepLimit()
        self.recorder.note_control(self.step,self.data,self.bridge.source)
        self._native_step(*args,**kwargs)
        self.step+=1;self.completed_steps+=1
        self.bridge.after_physics_step(float(self.model.opt.timestep))
        self.timing.record(float(self.model.opt.timestep),time.monotonic())
    def reset_schedule(self):self.deadline=time.monotonic();self.timing.reset(self.deadline)
    def capture(self):
        images=self.renderer.capture(self.data)
        self.publisher.offer(images,self.step,simulator='dexmimicgen',task=self.ui.state['task'],debug_mode=self.args.debug,execution_mode='serial')
        return images
    def reset(self):
        self.ui.update(ready=False,phase='resetting');self.bridge.set_enabled(False)
        reset_environment(self.env)
        self.step=self.control_steps=0
        self.bridge.reset();self.bridge.set_enabled(self.args.mode=='teleop' and not self.args.debug)
        self.ui.update(ready=True,phase='saving' if self.recorder.saving else 'idle',workspace_effective=self.bridge.workspace_status())
        self.publisher.reset();self.reset_schedule();return self.capture()
    def commands(self):
        while True:
            try:c=self.ui.commands.get_nowait()
            except queue.Empty:break
            try:
                action=c['action']
                if self.recorder.saving:
                    if action=='toggle' and self.recorder.error:self.recorder.retry();continue
                    raise ValueError('Wait until the episode has finished saving')
                if action=='toggle':
                    if self.recorder.active:
                        self.recorder.finish(self.step,self.data,self.capture(),False,'manual_stop');self.reset()
                    else:
                        images=self.reset();self.recorder.policy_action=self.bridge.update(0.)
                        self.recorder.start(self.step,self.data,images)
                elif action=='reset':
                    if self.recorder.active:self.recorder.finish(self.step,self.data,self.capture(),False,'environment_reset')
                    self.reset()
                elif action=='task':
                    previous=self.ui.snapshot();self.ui.update(ready=False,phase='switching',folder=c['task'])
                    try:self.load(c['task'])
                    except Exception:
                        self.ui.update(folder=previous['folder']);self.load(previous['task']);raise
                elif action=='workspace':
                    self.bridge.apply_workspace(c['settings']);self.ui.update(workspace=c['settings'],workspace_pending=False,workspace_effective=self.bridge.workspace_status());self.ui.persist()
            except Exception as exc:
                self.ui.update(error=str(exc),workspace_pending=False);print('[DexMimicGen] Command failed:',exc,flush=True)
    def service_replay(self):
        jobs=self.ui.replays
        if jobs.busy:
            if not self.replay_paused:self.bridge.set_enabled(False);self.ui.update(phase='replay');self.replay_paused=True
            if jobs.needs_service:jobs.start_pending()
            jobs.snapshot(jobs.job['id']);return True
        if self.replay_paused:
            self.replay_paused=False;self.bridge.set_enabled(self.args.mode=='teleop' and not self.args.debug)
            self.ui.update(phase='idle');self.reset_schedule()
        return False
    def run(self):
        while not self.stopping and (not self.args.steps or self.completed_steps<self.args.steps):
            self.recorder.poll();self.commands()
            if self.args.debug or self.service_replay():time.sleep(.02);continue
            settings=self.ui.snapshot();self.bridge.haptic_enabled=settings['haptic_enabled']
            self.bridge.haptic_encoder.set_force_range(settings['haptic_force_min_n'],settings['haptic_force_max_n'])
            self.bridge.haptic_encoder.set_max_power(settings['haptic_max_power'])
            action=self.bridge.update(1/self.sim['control_hz']);self.recorder.policy_action=action.copy()
            if self.bridge.consume_environment_reset():
                if self.recorder.active:self.recorder.finish(self.step,self.data,self.capture(),False,'environment_reset')
                self.reset();continue
            self.recorder.note_policy(self.step,action)
            try:self.env.step(action)
            except StepLimit:break
            self.control_steps+=1
            sample=self.recorder.active and self.control_steps%self.sample_stride==0
            preview=self.control_steps%self.preview_stride==0
            success=bool(self.env._check_success()) if self.recorder.active else False
            images=self.capture() if sample or preview or success else None
            if sample:self.recorder.append(self.step,self.data,images)
            if self.recorder.active and (success or self.recorder.limit_reached):
                self.recorder.finish(self.step,self.data,images if images is not None else self.capture(),success,'stable_success' if success else 'frame_limit');self.reset()
            self.frames.publish_timing(self.timing.snapshot());self.ui.update(workspace_effective=self.bridge.workspace_status())
            self.deadline+=1/self.sim['control_hz'];delay=self.deadline-time.monotonic()
            if delay>0:time.sleep(delay)
            elif delay<-.25:self.deadline=time.monotonic()
    def close(self):
        errors=[]
        if self.bridge:self.bridge.set_enabled(False)
        if self.recorder:
            try:
                if self.recorder.active:self.recorder.finish(self.step,self.data,self.capture(),False,'shutdown')
            except Exception as exc:errors.append(str(exc))
        for resource in (self.ui.replays,self.recorder,self.bridge,self.publisher,self.renderer,self.ui.previews,self.env):
            if resource:
                try:(resource.stop if resource is self.bridge else resource.close)()
                except Exception as exc:errors.append(str(exc))
        if self.server:self.server.shutdown();self.server.server_close()
        if errors:raise RuntimeError('; '.join(errors))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default='dexmimicgen.yaml');p.add_argument('--task');p.add_argument('--mode',choices=['teleop','viewer'],default='teleop')
    p.add_argument('--port',type=int,default=int(os.environ.get('DEX_TELEOP_BROWSER_PORT','8080')))
    p.add_argument('--input-port',type=int);p.add_argument('--steps',type=int,default=0)
    p.add_argument('--timing-window',type=int,default=int(os.environ.get('TIMING_WINDOW','120')))
    p.add_argument('--output-dir',type=Path,default=WORKSPACE/'logs/dexmimicgen-live');p.add_argument('--data-root',type=Path,default=DATA_ROOT)
    args=p.parse_args()
    if args.steps<0 or not 1024<=args.port<=65535 or (args.input_port and not 1024<=args.input_port<=65535):p.error('Invalid steps or port')
    debug=os.environ.get('DEBUG_MODE','false').lower()
    if debug not in ('0','1','false','true'):p.error('DEBUG_MODE must be true or false')
    args.debug=debug in ('1','true');args.output_dir=args.output_dir.resolve();args.output_dir.mkdir(parents=True,exist_ok=True)
    path=resolve_server_config(args.config);os.environ['DEX_TELEOP_SERVER_CONFIG']=str(path)
    config=load_server_config(path)
    config['control']['status_path']=str(args.output_dir/'teleop-status.json')
    if args.input_port:config['network']['bind']=f'tcp://127.0.0.1:{args.input_port}'
    runtime=Runtime(args,config)
    def stop(*_):runtime.stopping=True
    old={sig:signal.signal(sig,stop) for sig in (signal.SIGTERM,signal.SIGINT)}
    try:runtime.start();runtime.run()
    finally:
        try:runtime.close()
        finally:
            for sig,handler in old.items():signal.signal(sig,handler)
if __name__=='__main__':main()
