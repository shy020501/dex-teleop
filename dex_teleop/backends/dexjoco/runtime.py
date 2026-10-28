"""Official DexJoCo tasks behind the shared Windows and browser interfaces."""
import argparse
import copy
import math
import os
from pathlib import Path
import queue
import signal
import time
from dex_teleop.paths import WORKSPACE
from dex_teleop.core.config import load_server_config,resolve_server_config
from dex_teleop.core.step_timing import RollingStepTiming
from dex_teleop.dashboard import state
from dex_teleop.dashboard.http import LatestFrame,serve_viewer
from .assets import AssetDownloads
from .paths import DATA_ROOT,SOURCE_REVISION
from .task_info import task_catalog,task_info


class Backend:
    name='dexjoco'
    default_task='click_mouse'
    def __init__(self,data_root=DATA_ROOT):
        self.data_root=Path(data_root).resolve();self.data_root.mkdir(parents=True,exist_ok=True)
        self.assets=AssetDownloads()
    def tasks(self):return task_catalog(self.assets)


def simulation_settings(config):
    result=dict(physics_dt=.002,control_hz=50.,sample_hz=25.,preview_hz=25.,max_episode_frames=1800,max_buffer_mb=6144,seed=0)
    result.update(config.get('simulation',{}))
    for name in ('physics_dt','control_hz','sample_hz','preview_hz'):
        value=result[name]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('Invalid simulation setting: '+name)
    if result['physics_dt']>.01:raise ValueError('Physics timestep must not exceed0.01s')
    for stride in (1/result['control_hz']/result['physics_dt'],result['control_hz']/result['sample_hz']):
        if stride<1 or not math.isclose(stride,round(stride),abs_tol=1e-8):
            raise ValueError('Policy rate must divide Physics rate, and sampling rate must divide policy rate')
    if result['preview_hz']>result['control_hz']:raise ValueError('Preview rate must not exceed policy rate')
    for name,lo,hi in [('max_episode_frames',2,10000),('max_buffer_mb',16,16384)]:
        if type(result[name]) is not int or not lo<=result[name]<=hi:raise ValueError(f'{name} must be in {lo}..{hi}')
    return result


