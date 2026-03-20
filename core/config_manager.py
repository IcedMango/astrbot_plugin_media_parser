"""配置管理模块，负责默认值处理、类型转换与配置兜底。"""
import os
import tempfile
from typing import List

from .logger import logger

from .constants import Config
from .downloader.utils import check_cache_dir_available
from .parser.platform import (
    BilibiliParser,
    DouyinParser,
    KuaishouParser,
    WeiboParser,
    XiaohongshuParser,
    XiaoheiheParser,
    TwitterParser
)


BILIBILI_QUALITY_MAP = {
    "不限制": 0,
    "4K": 120,
    "1080P60": 116,
    "1080P+": 112,
    "1080P": 80,
    "720P": 64,
    "480P": 32,
    "360P": 16,
}

TELEGRAM_TRANSCODE_PROFILES = {
    "画质优先": {
        "preset": "medium",
        "crf": 20,
        "audio_bitrate_kbps": 192,
    },
    "均衡": {
        "preset": "veryfast",
        "crf": 23,
        "audio_bitrate_kbps": 128,
    },
    "速度": {
        "preset": "superfast",
        "crf": 26,
        "audio_bitrate_kbps": 96,
    },
}


class ConfigManager:

    """配置读取门面，向业务层提供类型安全的配置访问。"""
    def __init__(self, config: dict):
        """初始化配置管理器

        Args:
            config: 原始配置字典

        Raises:
            ValueError: 没有启用任何解析器时
        """
        self._config = config
        self.bilibili_parser = None
        self._parse_config()

    def _parse_config(self):
        """解析配置"""
        self.is_auto_pack = self._config.get("is_auto_pack", False)
        
        trigger_settings = self._config.get("trigger_settings", {})
        self.is_auto_parse = trigger_settings.get("is_auto_parse", True)
        self.trigger_keywords = trigger_settings.get(
            "trigger_keywords",
            ["视频解析", "解析视频"]
        )
        
        permissions = self._config.get("permissions", {})
        whitelist = permissions.get("whitelist", {})
        blacklist = permissions.get("blacklist", {})
        self.admin_id = str(
            permissions.get("admin_id", self._config.get("admin_id", "")) or ""
        ).strip()
        
        self.whitelist_enable = whitelist.get("enable", False)
        whitelist_user = whitelist.get("user", [])
        self.whitelist_user = self._normalize_id_list(whitelist_user)
        if self.admin_id and self.admin_id not in self.whitelist_user:
            self.whitelist_user.append(self.admin_id)
        whitelist_group = whitelist.get("group", [])
        self.whitelist_group = self._normalize_id_list(whitelist_group)
        
        self.blacklist_enable = blacklist.get("enable", False)
        blacklist_user = blacklist.get("user", [])
        self.blacklist_user = self._normalize_id_list(blacklist_user)
        blacklist_group = blacklist.get("group", [])
        self.blacklist_group = self._normalize_id_list(blacklist_group)
        
        text_settings = self._config.get("text_settings", {})
        self.enable_opening_msg = text_settings.get("enable_opening_msg", True)
        self.opening_msg_content = text_settings.get(
            "opening_msg_content",
            "流媒体解析bot为您服务 ٩( 'ω' )و"
        )
        self.enable_text_metadata = text_settings.get("enable_text_metadata", True)
        hot_comment_settings = text_settings.get("hot_comment_settings", {})
        if not isinstance(hot_comment_settings, dict):
            hot_comment_settings = {}
        self.hot_comment_count = self._parse_non_negative_int(
            hot_comment_settings.get("hot_comment_count", 0),
            0
        )
        self.hot_comment_bilibili = bool(
            hot_comment_settings.get("enable_bilibili", True)
        )
        self.hot_comment_weibo = bool(
            hot_comment_settings.get("enable_weibo", True)
        )
        self.hot_comment_xiaohongshu = bool(
            hot_comment_settings.get("enable_xiaohongshu", True)
        )
        if not self.enable_text_metadata:
            self.hot_comment_count = 0
        
        video_size_settings = self._config.get("video_size_settings", {})
        self.max_video_size_mb = video_size_settings.get("max_video_size_mb", 0.0)
        large_video_threshold_mb = video_size_settings.get(
            "large_video_threshold_mb",
            Config.MAX_LARGE_VIDEO_THRESHOLD_MB
        )
        if large_video_threshold_mb > 0:
            large_video_threshold_mb = min(
                large_video_threshold_mb,
                Config.MAX_LARGE_VIDEO_THRESHOLD_MB
            )
        self.large_video_threshold_mb = large_video_threshold_mb
        
        download_settings = self._config.get("download_settings", {})
        configured_cache_dir = download_settings.get("cache_dir", "").strip()
        
        if not configured_cache_dir or configured_cache_dir == "/app/sharedFolder/video_parser/cache":
            if os.path.exists('/.dockerenv'):
                self.cache_dir = "/app/sharedFolder/video_parser/cache"
            else:
                self.cache_dir = os.path.join(tempfile.gettempdir(), "astrbot_media_parser_cache")
        else:
            self.cache_dir = configured_cache_dir
        self.pre_download_all_media = download_settings.get(
            "pre_download_all_media",
            False
        )
        self.max_concurrent_downloads = download_settings.get(
            "max_concurrent_downloads",
            Config.DOWNLOAD_MANAGER_MAX_CONCURRENT
        )
        self.download_retry_count = self._parse_non_negative_int(
            download_settings.get(
                "download_retry_count",
                Config.DEFAULT_DOWNLOAD_RETRY_COUNT
            ),
            Config.DEFAULT_DOWNLOAD_RETRY_COUNT
        )
        
        if self.pre_download_all_media:
            if not check_cache_dir_available(self.cache_dir):
                logger.warning(
                    f"预下载模式已启用，但缓存目录不可用: {self.cache_dir}，"
                    f"将自动降级为禁用预下载模式"
                )
                self.pre_download_all_media = False

        cookie_settings = self._config.get("cookie_settings", {})
        bilibili_cookie_settings = {}
        if isinstance(cookie_settings, dict):
            bilibili_cookie_settings = cookie_settings.get("bilibili", {})
        if not isinstance(bilibili_cookie_settings, dict):
            bilibili_cookie_settings = {}
        if not bilibili_cookie_settings:
            legacy_settings = self._config.get("bilibili_cookie_settings", {})
            if isinstance(legacy_settings, dict):
                bilibili_cookie_settings = legacy_settings

        self.bilibili_use_cookie_for_parsing = bool(
            bilibili_cookie_settings.get("use_cookie_for_parsing", False)
        )
        if self.bilibili_use_cookie_for_parsing:
            self.bilibili_cookie = str(
                bilibili_cookie_settings.get("cookie", "") or ""
            ).strip()
            max_quality_label = str(
                bilibili_cookie_settings.get("max_quality", "不限制") or "不限制"
            ).strip()
            self.bilibili_max_quality = BILIBILI_QUALITY_MAP.get(
                max_quality_label,
                0
            )
            self.bilibili_enable_admin_assist_on_expire = bool(
                bilibili_cookie_settings.get("enable_admin_assist_on_expire", False)
            )
            self.bilibili_admin_reply_timeout_minutes = self._parse_positive_int(
                bilibili_cookie_settings.get("admin_reply_timeout_minutes", 1440),
                1440
            )
            self.bilibili_admin_request_cooldown_minutes = self._parse_positive_int(
                bilibili_cookie_settings.get("admin_request_cooldown_minutes", 1440),
                1440
            )
        else:
            self.bilibili_cookie = ""
            self.bilibili_max_quality = 0
            self.bilibili_enable_admin_assist_on_expire = False
            self.bilibili_admin_reply_timeout_minutes = 1440
            self.bilibili_admin_request_cooldown_minutes = 1440

        self.bilibili_cookie_feature_requested = self.bilibili_use_cookie_for_parsing
        self.bilibili_cookie_runtime_enabled = bool(
            self.bilibili_use_cookie_for_parsing and self.pre_download_all_media
        )
        runtime_file_name = "cookie.json"
        core_dir = os.path.dirname(os.path.abspath(__file__))
        cookie_dir = os.path.join(
            core_dir,
            "parser",
            "runtime_manager",
            "bilibili"
        )
        self.bilibili_cookie_runtime_file = os.path.join(
            cookie_dir,
            runtime_file_name
        )
        if self.bilibili_use_cookie_for_parsing:
            try:
                os.makedirs(cookie_dir, exist_ok=True)
            except Exception as e:
                logger.warning(
                    f"B站Cookie运行时目录不可用，将回退到缓存目录保存: {e}"
                )
                fallback_cookie_dir = os.path.join(
                    self.cache_dir,
                    "runtime_manager",
                    "bilibili"
                )
                self.bilibili_cookie_runtime_file = os.path.join(
                    fallback_cookie_dir,
                    runtime_file_name
                )
        if (
            self.bilibili_cookie_feature_requested and
            not self.bilibili_cookie_runtime_enabled
        ):
            logger.warning(
                "检测到已开启“是否携带Cookie解析视频”，但预下载未启用或不可用，"
                "将旁路B站Cookie与协助登录流程，直接使用无Cookie直链模式。"
            )
        
        parser_enable_settings = self._config.get("parser_enable_settings", {})
        self.enable_bilibili = parser_enable_settings.get("enable_bilibili", True)
        self.enable_douyin = parser_enable_settings.get("enable_douyin", True)
        self.enable_kuaishou = parser_enable_settings.get(
            "enable_kuaishou",
            True
        )
        self.enable_weibo = parser_enable_settings.get(
            "enable_weibo",
            True
        )
        self.enable_xiaohongshu = parser_enable_settings.get(
            "enable_xiaohongshu",
            True
        )
        self.enable_xiaoheihe = parser_enable_settings.get(
            "enable_xiaoheihe",
            True
        )
        self.enable_twitter = parser_enable_settings.get("enable_twitter", True)
        
        proxy_settings = self._config.get("proxy_settings", {})
        self.proxy_addr = proxy_settings.get("proxy_addr", "")
        
        xiaoheihe_proxy = proxy_settings.get("xiaoheihe", {})
        self.xiaoheihe_use_video_proxy = xiaoheihe_proxy.get("video", False)
        
        twitter_proxy = proxy_settings.get("twitter", {})
        self.twitter_use_parse_proxy = twitter_proxy.get("parse", False)
        self.twitter_use_image_proxy = twitter_proxy.get("image", False)
        self.twitter_use_video_proxy = twitter_proxy.get("video", False)

        telegram_settings = self._config.get("telegram", {})
        if not isinstance(telegram_settings, dict):
            telegram_settings = {}
        self.telegram_send_media_group = bool(
            telegram_settings.get("send_media_group", False)
        )
        self.telegram_video_faststart = bool(
            telegram_settings.get("video_faststart", True)
        )
        self.telegram_video_transcode = bool(
            telegram_settings.get("video_transcode", False)
        )
        transcode_settings = telegram_settings.get("transcode_settings", {})
        if not isinstance(transcode_settings, dict):
            transcode_settings = {}
        profile = str(
            transcode_settings.get("profile", "均衡") or "均衡"
        ).strip()
        if profile not in TELEGRAM_TRANSCODE_PROFILES:
            logger.warning(
                f"Telegram 转码预设非法，回退为均衡: {profile}"
            )
            profile = "均衡"
        self.telegram_transcode_profile = profile
        self.telegram_transcode_profile_config = TELEGRAM_TRANSCODE_PROFILES[
            profile
        ].copy()
        self.telegram_read_timeout = self._parse_positive_int(
            telegram_settings.get("read_timeout", 900),
            900
        )
        self.telegram_write_timeout = self._parse_positive_int(
            telegram_settings.get("write_timeout", 900),
            900
        )
        self.telegram_connect_timeout = self._parse_positive_int(
            telegram_settings.get("connect_timeout", 30),
            30
        )
        self.telegram_pool_timeout = self._parse_positive_int(
            telegram_settings.get("pool_timeout", 30),
            30
        )
        
        self.debug_mode = self._config.get("debug", False)
        if self.debug_mode:
            import logging
            logger.setLevel(logging.DEBUG)
            logger.debug("Debug模式已启用")

    @staticmethod
    def _parse_positive_int(value, default: int) -> int:
        """将配置值解析为正整数，非法值回退为默认值。"""
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, int(default))

    @staticmethod
    def _parse_non_negative_int(value, default: int) -> int:
        """将配置值解析为非负整数，非法值回退为默认值。"""
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return max(0, int(default))

    @staticmethod
    def _normalize_id_list(values) -> List[str]:
        """将管理员或白名单配置规范化为字符串 ID 集合。"""
        if not isinstance(values, list):
            return []
        normalized: List[str] = []
        seen = set()
        for value in values:
            if value is None:
                continue
            value_str = str(value).strip()
            if not value_str or value_str in seen:
                continue
            seen.add(value_str)
            normalized.append(value_str)
        return normalized


    def _effective_hot_comment_count(self, enabled: bool) -> int:
        """根据开关状态返回实际生效的热评条数。"""
        if not self.enable_text_metadata:
            return 0
        if not enabled:
            return 0
        return self.hot_comment_count

    def create_parsers(self) -> List:
        """创建解析器列表

        Returns:
            解析器列表

        Raises:
            ValueError: 没有启用任何解析器时
        """
        parsers = []
        bilibili_hot_comment_count = self._effective_hot_comment_count(
            self.hot_comment_bilibili
        )
        weibo_hot_comment_count = self._effective_hot_comment_count(
            self.hot_comment_weibo
        )
        xiaohongshu_hot_comment_count = self._effective_hot_comment_count(
            self.hot_comment_xiaohongshu
        )
        
        if self.enable_bilibili:
            self.bilibili_parser = BilibiliParser(
                cookie_runtime_enabled=self.bilibili_cookie_runtime_enabled,
                configured_cookie=self.bilibili_cookie,
                max_quality=self.bilibili_max_quality,
                admin_assist_enabled=self.bilibili_enable_admin_assist_on_expire,
                admin_reply_timeout_minutes=self.bilibili_admin_reply_timeout_minutes,
                admin_request_cooldown_minutes=self.bilibili_admin_request_cooldown_minutes,
                credential_path=self.bilibili_cookie_runtime_file,
                hot_comment_count=bilibili_hot_comment_count
            )
            parsers.append(self.bilibili_parser)
        if self.enable_douyin:
            parsers.append(DouyinParser())
        if self.enable_kuaishou:
            parsers.append(KuaishouParser())
        if self.enable_weibo:
            parsers.append(
                WeiboParser(hot_comment_count=weibo_hot_comment_count)
            )
        if self.enable_xiaohongshu:
            parsers.append(
                XiaohongshuParser(
                    hot_comment_count=xiaohongshu_hot_comment_count
                )
            )
        if self.enable_xiaoheihe:
            parsers.append(XiaoheiheParser(
                use_video_proxy=self.xiaoheihe_use_video_proxy,
                proxy_url=self.proxy_addr if self.proxy_addr else None
            ))
        if self.enable_twitter:
            parsers.append(TwitterParser(
                use_parse_proxy=self.twitter_use_parse_proxy,
                use_image_proxy=self.twitter_use_image_proxy,
                use_video_proxy=self.twitter_use_video_proxy,
                proxy_url=self.proxy_addr if self.proxy_addr else None
            ))
        
        if not parsers:
            raise ValueError(
                "至少需要启用一个视频解析器。"
                "请检查配置中的 parser_enable_settings 设置。"
            )
        
        return parsers
