from django.urls import path
from .api import ViolationDetectView
from .api_object import ObjectDetectView
from .api_db import VisionAIDBAPI
urlpatterns = [
    path('violations_detect_service/', ViolationDetectView.as_view(), name='violation-detect'),
    path('object_detect_service/', ObjectDetectView.as_view(), name='object-detect'),
    path('vision_ai_db_service/', VisionAIDBAPI.as_view(), name='vision-ai-db'),
]