class Runtime:
    def __init__(self,args,config):
        self.args,self.config=args,config;self.settings=simulation_settings(config);self.render_config=dict(config.get('render',{}))
        self.stopping=False;self.completed_steps=0;self.replay_paused=False
        self.scene=self.bridge=self.renderer=self.recorder=self.publisher=self.server=None
        self.ui=state.install_dashboard(state.Dashboard(backend=Backend(args.data_root),state_file=args.output_dir/'settings.json',debug_mode=args.debug),args.task)
        self.ui.update(simulator='dexjoco',success_mode='automatic',outcome_request=None,pose_reference='end_effector',
            execution_mode='serial',max_episode_frames=self.settings['max_episode_frames'],sample_hz=self.settings['sample_hz'])
        self.frames=LatestFrame();self.timing=RollingStepTiming(args.timing_window)
    @property
    def step(self):return self.scene.physics_step
    def start(self):
        from .rendering import FramePublisher
        self.server=serve_viewer(self.frames,self.args.port)
        self.publisher=FramePublisher(self.frames,self.ui.previews,self.render_config.get('jpeg_quality',75))
        self.load(self.ui.snapshot()['task'])
        print(f'[DexJoCo] Browser: http://127.0.0.1:{self.server.server_address[1]}; input: {self.config["network"]["bind"]}; task={self.scene.task}',flush=True)
    def dispose_scene(self):
        errors=[]
        for name in ('bridge','recorder','renderer','scene'):
            resource=getattr(self,name)
            if resource is not None:
                try:(resource.stop if name=='bridge' else resource.close)()
                except Exception as exc:errors.append(str(exc))
                finally:setattr(self,name,None)
        if self.ui.replays is not None:
            try:self.ui.replays.close()
            except Exception as exc:errors.append(str(exc))
            finally:self.ui.replays=None
        if errors:raise RuntimeError('; '.join(errors))
    def load(self,task):
        from .scene import Scene
        from .control import Bridge
        from .rendering import Renderer
        from .recording import RecordingSession
        from .replay import ReplayJobs
        task_info(task)
        self.ui.update(ready=False,phase='loading',workspace_ready=False,error=None)
        self.dispose_scene();self.scene=Scene(task,self.settings)
        self.renderer=Renderer(self.scene,self.render_config);self.bridge=Bridge(self.scene,self.config)
        self.bridge.apply_workspace(self.ui.snapshot()['workspace'])
        if self.args.mode=='teleop':self.bridge.start()
        self.bridge.set_enabled(not self.args.debug and self.args.mode=='teleop')
        launch=dict(task=task,physics_dt=self.settings['physics_dt'],render=self.render_config,
            simulation=copy.deepcopy(self.settings),source_revision=SOURCE_REVISION,sides=self.scene.sides,
            camera_mapping={key:self.scene.model.camera(index).name for key,index in self.scene.cameras.items()})
        self.recorder=RecordingSession(self.ui,self.scene.model,task,launch,sample_hz=self.settings['sample_hz'],
            max_frames=self.settings['max_episode_frames'],max_buffer_mb=self.settings['max_buffer_mb'])
        self.recorder.scene=self.scene
        self.scene.before_step=lambda:self.recorder.note_control(self.step,self.scene.data,self.bridge.source)
        self.scene.after_step=self.after_physics
        self.ui.replays=ReplayJobs(self.ui.store,self.args.output_dir/'replay-cache')
        self.ui._pose_receiver=self.bridge.receiver;self.ui.previews.configure(list(self.scene.cameras))
        self.ui.update(task=task,enabled_sides=self.scene.sides,ready=True,phase='debug' if self.args.debug else 'idle',
            recording=False,frames=0,sim_seconds=0.,success=False,outcome_request=None,workspace_ready=not self.args.debug,
            workspace_pending=False,workspace_effective=self.bridge.workspace_status(),action_spec=self.bridge.action_spec)
        self.ui.persist();self.publisher.reset();self.capture();self.reset_schedule()
    def after_physics(self):
        self.completed_steps+=1;self.bridge.after_physics_step(self.settings['physics_dt'])
        # Timestamp each actual mj_step; sleeping/rendering is included between steps.
        self.timing.record(self.settings['physics_dt'],time.monotonic())
    def reset_schedule(self):
        self.next_preview=float(self.scene.data.time)+1/self.settings['preview_hz']
        self.deadline=time.monotonic();self.timing.reset(self.deadline)
    def capture(self):
        cameras=self.renderer.capture()
        self.publisher.offer(cameras,self.step,simulator='dexjoco',task=self.scene.task,debug_mode=self.args.debug,execution_mode='serial')
        return cameras
    def reset(self):
        self.ui.update(ready=False,phase='resetting');self.bridge.set_enabled(False);self.scene.reset();self.bridge.reset()
        self.bridge.set_enabled(self.args.mode=='teleop' and not self.args.debug);self.publisher.reset()
        self.ui.update(ready=True,phase='saving' if self.recorder.saving else 'idle',outcome_request=None,workspace_effective=self.bridge.workspace_status())
        self.reset_schedule();return self.capture()
    def commands(self):
        while True:
            try:command=self.ui.commands.get_nowait()
            except queue.Empty:break
            try:
                action=command['action']
                if self.recorder.saving:
                    if action=='toggle' and self.recorder.error:self.recorder.retry();continue
                    raise ValueError('Wait until the previous episode has finished saving')
                if action=='toggle':
                    if self.recorder.active:
                        self.recorder.finish(self.step,self.scene.data,self.capture(),False,'manual_stop');self.reset()
                    else:
                        cameras=self.reset();self.recorder.start(self.step,self.scene.data,cameras)
                elif action=='reset':
                    if self.recorder.active:self.recorder.finish(self.step,self.scene.data,self.capture(),False,'environment_reset')
                    self.reset()
                elif action=='task':
                    if self.recorder.active:raise ValueError('Finish recording before switching tasks')
                    previous=self.ui.snapshot();self.ui.update(phase='switching',ready=False,folder=command['task'])
                    try:self.load(command['task'])
                    except Exception:
                        self.ui.update(folder=previous['folder']);self.load(previous['task']);raise
                elif action=='workspace':
                    self.bridge.apply_workspace(command['settings']);self.ui.update(workspace=command['settings'],workspace_pending=False,workspace_effective=self.bridge.workspace_status());self.ui.persist()
                else:raise ValueError('Unsupported command: '+action)
            except Exception as exc:
                self.ui.update(error=str(exc),workspace_pending=False);print(f'[DexJoCo] Command failed: {exc}',flush=True)
    def service_replay(self):
        jobs=self.ui.replays
        if jobs.busy:
            if not self.replay_paused:
                self.bridge.set_enabled(False);self.ui.update(phase='replay');self.replay_paused=True
            if jobs.needs_service:jobs.start_pending()
            jobs.snapshot(jobs.job['id']);return True
        if self.replay_paused:
            self.replay_paused=False;self.bridge.set_enabled(self.args.mode=='teleop' and not self.args.debug)
            self.ui.update(phase='idle');self.reset_schedule()
        return False
    def run(self):
        stride=round(1/self.settings['sample_hz']/self.settings['physics_dt']);last_status=0.
        while not self.stopping and (not self.args.steps or self.completed_steps<self.args.steps):
            self.recorder.poll();self.commands()
            if self.args.debug or self.service_replay():time.sleep(.02);continue
            if time.monotonic()-last_status>=.1:
                settings=self.ui.snapshot();self.bridge.haptic_enabled=settings['haptic_enabled']
                self.bridge.haptic_encoder.set_force_range(settings['haptic_force_min_n'],settings['haptic_force_max_n'])
                self.bridge.haptic_encoder.set_max_power(settings['haptic_max_power']);last_status=time.monotonic()
                self.ui.update(workspace_effective=self.bridge.workspace_status())
            action=self.bridge.update(1/self.settings['control_hz'])
            if self.bridge.consume_environment_reset():
                if self.recorder.active:self.recorder.finish(self.step,self.scene.data,self.capture(),False,'environment_reset')
                self.reset();continue
            substeps=self.scene.n_substeps
            if self.args.steps:substeps=min(substeps,self.args.steps-self.completed_steps)
            self.recorder.note_policy(self.step,action,substeps)
            result=self.scene.step(action,substeps)
            sample=self.recorder.active and self.step%stride==0
            preview=self.scene.data.time+1e-10>=self.next_preview
            terminal=result['success'] or result['terminated'] or result['truncated']
            if sample or preview or (terminal and self.recorder.active):
                cameras=self.capture();self.next_preview=float(self.scene.data.time)+1/self.settings['preview_hz']
                if sample:self.recorder.append(self.step,self.scene.data,cameras)
                if terminal and self.recorder.active:
                    self.recorder.finish(self.step,self.scene.data,cameras,result['success'],'task_success' if result['success'] else 'task_terminated');self.reset()
                elif self.recorder.limit_reached:self.recorder.finish(self.step,self.scene.data,cameras,False,'frame_limit');self.reset()
            if terminal and self.step>0 and not self.recorder.active:self.reset()
            self.frames.publish_timing(self.timing.snapshot())
            self.deadline+=substeps*self.settings['physics_dt'];remaining=self.deadline-time.monotonic()
            if remaining>0:time.sleep(remaining)
            elif remaining<-.25:self.deadline=time.monotonic()
    def close(self):
        errors=[]
        if self.bridge is not None:self.bridge.set_enabled(False)
        if self.recorder is not None and self.recorder.active:
            try:self.recorder.finish(self.step,self.scene.data,self.capture(),False,'shutdown')
            except Exception as exc:errors.append(str(exc))
        try:self.dispose_scene()
        except Exception as exc:errors.append(str(exc))
        for resource in (self.publisher,self.ui.previews):
            if resource is not None:
                try:resource.close()
                except Exception as exc:errors.append(str(exc))
        if self.server:self.server.shutdown();self.server.server_close()
        if errors:raise RuntimeError('; '.join(errors))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='dexjoco.yaml');parser.add_argument('--task')
    parser.add_argument('--mode',choices=('teleop','viewer'),default='teleop')
    parser.add_argument('--port',type=int,default=int(os.environ.get('DEX_TELEOP_BROWSER_PORT','8080')))
    parser.add_argument('--input-port',type=int);parser.add_argument('--steps',type=int,default=0)
    parser.add_argument('--timing-window',type=int,default=int(os.environ.get('TIMING_WINDOW','120')))
    parser.add_argument('--output-dir',type=Path,default=WORKSPACE/'logs/dexjoco-live')
    parser.add_argument('--data-root',type=Path,default=DATA_ROOT,help='Server-side dataset root; never editable in browser')
    args=parser.parse_args()
    if args.steps<0 or not 1024<=args.port<=65535 or (args.input_port and not 1024<=args.input_port<=65535):parser.error('Invalid steps or port')
    debug=os.environ.get('DEBUG_MODE','false').lower()
    if debug not in ('0','1','false','true'):parser.error('DEBUG_MODE must be true or false')
    args.debug=debug in ('1','true');args.output_dir=args.output_dir.resolve();args.output_dir.mkdir(parents=True,exist_ok=True)
    config_path=resolve_server_config(args.config);os.environ['DEX_TELEOP_SERVER_CONFIG']=str(config_path)
    config=load_server_config(config_path)
    if args.input_port:config['network']['bind']=f'tcp://127.0.0.1:{args.input_port}'
    config['control']['status_path']=str(args.output_dir/'teleop-status.json')
    runtime=Runtime(args,config)
    def stop(signum,frame):runtime.stopping=True;print('[DexJoCo] Stop requested',flush=True)
    previous={number:signal.signal(number,stop) for number in (signal.SIGINT,signal.SIGTERM)}
    try:runtime.start();runtime.run()
    finally:
        try:runtime.close()
        finally:
            for number,handler in previous.items():signal.signal(number,handler)


if __name__=='__main__':main()
