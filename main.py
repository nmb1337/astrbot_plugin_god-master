"""
AI智能表情包 - AstrBot 插件

功能：
- 在机器人回复群聊时，自动分析自身回复的语气和情绪
- 调用大模型判断最合适的表情包分类
- 从对应分类目录随机选择图片追加到回复末尾
- 支持群黑白名单、触发概率、冷却时间
- 支持 WebUI 配置管理

兼容：AstrBot v4.16+（含 v4.25.5）
"""

import json
import random
import time
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# ---- Web API 兼容层（兼容 AstrBot v4.25.x 及更早版本） ----
# 新版 AstrBot 提供 astrbot.api.web，旧版使用 Quart 原生 API。
try:
    from astrbot.api.web import json_response, error_response, request
    # request.json(default=) 是 astrbot.api.web 特有方法
    _HAS_ASTRBOT_WEB = True
except ImportError:
    from quart import jsonify, request

    _HAS_ASTRBOT_WEB = False

    def json_response(data):
        return jsonify(data)

    def error_response(message, status_code=400):
        return jsonify({"status": "error", "message": message}), status_code


async def _get_json_body(default=None):
    """跨版本获取 JSON 请求体"""
    if _HAS_ASTRBOT_WEB:
        return await request.json(default=default)
    else:
        data = await request.get_json(silent=True)
        return data if data is not None else default

# ---------------------------------------------------------------------------
# 插件注册
# ---------------------------------------------------------------------------

PLUGIN_NAME = "astrbot_plugin_ai_sticker"

