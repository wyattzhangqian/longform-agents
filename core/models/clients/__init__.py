"""多模态生成客户端 — 与 ModelRouter 内聚，由 router 产出 config 实例化。

ImageGenClient / VideoGenClient 的 base_url 归一化逻辑在此统一维护，
避免散落 tools/ 导致自定义模型 URL 双拼等问题。
"""

from core.models.clients.image_client import ImageGenClient
from core.models.clients.video_client import VideoGenClient
from core.models.clients.music_client import MusicGenClient

__all__ = ["ImageGenClient", "VideoGenClient", "MusicGenClient"]
