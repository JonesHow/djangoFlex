import cv2
import time
import threading
import logging
from ..models import VideoCapConfig, CurrentVideoClip
from django.db import transaction
from django.utils import timezone
import redis
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from datetime import timedelta
import subprocess

logger = logging.getLogger(__name__)

class VideoCapService:
    def __init__(self):
        self.configs = {}
        self.caps = {}
        self.running = {}
        self.capture_threads = {}
        self.max_reconnect_attempts = 5
        self.reconnect_timeout = 5 
        self.redis_client = redis.StrictRedis(host='localhost', port='6379', db=0)
        self.executor = ThreadPoolExecutor(max_workers=10)  # Adjust max_workers as needed
        self.fps = 15  # Set the desired FPS
        self.frame_interval = 1 / self.fps  # Calculate frame interval based on FPS
        self.video_clip_duration = 30  # Set the video clip duration to 30 seconds
        self.video_clip_dir = os.path.join('tmp', 'video_clip')
        os.makedirs(self.video_clip_dir, exist_ok=True)
        self._load_configs()
        logger.info("VideoCapService initialized")

    @staticmethod
    @transaction.atomic
    def reset_video_cap_system():
        from ..models import VideoCapConfig, CurrentVideoClip
        try:
            CurrentVideoClip.objects.all().delete()
            VideoCapConfig.objects.update(is_active=False)
            redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)
            for key in redis_client.keys("video_cap_service:current_image:*"):
                redis_client.delete(key)
            logger.info("Video capture system reset completed")
        except Exception as e:
            logger.error(f"Error resetting video capture system: {str(e)}")

    def _load_configs(self):
        try:
            for config in VideoCapConfig.objects.filter(is_active=True):
                self.configs[config.rtmp_url] = config
                self.running[config.rtmp_url] = False
            logger.info("Configurations loaded successfully")
        except Exception as e:
            logger.error(f"Error loading configurations: {str(e)}")

    def start_server(self, rtmp_url):
        if rtmp_url in self.running and self.running[rtmp_url]:
            return False, "Server already running"

        config, created = VideoCapConfig.objects.get_or_create(rtmp_url=rtmp_url)
        if created:
            config.name = f"Config_{config.id}"
            config.save()

        self.configs[rtmp_url] = config
        self.running[rtmp_url] = True
        config.is_active = True
        config.save()

        self._initialize_capture(rtmp_url)
        self.capture_threads[rtmp_url] = threading.Thread(target=self._capture_loop, args=(rtmp_url,))
        self.capture_threads[rtmp_url].start()

        logger.info(f"Server started for {rtmp_url}")
        return True, "Server started successfully"

    def stop_server(self, rtmp_url):
        if rtmp_url not in self.running or not self.running[rtmp_url]:
            return False, "Server not running"

        self.running[rtmp_url] = False
        if rtmp_url in self.capture_threads:
            self.capture_threads[rtmp_url].join()
            del self.capture_threads[rtmp_url]

        if rtmp_url in self.caps:
            self.caps[rtmp_url].release()
            del self.caps[rtmp_url]

        with transaction.atomic():
            config = self.configs[rtmp_url]
            config.is_active = False
            config.save()

        self.redis_client.delete(f"video_cap_service:current_image:{rtmp_url}")
        logger.info(f"Server stopped for {rtmp_url}")
        return True, "Server stopped successfully"

    def check_server_status(self, rtmp_url):
        status = self.running.get(rtmp_url, False)
        return status

    def _initialize_capture(self, rtmp_url):
        if rtmp_url in self.caps and self.caps[rtmp_url] is not None:
            self.caps[rtmp_url].release()
        
        cap_source = 0 if rtmp_url == '0' else rtmp_url
        try:
            self.caps[rtmp_url] = cv2.VideoCapture(cap_source)
            self.caps[rtmp_url].set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.caps[rtmp_url].set(cv2.CAP_PROP_FPS, self.fps)  # Set the FPS
            if not self.caps[rtmp_url].isOpened():
                raise Exception("Failed to open video capture")
        except Exception as e:
            logger.error(f"Failed to initialize video capture for {rtmp_url}: {str(e)}")
            self.caps[rtmp_url] = None

    def _capture_loop(self, rtmp_url):
        config = self.configs[rtmp_url]
        reconnect_start_time = None
        reconnect_attempts = 0
        last_frame_time = time.time()
        video_clip_start_time = time.time()

        # Initialize HLS output
        hls_output_dir = os.path.join(self.video_clip_dir, f"{rtmp_url.split('/')[-1]}_hls")
        os.makedirs(hls_output_dir, exist_ok=True)
        hls_output = os.path.join(hls_output_dir, 'index.m3u8')

        ffmpeg_command = [
            'ffmpeg',
            '-y',
            '-i', rtmp_url,
            '-c:v', 'copy',
            '-an',
            '-f', 'hls',
            '-hls_time', '6',
            '-r', '15',
            '-hls_flags', 'second_level_segment_duration',
            '-strftime', '1',
            '-strftime_mkdir', '1',
            '-hls_segment_filename', '%Y%m%d%H%M_%s_%%t.ts',
            hls_output
        ]

        logger.info(f"Starting FFmpeg process for {rtmp_url} with command: {' '.join(ffmpeg_command)}")

        ffmpeg_process = None
        try:
            ffmpeg_process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE)

            def log_stderr(stderr):
                for line in iter(stderr.readline, b''):
                    logger.error(f"FFmpeg error for {rtmp_url}: {line.decode().strip()}")

            # Start a thread to log FFmpeg errors in real-time
            threading.Thread(target=log_stderr, args=(ffmpeg_process.stderr,), daemon=True).start()

            while self.running[rtmp_url]:
                if rtmp_url in self.caps and self.caps[rtmp_url] is not None and self.caps[rtmp_url].isOpened():
                    ret, frame = self.caps[rtmp_url].read()
                    current_time = time.time()

                    if ret:
                        last_frame_time = current_time
                        reconnect_start_time = None
                        reconnect_attempts = 0

                        # Check if it's time to save metadata for a new video clip
                        if current_time - video_clip_start_time >= self.video_clip_duration:
                            self._save_video_clip_metadata(rtmp_url, hls_output, video_clip_start_time, current_time)
                            video_clip_start_time = current_time

                    elif current_time - last_frame_time > 1:
                        if reconnect_start_time is None:
                            reconnect_start_time = current_time
                            reconnect_attempts += 1
                        self._reconnect(rtmp_url)
                else:
                    if reconnect_start_time is None:
                        reconnect_start_time = time.time()
                        reconnect_attempts += 1
                    self._reconnect(rtmp_url)
                
                if reconnect_start_time is not None:
                    elapsed_time = time.time() - reconnect_start_time
                    if elapsed_time > self.reconnect_timeout or reconnect_attempts > self.max_reconnect_attempts:
                        self._set_inactive(rtmp_url)
                        break

        except Exception as e:
            logger.error(f"Error in capture loop for {rtmp_url}: {str(e)}")
        finally:
            # Clean up
            logger.info(f"Closing FFmpeg process for {rtmp_url}")
            if ffmpeg_process:
                ffmpeg_process.terminate()
                ffmpeg_process.wait()
            logger.info(f"FFmpeg process for {rtmp_url} closed")

    def _get_frame_size(self, rtmp_url):
        if rtmp_url in self.caps and self.caps[rtmp_url] is not None:
            ret, frame = self.caps[rtmp_url].read()
            if ret:
                return frame.shape[:2][::-1]  # Returns (width, height)
        return (640, 480)  # Default size if unable to get frame

    def _reconnect(self, rtmp_url):
        if rtmp_url in self.caps and self.caps[rtmp_url] is not None:
            self.caps[rtmp_url].release()
        self.caps[rtmp_url] = None
        time.sleep(1)
        self._initialize_capture(rtmp_url)
        return self.caps[rtmp_url] is not None and self.caps[rtmp_url].isOpened()

    def _set_inactive(self, rtmp_url):
        with transaction.atomic():
            config = self.configs[rtmp_url]
            config.is_active = False
            config.save()

        self.running[rtmp_url] = False
        if rtmp_url in self.caps and self.caps[rtmp_url] is not None:
            self.caps[rtmp_url].release()
        self.caps[rtmp_url] = None

        if rtmp_url in self.capture_threads:
            del self.capture_threads[rtmp_url]

        self.redis_client.delete(f"video_cap_service:current_image:{rtmp_url}")

    async def update_frame(self, rtmp_url, frame):
        if frame is None:
            return
        
        _, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        
        # Update Redis immediately
        await asyncio.to_thread(self.redis_client.set, f"video_cap_service:current_image:{rtmp_url}", frame_bytes)

    def _save_video_clip_metadata(self, rtmp_url, hls_output, start_time, end_time):
        config = self.configs[rtmp_url]
        duration = end_time - start_time

        with transaction.atomic():
            CurrentVideoClip.objects.create(
                config=config,
                clip_path=hls_output,
                start_time=timezone.now() - timedelta(seconds=duration),
                end_time=timezone.now(),
                duration=duration
            )

    def __del__(self):
        for rtmp_url in list(self.running.keys()):
            if self.running[rtmp_url]:
                self.stop_server(rtmp_url)

        for rtmp_url in self.configs.keys():
            self.redis_client.delete(f"video_cap_service:current_image:{rtmp_url}")

        logger.info("VideoCapService destroyed")

    def list_running_threads(self):
        running_threads = []
        for rtmp_url, thread in self.capture_threads.items():
            running_threads.append({
                'rtmp_url': rtmp_url,
                'thread_id': thread.ident,
                'thread_name': thread.name,
                'is_alive': thread.is_alive()
            })
        return running_threads
