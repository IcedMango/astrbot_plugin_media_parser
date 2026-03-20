"""消息发送封装，统一不同会话场景下的发送行为。"""
import asyncio
from contextlib import ExitStack
import json
import os
from typing import Any, List

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Nodes, Plain, Image, Node, Video, Reply

from .node_builder import is_pure_image_gallery
from ..file_cleaner import cleanup_files
from ..logger import logger


class MessageSender:

    """消息发送器，封装统一的私聊/群聊发送接口。"""
    def __init__(
        self,
        telegram_send_media_group: bool = False,
        telegram_video_faststart: bool = True,
        telegram_video_transcode: bool = False,
        telegram_transcode_profile: str = "均衡",
        telegram_transcode_profile_config: dict[str, Any] | None = None,
        telegram_read_timeout: int = 900,
        telegram_write_timeout: int = 900,
        telegram_connect_timeout: int = 30,
        telegram_pool_timeout: int = 30
    ):
        """初始化消息发送器

        Args:
            无
        """
        self.telegram_send_media_group = telegram_send_media_group
        self.telegram_video_faststart = telegram_video_faststart
        self.telegram_video_transcode = telegram_video_transcode
        self.telegram_transcode_profile = telegram_transcode_profile
        self.telegram_transcode_profile_config = (
            telegram_transcode_profile_config or {}
        )
        self.telegram_read_timeout = telegram_read_timeout
        self.telegram_write_timeout = telegram_write_timeout
        self.telegram_connect_timeout = telegram_connect_timeout
        self.telegram_pool_timeout = telegram_pool_timeout

    def get_sender_info(self, event: AstrMessageEvent) -> tuple:
        """获取发送者信息

        Args:
            event: 消息事件对象

        Returns:
            包含发送者名称和ID的元组 (sender_name, sender_id)
        """
        sender_name = "视频解析bot"
        platform = event.get_platform_name()
        sender_id = event.get_self_id()
        if platform not in ("wechatpadpro", "webchat", "gewechat"):
            try:
                sender_id = int(sender_id)
            except (ValueError, TypeError):
                sender_id = 10000
        return sender_name, sender_id

    async def _notify_send_failure(
        self,
        event: AstrMessageEvent,
        message: str
    ) -> None:
        """尽力向用户补发发送失败提示。"""
        try:
            await event.send(event.plain_result(message))
        except Exception as e:
            logger.warning(f"发送失败提示消息时出错: {e}")

    def _build_telegram_payload(
        self,
        event: AstrMessageEvent
    ) -> tuple[str | None, dict[str, Any]]:
        """构建 Telegram 原生发送接口所需的 payload。"""
        if event.is_private_chat():
            chat_id = event.get_sender_id()
            message_thread_id = None
        else:
            group_id = event.get_group_id() or ""
            if "#" in group_id:
                chat_id, message_thread_id = group_id.split("#", 1)
            else:
                chat_id = group_id
                message_thread_id = None

        payload: dict[str, Any] = {}
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        reply_message_id = None
        for item in event.get_messages():
            if isinstance(item, Reply):
                reply_message_id = item.id
                break
        if reply_message_id is not None:
            payload["reply_to_message_id"] = str(reply_message_id)

        return chat_id, payload

    async def _probe_video_details(
        self,
        file_path: str
    ) -> dict[str, Any]:
        """使用 ffprobe 提取 Telegram 发送视频所需的元数据。"""
        if not file_path:
            return {}

        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json",
                file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
            if process.returncode != 0:
                logger.warning(
                    f"ffprobe 获取视频元数据失败: {file_path}, "
                    f"stderr={stderr.decode('utf-8', errors='ignore')[:200]}"
                )
                return {}

            payload = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
            stream = (payload.get("streams") or [{}])[0]
            fmt = payload.get("format") or {}

            result: dict[str, Any] = {}
            width = stream.get("width")
            height = stream.get("height")
            duration = fmt.get("duration")
            codec_name = stream.get("codec_name")
            pix_fmt = stream.get("pix_fmt")
            format_name = fmt.get("format_name")

            if isinstance(width, int) and width > 0:
                result["width"] = width
            if isinstance(height, int) and height > 0:
                result["height"] = height
            try:
                duration_value = int(float(duration))
                if duration_value > 0:
                    result["duration"] = duration_value
            except (TypeError, ValueError):
                pass
            if codec_name:
                result["codec_name"] = codec_name
            if pix_fmt:
                result["pix_fmt"] = pix_fmt
            if format_name:
                result["format_name"] = format_name

            logger.debug(f"ffprobe 视频元数据: {file_path}, metadata={result}")
            return result
        except FileNotFoundError:
            logger.warning("ffprobe 未找到，无法补全 Telegram 视频元数据")
            return {}
        except Exception as e:
            logger.warning(f"ffprobe 读取视频元数据异常: {file_path}, 错误: {e}")
            return {}

    async def _prepare_telegram_video_file(
        self,
        file_path: str
    ) -> tuple[str, dict[str, Any]]:
        """将视频调整为更适合 Telegram 流式播放的封装/编码。"""
        metadata = await self._probe_video_details(file_path)
        if not metadata:
            return file_path, {}

        codec_name = str(metadata.get("codec_name") or "").lower()
        pix_fmt = str(metadata.get("pix_fmt") or "").lower()
        format_name = str(metadata.get("format_name") or "").lower()

        needs_transcode = self.telegram_video_transcode and (
            codec_name not in {"h264", "avc1"} or (
                pix_fmt and pix_fmt not in {"yuv420p", "yuvj420p"}
            )
        )
        needs_faststart = self.telegram_video_faststart and "mp4" in format_name

        if not self.telegram_video_faststart and not self.telegram_video_transcode:
            return file_path, metadata

        if not needs_transcode and not needs_faststart:
            return file_path, metadata

        if not self.telegram_video_transcode and (
            pix_fmt and pix_fmt not in {"yuv420p", "yuvj420p"}
        ):
            logger.debug(
                f"跳过 Telegram 视频转码: {file_path}, codec={codec_name}, pix_fmt={pix_fmt}"
            )

        output_path = f"{file_path}.telegram.mp4"
        if needs_transcode:
            preset = str(
                self.telegram_transcode_profile_config.get("preset", "veryfast")
            )
            crf = int(self.telegram_transcode_profile_config.get("crf", 23))
            audio_bitrate_kbps = int(
                self.telegram_transcode_profile_config.get(
                    "audio_bitrate_kbps",
                    128
                )
            )
            cmd = [
                "ffmpeg", "-y", "-i", file_path,
                "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", f"{audio_bitrate_kbps}k",
                output_path
            ]
            action = "转码"
        else:
            cmd = [
                "ffmpeg", "-y", "-i", file_path,
                "-map", "0:v:0", "-map", "0:a?",
                "-c", "copy",
                "-movflags", "+faststart",
                output_path
            ]
            action = "重排"

        try:
            logger.debug(
                f"开始 Telegram 视频{action}: {file_path}, codec={codec_name}, "
                f"pix_fmt={pix_fmt}, profile={self.telegram_transcode_profile}"
            )
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=1800)
            if process.returncode != 0 or not os.path.exists(output_path):
                logger.warning(
                    f"Telegram 视频{action}失败: {file_path}, "
                    f"stderr={stderr.decode('utf-8', errors='ignore')[:300]}"
                )
                return file_path, metadata

            os.replace(output_path, file_path)
            refreshed_metadata = await self._probe_video_details(file_path)
            logger.debug(f"Telegram 视频{action}完成: {file_path}")
            return file_path, refreshed_metadata or metadata
        except FileNotFoundError:
            logger.warning("ffmpeg 未找到，无法调整 Telegram 视频为流式友好格式")
            return file_path, metadata
        except Exception as e:
            logger.warning(f"调整 Telegram 视频失败: {file_path}, 错误: {e}")
            return file_path, metadata
        finally:
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass

    async def _send_telegram_video(
        self,
        event: AstrMessageEvent,
        video: Video
    ) -> bool:
        """在 Telegram 上发送视频，并尽量补齐宽高/时长元数据。"""
        if event.get_platform_name() != "telegram":
            return False

        client = getattr(event, "client", None)
        if client is None:
            logger.warning("Telegram event 缺少 client，无法发送原生视频")
            return False

        chat_id, payload = self._build_telegram_payload(event)
        if not chat_id:
            logger.warning("无法确定 Telegram chat_id，跳过原生视频发送")
            return False

        path = await video.convert_to_file_path()
        path, details = await self._prepare_telegram_video_file(path)
        metadata = {
            key: details[key]
            for key in ("width", "height", "duration")
            if key in details
        }
        send_payload: dict[str, Any] = {
            "chat_id": chat_id,
            "video": path,
            "caption": getattr(video, "text", None) or None,
            "supports_streaming": True,
            "read_timeout": self.telegram_read_timeout,
            "write_timeout": self.telegram_write_timeout,
            "connect_timeout": self.telegram_connect_timeout,
            "pool_timeout": self.telegram_pool_timeout,
            **payload,
            **metadata
        }
        logger.debug(
            f"发送 Telegram 视频: chat_id={chat_id}, path={path}, metadata={metadata}, details={details}"
        )
        await client.send_video(**send_payload)
        return True

    async def _send_telegram_media_group(
        self,
        event: AstrMessageEvent,
        images: list[Image]
    ) -> bool:
        """使用 Telegram 原生 media group 发送纯图片图集。"""
        if not self.telegram_send_media_group:
            return False
        if event.get_platform_name() != "telegram":
            return False

        client = getattr(event, "client", None)
        if client is None:
            logger.warning("Telegram event 缺少 client，无法使用 media group")
            return False

        try:
            from telegram import InputMediaPhoto
        except Exception as e:
            logger.warning(f"导入 Telegram InputMediaPhoto 失败: {e}")
            return False

        chat_id, payload = self._build_telegram_payload(event)
        if not chat_id:
            logger.warning("无法确定 Telegram chat_id，跳过 media group 发送")
            return False

        image_paths = []
        for image in images:
            image_paths.append(await image.convert_to_file_path())

        with ExitStack() as stack:
            image_files = [
                stack.enter_context(open(path, "rb"))
                for path in image_paths
            ]
            for start in range(0, len(image_files), 10):
                batch_files = image_files[start:start + 10]
                media = [InputMediaPhoto(media=f) for f in batch_files]
                batch_payload = {
                    "chat_id": chat_id,
                    "media": media,
                    "read_timeout": self.telegram_read_timeout,
                    "write_timeout": self.telegram_write_timeout,
                    "connect_timeout": self.telegram_connect_timeout,
                    "pool_timeout": self.telegram_pool_timeout
                }
                batch_payload.update(payload)
                logger.debug(
                    f"发送 Telegram media group: chat_id={chat_id}, "
                    f"batch_size={len(batch_files)}, start_index={start}"
                )
                await client.send_media_group(**batch_payload)

        return True

    async def send_packed_results(
        self,
        event: AstrMessageEvent,
        link_metadata: list,
        sender_name: str,
        sender_id: Any,
        large_video_threshold_mb: float = 0.0
    ):
        """发送打包的结果（使用Nodes）

        Args:
            event: 消息事件对象
            link_metadata: 链接元数据列表
            sender_name: 发送者名称
            sender_id: 发送者ID
            large_video_threshold_mb: 大视频阈值(MB)
        """
        normal_metadata = [
            meta for meta in link_metadata if meta.get('is_normal', True)
        ]
        large_media_metadata = [
            meta for meta in link_metadata if meta.get('is_large_media', False)
        ]
        normal_link_nodes = [
            meta['link_nodes'] for meta in normal_metadata
        ]
        large_media_link_nodes = [
            meta['link_nodes'] for meta in large_media_metadata
        ]
        separator = "-------------------------------------"

        if normal_link_nodes:
            flat_nodes = []
            normal_video_files_to_cleanup = []
            for link_idx, link_nodes in enumerate(normal_link_nodes):
                if link_idx < len(normal_metadata):
                    link_video_files = normal_metadata[link_idx].get(
                        'video_files',
                        []
                    )
                    if link_video_files:
                        normal_video_files_to_cleanup.extend(
                            link_video_files
                        )
                if is_pure_image_gallery(link_nodes):
                    texts = [
                        node for node in link_nodes
                        if isinstance(node, Plain)
                    ]
                    images = [
                        node for node in link_nodes
                        if isinstance(node, Image)
                    ]
                    for text in texts:
                        flat_nodes.append(Node(
                            name=sender_name,
                            uin=sender_id,
                            content=[text]
                        ))
                    if images:
                        flat_nodes.append(Node(
                            name=sender_name,
                            uin=sender_id,
                            content=images
                        ))
                else:
                    for node in link_nodes:
                        if node is not None:
                            flat_nodes.append(Node(
                                name=sender_name,
                                uin=sender_id,
                                content=[node]
                            ))
                if link_idx < len(normal_link_nodes) - 1:
                    flat_nodes.append(Node(
                        name=sender_name,
                        uin=sender_id,
                        content=[Plain(separator)]
                    ))
            if flat_nodes:
                try:
                    await event.send(event.chain_result([Nodes(flat_nodes)]))
                except Exception as e:
                    logger.warning(f"发送打包节点失败: {e}")
                    await self._notify_send_failure(
                        event,
                        "部分媒体消息发送失败，请稍后重试或切换为预下载模式。"
                    )
                    raise
                finally:
                    cleanup_files(normal_video_files_to_cleanup)

        if large_media_link_nodes:
            await self.send_large_media_results(
                event,
                large_media_metadata,
                large_media_link_nodes,
                sender_name,
                sender_id,
                large_video_threshold_mb
            )

    async def send_large_media_results(
        self,
        event: AstrMessageEvent,
        metadata: list,
        link_nodes_list: list,
        sender_name: str,
        sender_id: Any,
        large_video_threshold_mb: float = 0.0
    ):
        """发送大媒体结果（单独发送）

        Args:
            event: 消息事件对象
            metadata: 元数据列表
            link_nodes_list: 链接节点列表
            sender_name: 发送者名称
            sender_id: 发送者ID
            large_video_threshold_mb: 大视频阈值(MB)
        """
        separator = "-------------------------------------"
        threshold_mb = (
            int(large_video_threshold_mb)
            if large_video_threshold_mb > 0
            else 50
        )
        notice_text = (
            f"⚠️ 链接中包含超过{threshold_mb}MB的视频时"
            f"将单独发送所有媒体"
        )
        all_video_files_to_cleanup = []
        try:
            await event.send(event.plain_result(notice_text))
            for link_idx, link_nodes in enumerate(link_nodes_list):
                link_video_files = []
                failed_node_count = 0
                if link_idx < len(metadata):
                    link_video_files = metadata[link_idx].get('video_files', [])
                all_video_files_to_cleanup.extend(link_video_files)
                try:
                    for node in link_nodes:
                        if node is not None:
                            try:
                                if isinstance(node, Video):
                                    sent = await self._send_telegram_video(
                                        event,
                                        node
                                    )
                                    if not sent:
                                        await event.send(event.chain_result([node]))
                                else:
                                    await event.send(event.chain_result([node]))
                            except Exception as e:
                                logger.warning(f"发送大媒体节点失败: {e}")
                                failed_node_count += 1
                except Exception as e:
                    logger.warning(f"发送大媒体链接失败: {e}")
                finally:
                    cleanup_files(link_video_files)
                if failed_node_count > 0:
                    await self._notify_send_failure(
                        event,
                        f"有 {failed_node_count} 条媒体消息发送失败，请稍后重试。"
                    )
                if link_idx < len(link_nodes_list) - 1:
                    try:
                        await event.send(event.plain_result(separator))
                    except Exception as e:
                        logger.warning(f"发送分隔符失败: {e}")
        except Exception as e:
            logger.exception(f"发送大媒体结果失败: {e}")
            cleanup_files(all_video_files_to_cleanup)
            raise

    async def send_unpacked_results(
        self,
        event: AstrMessageEvent,
        all_link_nodes: list,
        link_metadata: list
    ):
        """发送非打包的结果（独立发送）

        Args:
            event: 消息事件对象
            all_link_nodes: 所有链接节点列表
            link_metadata: 链接元数据列表
        """
        separator = "-------------------------------------"
        for link_idx, (link_nodes, metadata) in enumerate(
            zip(all_link_nodes, link_metadata)
        ):
            link_video_files = metadata.get('video_files', [])
            failed_node_count = 0
            try:
                if is_pure_image_gallery(link_nodes):
                    texts = [
                        node for node in link_nodes
                        if isinstance(node, Plain)
                    ]
                    images = [
                        node for node in link_nodes
                        if isinstance(node, Image)
                    ]
                    for text in texts:
                        try:
                            await event.send(event.chain_result([text]))
                        except Exception as e:
                            logger.warning(f"发送文本节点失败: {e}")
                            failed_node_count += 1
                    if images:
                        try:
                            media_group_sent = await self._send_telegram_media_group(
                                event,
                                images
                            )
                            if not media_group_sent:
                                await event.send(event.chain_result(images))
                        except Exception as e:
                            logger.warning(f"发送图片图集失败: {e}")
                            failed_node_count += 1
                else:
                    for node in link_nodes:
                        if node is not None:
                            try:
                                if isinstance(node, Video):
                                    sent = await self._send_telegram_video(
                                        event,
                                        node
                                    )
                                    if not sent:
                                        await event.send(event.chain_result([node]))
                                else:
                                    await event.send(event.chain_result([node]))
                            except Exception as e:
                                logger.warning(f"发送节点失败: {e}")
                                failed_node_count += 1
            finally:
                cleanup_files(link_video_files)
            if failed_node_count > 0:
                await self._notify_send_failure(
                    event,
                    f"有 {failed_node_count} 条媒体消息发送失败，请稍后重试。"
                )
            if link_idx < len(all_link_nodes) - 1:
                await event.send(event.plain_result(separator))