# 支持的图片后缀
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@register(PLUGIN_NAME, "AstrBot Community", "AI智能表情包：机器人回复时根据自身语气自动搭配表情包", "1.0.0")
class AIStickerPlugin(Star):
    """AI 智能表情包插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 插件目录
        self.plugin_dir: Path = Path(__file__).parent
        # 图片根目录
        self.images_dir: Path = self.plugin_dir / "images"

        # 分类 -> 图片路径列表 的映射
        self.category_images: dict[str, list[Path]] = {}
        # 分类名列表
        self.categories: list[str] = []

        # 冷却时间记录：group_id -> 上次发送时间戳
        self._cooldown_map: dict[str, float] = {}

        # 注册 Web API
        self._register_web_apis()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self):
        """插件初始化：扫描图片目录、加载分类"""
        self._scan_images()
        logger.info(
            f"[AI表情包] 已加载 {len(self.categories)} 个分类，"
            f"共 {sum(len(v) for v in self.category_images.values())} 张图片"
        )

    async def terminate(self):
        """插件卸载时清理"""
        self._cooldown_map.clear()
        logger.info("[AI表情包] 插件已卸载")

    # ------------------------------------------------------------------
    # 图片扫描（公开方法，也供 WebUI 重载后调用）
    # ------------------------------------------------------------------

    def scan_images(self):
        """扫描 images 目录，自动识别所有分类及其图片"""
        self._scan_images()

    def _scan_images(self):
        """扫描 images 目录，自动识别所有分类及其图片"""
        self.category_images.clear()
        self.categories.clear()

        if not self.images_dir.exists():
            logger.warning(f"[AI表情包] 图片目录不存在: {self.images_dir}")
            return

        for child in sorted(self.images_dir.iterdir()):
            if not child.is_dir():
                continue
            category_name = child.name
            images = []
            for img_file in sorted(child.iterdir()):
                if img_file.is_file() and img_file.suffix.lower() in IMAGE_EXTENSIONS:
                    images.append(img_file)
            if images:
                self.category_images[category_name] = images
                self.categories.append(category_name)
                logger.info(f"[AI表情包] 分类「{category_name}」: {len(images)} 张图片")
            else:
                logger.warning(f"[AI表情包] 分类「{category_name}」下无有效图片，已跳过")

    # ------------------------------------------------------------------
    # 配置读取辅助
    # ------------------------------------------------------------------

    def _get_category_descriptions(self) -> dict[str, str]:
        """从配置中读取分类描述（JSON 字符串 -> dict）"""
        raw = self.config.get("category_descriptions", "{}")
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[AI表情包] 分类描述 JSON 解析失败，使用空字典")
            return {}

    def _get_ai_prompt_template(self) -> str:
        """获取 AI 提示词模板"""
        return self.config.get(
            "ai_prompt_template",
            "你是一个表情包分类助手。请根据机器人回复消息的语气和情绪，选择最合适的表情包分类。\n\n"
            "当前可用的表情包分类：\n{categories}\n\n"
            "机器人的回复内容：\n\"{message}\"\n\n"
            "请仅返回分类名称。如果该回复不适合搭配表情包，请返回\"不发送\"。\n"
            "禁止返回解释、额外文字或标点符号。只返回分类名称或\"不发送\"。",
        )

    def _is_enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    def _get_trigger_probability(self) -> int:
        return max(0, min(100, int(self.config.get("trigger_probability", 30))))

    def _get_cooldown_seconds(self) -> int:
        return max(0, int(self.config.get("cooldown_seconds", 60)))

    def _use_whitelist(self) -> bool:
        return bool(self.config.get("use_whitelist", False))

    def _get_whitelist(self) -> list[str]:
        val = self.config.get("group_whitelist", [])
        return val if isinstance(val, list) else []

    def _get_blacklist(self) -> list[str]:
        val = self.config.get("group_blacklist", [])
        return val if isinstance(val, list) else []

    # ------------------------------------------------------------------
    # 群管理检查
    # ------------------------------------------------------------------

    def _check_group(self, group_id: str) -> bool:
        """检查群聊是否允许发送表情包。返回 True 表示允许。"""
        if not group_id:
            return True  # 非群聊消息，允许
        if self._use_whitelist():
            whitelist = self._get_whitelist()
            return group_id in whitelist if whitelist else True
        else:
            blacklist = self._get_blacklist()
            return group_id not in blacklist if blacklist else True

    # ------------------------------------------------------------------
    # 冷却检查
    # ------------------------------------------------------------------

    def _check_cooldown(self, group_id: str) -> bool:
        """检查是否在冷却期内，返回 True 表示可以发送"""
        cooldown = self._get_cooldown_seconds()
        if cooldown <= 0:
            return True
        now = time.time()
        last = self._cooldown_map.get(group_id, 0)
        return (now - last) >= cooldown

    def _update_cooldown(self, group_id: str):
        """更新冷却时间"""
        self._cooldown_map[group_id] = time.time()

    # ------------------------------------------------------------------
    # 概率检查
    # ------------------------------------------------------------------

    def _check_probability(self) -> bool:
        """根据配置的概率决定是否触发"""
        prob = self._get_trigger_probability()
        if prob >= 100:
            return True
        if prob <= 0:
            return False
        return random.randint(1, 100) <= prob

    # ------------------------------------------------------------------
    # AI 分类
    # ------------------------------------------------------------------

    async def _classify_message(self, message: str, event: AstrMessageEvent) -> str | None:
        """调用大模型对消息进行分类。返回分类名，或 None 表示不发送。"""
        if not self.categories:
            logger.warning("[AI表情包] 没有可用分类，跳过")
            return None

        # 构建分类描述文本
        descriptions = self._get_category_descriptions()
        category_lines = []
        for cat in self.categories:
            desc = descriptions.get(cat, cat)
            category_lines.append(f"- {cat}：{desc}")
        categories_text = "\n".join(category_lines)

        # 构建完整提示词
        prompt_template = self._get_ai_prompt_template()
        prompt = prompt_template.replace("{categories}", categories_text).replace(
            "{message}", message
        )

        try:
            # 获取当前会话的聊天模型 ID
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning("[AI表情包] 无法获取当前聊天模型 ID")
                return None

            # 调用大模型
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            if not llm_resp:
                logger.warning("[AI表情包] LLM 返回为空")
                return None

            result_text = llm_resp.completion_text.strip()
            logger.info(f"[AI表情包] LLM 返回原始结果: '{result_text}'")

            # 清理响应（去除可能的引号、标点、空白等）
            result_text = result_text.strip("。，！？、\"'「」『』\n\r\t ")

            if result_text == "不发送":
                return None

            # 精确匹配分类名
            for cat in self.categories:
                if result_text == cat:
                    return cat

            # 模糊匹配：如果 LLM 返回的文本以分类名开头或包含分类名，且整体长度合理
            for cat in self.categories:
                if result_text.startswith(cat) and len(result_text) <= len(cat) + 3:
                    return cat
                if cat in result_text and len(result_text) <= len(cat) * 2:
                    return cat

            logger.warning(f"[AI表情包] LLM 返回了无法识别的分类: '{result_text}'")
            return None

        except Exception as e:
            logger.error(f"[AI表情包] AI 分类调用失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 随机选择图片
    # ------------------------------------------------------------------

    def _pick_random_image(self, category: str) -> Path | None:
        """从指定分类中随机选择一张图片"""
        images = self.category_images.get(category, [])
        if not images:
            logger.warning(f"[AI表情包] 分类「{category}」下无图片")
            return None
        return random.choice(images)

    # ------------------------------------------------------------------
    # 表情包追加钩子（机器人回复时根据自身语气自动配图）
    # ------------------------------------------------------------------

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在机器人发送回复前，分析自身回复的语气并追加对应表情包"""

        # 1. 仅处理群聊消息
        group_id = event.message_obj.group_id
        if not group_id:
            return

        # 2. 检查是否启用
        if not self._is_enabled():
            return

        # 3. 群管理检查
        if not self._check_group(group_id):
            return

        # 4. 冷却检查
        if not self._check_cooldown(group_id):
            return

        # 5. 概率检查
        if not self._check_probability():
            return

        # 6. 提取机器人即将发送的文本内容
        result = event.get_result()
        chain = result.chain
        reply_text = ""
        for comp in chain:
            if hasattr(comp, "text") and comp.text:
                reply_text += comp.text
        reply_text = reply_text.strip()
        if not reply_text:
            return

        # 7. AI 分类（分析机器人自身的回复语气）
        category = await self._classify_message(reply_text, event)
        if category is None:
            return

        # 8. 随机选择图片
        img_path = self._pick_random_image(category)
        if img_path is None:
            return

        # 9. 更新冷却时间
        self._update_cooldown(group_id)

        # 10. 将表情包图片追加到消息链末尾
        try:
            import astrbot.api.message_components as Comp

            chain.append(Comp.Image.fromFileSystem(str(img_path)))
            logger.info(
                f"[AI表情包] 群 {group_id} 追加「{category}」表情: {img_path.name}"
            )
        except Exception as e:
            logger.error(f"[AI表情包] 追加图片失败: {e}")

    # ------------------------------------------------------------------
    # 管理指令：重载图片
    # ------------------------------------------------------------------

    @filter.command("ai_sticker_reload")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_reload(self, event: AstrMessageEvent):
        """管理员指令：重新扫描图片目录"""
        self._scan_images()
        yield event.plain_result(
            f"[AI表情包] 已重新扫描！共 {len(self.categories)} 个分类，"
            f"{sum(len(v) for v in self.category_images.values())} 张图片"
        )

    # ------------------------------------------------------------------
    # Web API 注册
    # ------------------------------------------------------------------

    def _register_web_apis(self):
        """注册插件 Page 所需的 Web API"""

        # 获取插件状态信息
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/status",
            self._api_status,
            ["GET"],
            "获取插件状态",
        )

        # 获取所有分类及描述
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/categories",
            self._api_get_categories,
            ["GET"],
            "获取所有分类及描述",
        )

        # 保存分类描述
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/categories/save",
            self._api_save_categories,
            ["POST"],
            "保存分类描述",
        )

        # 获取某分类下的图片列表
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/images/<category>",
            self._api_get_images,
            ["GET"],
            "获取分类下的图片信息",
        )

        # 获取单张图片（用于预览）
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image/preview/<category>/<filename>",
            self._api_preview_image,
            ["GET"],
            "获取图片预览",
        )

        # 重新扫描图片目录
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/rescan",
            self._api_rescan,
            ["POST"],
            "重新扫描图片目录",
        )

        # 获取提示词模板
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/prompt",
            self._api_get_prompt,
            ["GET"],
            "获取 AI 提示词模板",
        )

        # 保存提示词模板
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/prompt/save",
            self._api_save_prompt,
            ["POST"],
            "保存 AI 提示词模板",
        )

    # --- API Handlers ---

    async def _api_status(self):
        """返回插件运行状态"""
        return json_response({
            "enabled": self._is_enabled(),
            "trigger_probability": self._get_trigger_probability(),
            "cooldown_seconds": self._get_cooldown_seconds(),
            "use_whitelist": self._use_whitelist(),
            "group_whitelist": self._get_whitelist(),
            "group_blacklist": self._get_blacklist(),
            "total_categories": len(self.categories),
            "total_images": sum(len(v) for v in self.category_images.values()),
        })

    async def _api_get_categories(self):
        """返回所有分类及其描述、图片列表"""
        descriptions = self._get_category_descriptions()
        result = []
        for cat in self.categories:
            result.append({
                "name": cat,
                "description": descriptions.get(cat, ""),
                "image_count": len(self.category_images.get(cat, [])),
                "images": [
                    img.name for img in self.category_images.get(cat, [])
                ],
            })
        return json_response({"categories": result})

    async def _api_save_categories(self):
        """保存分类描述到配置"""
        payload = await _get_json_body(default={})
        new_descriptions = payload.get("descriptions", {})
        if not isinstance(new_descriptions, dict):
            return error_response("descriptions 必须是字典格式", status_code=400)

        # 更新配置
        self.config["category_descriptions"] = json.dumps(
            new_descriptions, ensure_ascii=False, indent=2
        )
        try:
            self.config.save_config()
        except Exception as e:
            logger.error(f"[AI表情包] 保存配置失败: {e}")
            return error_response(f"保存失败: {e}", status_code=500)

        logger.info("[AI表情包] 分类描述已更新")
        return json_response({"saved": True})

    async def _api_get_images(self, category: str):
        """返回指定分类下的图片文件列表"""
        if category not in self.category_images:
            return error_response(f"分类「{category}」不存在", status_code=404)

        images = self.category_images[category]
        return json_response({
            "category": category,
            "images": [img.name for img in images],
            "count": len(images),
        })

    async def _api_preview_image(self, category: str, filename: str):
        """返回图片的 base64 编码数据（用于前端预览）"""
        import base64

        # 安全检查：防止路径遍历攻击
        if ".." in filename or "/" in filename or "\\" in filename:
            return error_response("非法文件名", status_code=400)

        if category not in self.category_images:
            return error_response(f"分类「{category}」不存在", status_code=404)

        # 在图片列表中查找匹配文件
        img_path = None
        for img in self.category_images[category]:
            if img.name == filename:
                img_path = img
                break

        if img_path is None or not img_path.exists():
            return error_response("图片不存在", status_code=404)

        # 限制预览图片大小（最大 5MB）
        file_size = img_path.stat().st_size
        if file_size > 5 * 1024 * 1024:
            return error_response("图片过大，无法预览（超过5MB）", status_code=400)

        # 根据后缀确定 MIME 类型
        ext = img_path.suffix.lower()
        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        mime_type = content_types.get(ext, "application/octet-stream")

        # 读取图片并 base64 编码
        try:
            with open(img_path, "rb") as f:
                img_data = f.read()
            b64_data = base64.b64encode(img_data).decode("ascii")
        except Exception as e:
            logger.error(f"[AI表情包] 读取图片失败: {e}")
            return error_response(f"读取图片失败: {e}", status_code=500)

        return json_response({
            "filename": filename,
            "content_type": mime_type,
            "base64": b64_data,
        })

    async def _api_rescan(self):
        """重新扫描图片目录"""
        self._scan_images()
        return json_response({
            "rescanned": True,
            "total_categories": len(self.categories),
            "total_images": sum(len(v) for v in self.category_images.values()),
        })

    async def _api_get_prompt(self):
        """返回当前 AI 提示词模板"""
        return json_response({
            "prompt_template": self._get_ai_prompt_template(),
        })

    async def _api_save_prompt(self):
        """保存 AI 提示词模板到配置"""
        payload = await _get_json_body(default={})
        new_prompt = payload.get("prompt_template", "")
        if not isinstance(new_prompt, str):
            return error_response("prompt_template 必须是字符串", status_code=400)

        self.config["ai_prompt_template"] = new_prompt
        try:
            self.config.save_config()
        except Exception as e:
            logger.error(f"[AI表情包] 保存提示词失败: {e}")
            return error_response(f"保存失败: {e}", status_code=500)

        logger.info("[AI表情包] AI 提示词模板已更新")
        return json_response({"saved": True})
