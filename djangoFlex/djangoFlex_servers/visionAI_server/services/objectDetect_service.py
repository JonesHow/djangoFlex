import cv2
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from django.utils import timezone
from djangoFlex_servers.videoCap_server.models import VideoCapConfig    
from ..models import DetectedObject, KeyFrame, EntityType
import redis
import numpy as np
import yaml
from django.conf import settings


logger = logging.getLogger(__name__)

class ObjectDetectService:
    def __init__(self):
        self.configs = {}
        self.running = {}
        self.detect_threads = {}
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)
        self._load_configs()
        self._load_entity_types()
        logger.info("ObjectDetectService initialized")

    def _load_configs(self):
        try:
            for config in VideoCapConfig.objects.filter(is_active=True):
                self.configs[config.rtmp_url] = config
                self.running[config.rtmp_url] = False
            logger.info("Configurations loaded successfully")
        except Exception as e:
            logger.error(f"Error loading configurations: {str(e)}")

    def _load_entity_types(self):
        try:
            db_entity_types = EntityType.objects.all()
            if db_entity_types.exists():
                for entity_type in db_entity_types:
                    self.entity_types[entity_type.code] = entity_type
            else:
                with open("djangoFlex_servers/visionAI_server/type_initial_config/entity_type.yaml", 'r') as file:
                    yaml_entity_types = yaml.safe_load(file)
                    self.entity_types = {}
                    logging.info("yaml_entity_types", yaml_entity_types)
                    for entity_type_data in yaml_entity_types:
                        entity_type = EntityType.objects.create(**entity_type_data)
                        self.entity_types[entity_type.code] = entity_type
                logger.info("Entity types loaded successfully")
        except Exception as e:
            logger.error(f"Error loading entity types: {str(e)}")
            self.entity_types = {}

    def start_service(self):
        logger.info("Starting ObjectDetectService")
        for rtmp_url in self.configs.keys():
            self.start_detection(rtmp_url)
        logger.info("ObjectDetectService started successfully")

    def stop_service(self):
        logger.info("Stopping ObjectDetectService")
        for rtmp_url in list(self.running.keys()):
            if self.running[rtmp_url]:
                self.stop_detection(rtmp_url)
        self.executor.shutdown(wait=True)
        logger.info("ThreadPoolExecutor shut down")
        logger.info("ObjectDetectService stopped successfully")

    def start_detection(self, rtmp_url):
        if rtmp_url in self.running and self.running[rtmp_url]:
            logger.info(f"Detection for {rtmp_url} is already running")
            return False, "Detection already running"

        config = self.configs.get(rtmp_url)
        if not config:
            logger.error(f"No configuration found for {rtmp_url}")
            return False, "No configuration found"

        self.running[rtmp_url] = True
        self.detect_threads[rtmp_url] = self.executor.submit(self._detect_loop, rtmp_url)

        logger.info(f"Detection started for {rtmp_url}")
        return True, "Detection started successfully"

    def stop_detection(self, rtmp_url):
        if rtmp_url not in self.running or not self.running[rtmp_url]:
            logger.warning(f"Detection for {rtmp_url} is not running")
            return False, "Detection not running"

        self.running[rtmp_url] = False
        if rtmp_url in self.detect_threads:
            del self.detect_threads[rtmp_url]
            logger.info(f"Detection thread for {rtmp_url} stopped")

        logger.info(f"Detection stopped for {rtmp_url}")
        return True, "Detection stopped successfully"

    def _detect_loop(self, rtmp_url):
        logger.info(f"Detection loop started for {rtmp_url}")
        while self.running[rtmp_url]:
            frame_bytes = self.redis_client.get(f"video_cap_service:current_image:{rtmp_url}")
            if frame_bytes:
                frame = cv2.imdecode(np.frombuffer(frame_bytes, np.uint8), cv2.IMREAD_COLOR)
                self._process_frame(rtmp_url, frame)
            time.sleep(self.configs[rtmp_url].frame_interval)
        logger.info(f"Detection loop ended for {rtmp_url}")

    def _process_frame(self, rtmp_url, frame):
        # Create a KeyFrame
        key_frame = KeyFrame.objects.create(
            frame_time=timezone.now(),
            frame_index=0  # You might want to implement a frame counter
        )

        # Perform object detection
        detected_objects = self._detect_objects(frame)

        # Create DetectedObject instances and save them to the database
        detected_object_instances = []
        for obj in detected_objects:
            entity_type, _ = EntityType.objects.get_or_create(type_name=obj['entity_type'])
            detected_object = DetectedObject(
                frame=key_frame,
                entity_type=entity_type,
                specific_type=obj['entity_type'],
                confidence_score=obj.get('confidence', 1.0),
                bounding_box={
                    'x': obj['x'],
                    'y': obj['y'],
                    'width': obj['width'],
                    'height': obj['height']
                },
                segmentation=obj.get('segmentation', []),
                re_id=obj.get('re_id', -1)
            )

        logger.info(f"Processed frame for {rtmp_url}, detected {len(detected_objects)} objects")

    def _detect_objects(self, frame):
        # This is a placeholder for the actual object detection logic
        # In a real implementation, you would use a pre-trained model here
        # For now, we'll return a dummy object with random position and size
        entity_type = np.random.choice(list(self.entity_types.keys()))
        frame_height, frame_width = frame.shape[:2]
        x = np.random.randint(0, frame_width - 50)  # Ensure object is within frame
        y = np.random.randint(0, frame_height - 50)
        width = np.random.randint(50, min(100, frame_width - x))
        height = np.random.randint(50, min(100, frame_height - y))
        return [{'x': x, 'y': y, 'width': width, 'height': height, 'entity_type': entity_type}]

    def update_config(self, rtmp_url):
        if rtmp_url in self.configs:
            self.configs[rtmp_url] = VideoCapConfig.objects.get(rtmp_url=rtmp_url)
            if self.running[rtmp_url]:
                self.stop_detection(rtmp_url)
                self.start_detection(rtmp_url)
            logger.info(f"Configuration updated for {rtmp_url}")
        else:
            logger.warning(f"No configuration found for {rtmp_url}")

    def __del__(self):
        logger.info("ObjectDetectService destructor called")
        self.stop_service()
        logger.info("ObjectDetectService destroyed")

    def list_running_threads(self):
        running_threads = []
        for rtmp_url, future in self.detect_threads.items():
            running_threads.append({
                'rtmp_url': rtmp_url,
                'is_running': self.running[rtmp_url],
                'is_done': future.done()
            })
        return running_threads