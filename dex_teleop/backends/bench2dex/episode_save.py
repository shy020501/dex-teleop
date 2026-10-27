"""One owned episode buffer, one background HDF5 writer, atomic publication."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
import time


@dataclass(frozen=True)
class PreparedEpisode:
    buffer: object
    metadata: dict
    folder: Path
    filename: str
    publish_lock: object

    def write(self):
        from collector.hdf5_writer import HDF5EpisodeWriter
        from dex_teleop.backends.bench2dex.split_rendering import resolve_recorded_frames
        resolve_recorded_frames(self.buffer.frames)
        metadata = dict(self.metadata, frame_error_count=self.buffer.error_count)
        if self.buffer.error_count:
            metadata['demo_eligible'] = False
        staging = Path(tempfile.mkdtemp(prefix='.saving-', dir=self.folder))
        try:
            writer = HDF5EpisodeWriter(str(staging), '', '', '')
            writer.stream_camera_images = True
            path = Path(writer.write_episode(self.buffer, 0, metadata))
            if self.buffer.frames[0].scene_state is not None:
                import h5py
                from dex_teleop.backends.bench2dex.scene_state import write_replay_state
                with h5py.File(path, 'a') as h:
                    write_replay_state(h, self.buffer.frames, metadata['replay_launch'], metadata['scene_signature'])
            final = self.folder / self.filename
            with self.publish_lock:
                if final.exists():
                    raise FileExistsError(final)
                path.rename(final)
            return str(final)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


class EpisodeSaver:
    """A failed job retains its buffer and frozen metadata for an explicit retry.

    There is deliberately no submission queue: collection is disabled while a
    completed episode is saving. The worker never touches Isaac or UI state.
    """
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='episode-hdf5')
        self.job = self.future = None
        self.started = 0.

    @property
    def pending(self):
        return self.job is not None

    def submit(self, job):
        if self.pending:
            raise RuntimeError('이전 episode 저장을 먼저 완료하세요.')
        self.started = time.monotonic()
        future = self.executor.submit(self._write, job)
        self.job, self.future = job, future

    def retry(self):
        if self.future is None or not self.future.done() or self.future.exception() is None:
            raise RuntimeError('재시도할 저장 오류가 없습니다.')
        self.started = time.monotonic()
        self.future = self.executor.submit(self._write, self.job)

    @staticmethod
    def _write(job):
        result = dict(frames=job.buffer.frame_count, success=job.metadata['success'])
        result['path'] = job.write()
        # Release potentially gigabytes of CPU arrays on this worker too. A
        # failed write skips this block so F7 can retry the original buffer.
        while job.buffer.frames:
            for _ in range(min(16, len(job.buffer.frames))):
                job.buffer.frames.pop()
            time.sleep(0)
        return result

    def poll(self, wait=False):
        if self.future is None or (not wait and not self.future.done()):
            return None
        try:
            result = self.future.result()
        except Exception as exc:
            return {'error': str(exc)}
        result['seconds'] = time.monotonic()-self.started
        self.job = self.future = None
        return result

    def close(self):
        # Normal shutdown/task switch waits for completion before native Kit exit.
        result = self.poll(wait=True)
        self.executor.shutdown(wait=True)
        return result
