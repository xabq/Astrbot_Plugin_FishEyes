"""
AstrbotFishEyes - AstrBot 电脑屏幕与远程控制插件
============================================================
集成本地/远程电脑屏幕查看、操控、视觉坐标网格识别、图片转述模型接入与 AstrBot 二级 WebUI 控制台。

作者: AstrBot Developer
版本: 1.1.0
"""

import io
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, Union

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from PIL import Image, ImageDraw

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

try:
    from astrbot.api.web import error_response, json_response, request as global_web_request
except ImportError:  # pragma: no cover
    def json_response(data, status_code=200):
        return {"status": "ok", "data": data, "status_code": status_code}

    def error_response(message, status_code=400):
        return {"status": "error", "error": message, "status_code": status_code}

    global_web_request = None

from screen_controller import UnifiedController

PLUGIN_NAME = "AstrbotFishEyes"
LEGACY_PLUGIN_NAME = "astrbot_plugin_screen_control"

HELP_TEXT = """🖥️ AstrbotFishEyes 电脑屏幕与远程控制

📌 聊天命令:
  /screen 连接                  连接被控端电脑
  /screen 断开                  断开连接并释放内存
  /screen 状态                  查看当前屏幕分辨率、连接状态与无操作倒计时
  /screen 截图 [观察目标]       截取电脑屏幕（带了观察目标时自动调用视觉模型分析坐标）
  /screen 识别点击 <目标描述>   视觉识别屏幕上指定元素并自动点击（如：/screen 识别点击 搜索按钮）
  /screen 点击 x y              左键点击指定屏幕像素坐标
  /screen 双击 x y              双击指定坐标
  /screen 右键 x y              右键点击指定坐标
  /screen 输入 <文本>           输入文字(支持中文)
  /screen 按键 <key>            敲击单键(enter/esc/tab/space/win等)
  /screen 组合键 <k1> <k2>      按组合键(如 ctrl c, alt tab, win d)
  /screen 滚动 <up/down>        滚轮滚动屏幕(up/down [数量])
  /screen 模式 [local/remote]   查看或切换控制模式
  /screen 帮助                  显示本帮助说明

🌐 二级 WebUI 控制台:
  请在 AstrBot 后台管理面板 →【插件】→ 进入 AstrbotFishEyes 插件详情，
  点击进入【屏幕控制台 (View)】，支持实时画面直观查看与点击操作！"""

KEY_MAP = {
    "回车": "enter", "enter": "enter",
    "空格": "space", "space": "space",
    "退格": "backspace", "backspace": "backspace",
    "制表": "tab", "tab": "tab",
    "esc": "escape", "escape": "escape",
    "删除": "delete", "del": "delete", "delete": "delete",
    "win": "win", "windows": "win", "super": "win", "cmd": "win",
    "up": "up", "上": "up",
    "down": "down", "下": "down",
    "left": "left", "左": "left",
    "right": "right", "右": "right",
    "ctrl": "ctrl", "alt": "alt", "shift": "shift",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
    "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8",
    "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
}


def _parse_keys_input(keys: Union[str, List[str]]) -> List[str]:
    """兼容 LLM 或前端传入列表、JSON 数组字符串、加号/空格/逗号分隔字符串的按键组合"""
    if isinstance(keys, list):
        raw_list = [str(k).strip() for k in keys if str(k).strip()]
    elif isinstance(keys, str):
        s = keys.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    raw_list = [str(k).strip() for k in parsed if str(k).strip()]
                else:
                    raw_list = [s]
            except Exception:
                raw_list = [k.strip() for k in re.split(r"[\s+,]+", s.strip("[]")) if k.strip()]
        else:
            raw_list = [k.strip() for k in re.split(r"[\s+,]+", s) if k.strip()]
    else:
        raw_list = []
    return [KEY_MAP.get(k.lower(), k.lower()) for k in raw_list]


def _cleanup_old_temp_files(temp_dir: str):
    """清理超过 5 分钟的临时截图文件，避免占用磁盘"""
    try:
        cutoff = time.time() - 300
        for fname in os.listdir(temp_dir):
            if fname.startswith("fisheyes_") and fname.endswith(".jpg"):
                fpath = os.path.join(temp_dir, fname)
                if os.path.getmtime(fpath) < cutoff:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
    except Exception:
        pass


def _save_temp_screenshot(img_bytes: bytes, prefix: str = "fisheyes_screen") -> str:
    """保存截图到临时目录"""
    temp_dir = tempfile.gettempdir()
    _cleanup_old_temp_files(temp_dir)
    now_ms = int(time.time() * 1000)
    img_path = os.path.join(temp_dir, f"{prefix}_{now_ms}.jpg")
    with open(img_path, "wb") as f:
        f.write(img_bytes)
    return img_path


def _build_grid_annotated_image(img_bytes: bytes) -> str:
    """
    在屏幕截图副本上绘制半透明像素坐标网格与 (x,y) 刻度标签，
    让图片转述模型/视觉大模型能够直接从画面上读取目标元素的精确像素坐标，消除分辨率缩放偏差。
    """
    with Image.open(io.BytesIO(img_bytes)) as base_im:
        img = base_im.convert("RGBA")
        w, h = img.size

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 自适应网格步长（通常 1920x1080 下为 200px）
    step_x = max(100, min(250, ((w // 9) // 50) * 50 or 200))
    step_y = max(100, min(250, ((h // 6) // 50) * 50 or 200))

    # 1. 绘制半透明网格线
    for x in range(step_x, w, step_x):
        draw.line([(x, 0), (x, h)], fill=(255, 60, 60, 95), width=1)
    for y in range(step_y, h, step_y):
        draw.line([(0, y), (w, y)], fill=(255, 60, 60, 95), width=1)

    # 2. 绘制顶部 X 轴与左侧 Y 轴标尺刻度
    for x in range(step_x, w, step_x):
        label = f"X={x}"
        tw = len(label) * 6 + 6
        draw.rectangle([x - tw // 2, 2, x + tw // 2, 16], fill=(0, 0, 0, 180))
        draw.text((x - tw // 2 + 3, 3), label, fill=(0, 255, 255, 255))

    for y in range(step_y, h, step_y):
        label = f"Y={y}"
        tw = len(label) * 6 + 6
        draw.rectangle([2, y - 7, 2 + tw, y + 7], fill=(0, 0, 0, 180))
        draw.text((5, y - 6), label, fill=(0, 255, 255, 255))

    # 3. 在每个网格交点绘制十字锚点与 "x,y" 坐标标签
    for x in range(step_x, w, step_x):
        for y in range(step_y, h, step_y):
            draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 40, 40, 230))
            label = f"{x},{y}"
            tw = len(label) * 6 + 6
            bx1 = min(x + 3, w - tw - 2)
            by1 = min(y + 2, h - 15)
            draw.rectangle([bx1, by1, bx1 + tw, by1 + 13], fill=(15, 23, 42, 165))
            draw.text((bx1 + 3, by1 + 1), label, fill=(255, 240, 80, 255))

    combined = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    combined.save(buf, format="JPEG", quality=85)
    return _save_temp_screenshot(buf.getvalue(), prefix="fisheyes_grid")


def _extract_coordinates_from_text(
    text: str,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int, str]]:
    """
    从视觉模型返回的分析文本或 JSON 中解析目标点击坐标 (x, y)，
    自动兼容真实像素坐标、[0, 1] 归一化坐标或 [0, 1000] 相对坐标。
    """
    if not text:
        return None

    def _convert_pair(raw_x: float, raw_y: float, coord_type: str = "") -> Tuple[int, int]:
        ctype = (coord_type or "").lower()
        if ctype == "normalized_1000":
            px = int(round((raw_x / 1000.0) * screen_w))
            py = int(round((raw_y / 1000.0) * screen_h))
        elif ctype == "normalized_1" or (0.0 < raw_x <= 1.0 and 0.0 < raw_y <= 1.0):
            px = int(round(raw_x * screen_w))
            py = int(round(raw_y * screen_h))
        else:
            px = int(round(raw_x))
            py = int(round(raw_y))
        px = max(0, min(px, max(0, screen_w - 1)))
        py = max(0, min(py, max(0, screen_h - 1)))
        return px, py

    # 1. 尝试解析代码块或内联 JSON 对象
    json_candidates = re.findall(r"\{[^{}]*\"x\"\s*:\s*[\d.]+[^{}]*\}", text, flags=re.DOTALL)
    for cand in json_candidates:
        try:
            obj = json.loads(cand)
            if obj.get("found") is False:
                return None
            if "x" in obj and "y" in obj:
                px, py = _convert_pair(float(obj["x"]), float(obj["y"]), str(obj.get("coord_type", "")))
                desc = str(obj.get("element") or obj.get("reason") or "")
                return px, py, desc
        except Exception:
            pass

    # 2. 尝试匹配 TARGET_COORDS: (x, y) 或 坐标: (x, y)
    m = re.search(
        r"(?:TARGET_COORDS|目标坐标|推荐点击坐标|中心坐标|坐标)\s*[:：]?\s*[\(\[（]\s*(\d+(?:\.\d+)?)\s*[,，\s]\s*(\d+(?:\.\d+)?)\s*[\)\]）]",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        px, py = _convert_pair(float(m.group(1)), float(m.group(2)))
        return px, py, "从模型坐标标记解析"

    # 3. 通用匹配 (x, y) 整数对
    pairs = re.findall(r"\(\s*(\d{1,4})\s*,\s*(\d{1,4})\s*\)", text)
    for sx, sy in pairs:
        px, py = _convert_pair(float(sx), float(sy))
        if 0 <= px < screen_w and 0 <= py < screen_h:
            return px, py, "从文本坐标对解析"

    return None


@register(
    "AstrbotFishEyes",
    "AstrBot Developer",
    "AstrbotFishEyes 电脑屏幕与远程控制：查看与操作本地或远程屏幕，支持视觉坐标识别、二级WebUI、LLM工具集与指令",
    "1.1.0",
)
class AstrbotFishEyesPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.controller = UnifiedController(self.config)

        # 注册 AstrBot 二级 WebUI (Plugin View) 后端 API 路由
        self._register_web_routes()

    def _persist_config(self):
        """如果 config 是 AstrBotConfig 实例，将其持久化保存到磁盘"""
        if hasattr(self.config, "save_config") and callable(self.config.save_config):
            try:
                self.config.save_config()
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 持久化保存配置异常: {e}")

    def _register_web_routes(self):
        """注册供前端可视化视图 (Plugin View) 通信的后端 API（同时兼容新旧路由前缀）"""
        ctx = self.context
        if not hasattr(ctx, "register_web_api"):
            return

        prefixes = [PLUGIN_NAME, LEGACY_PLUGIN_NAME]
        routes = [
            ("status", self._api_get_status, ["GET"], "获取当前屏幕控制器状态"),
            ("screenshot", self._api_get_screenshot, ["GET"], "获取屏幕最新截图"),
            ("action", self._api_post_action, ["POST"], "执行键鼠操作"),
            ("lock", self._api_post_lock, ["POST"], "切换紧急锁定开关"),
            ("config", self._api_handle_config, ["GET", "POST"], "获取或更新屏幕控制配置"),
            ("connect", self._api_post_connect, ["POST"], "连接被控目标"),
            ("disconnect", self._api_post_disconnect, ["POST"], "断开被控目标连接并释放资源"),
            ("logs", self._api_get_logs, ["GET"], "获取最近操作日志"),
        ]

        for prefix in prefixes:
            for endpoint, handler, methods, desc in routes:
                try:
                    ctx.register_web_api(f"/{prefix}/{endpoint}", handler, methods, desc)
                except Exception as e:
                    logger.debug(f"[{PLUGIN_NAME}] 注册路由 /{prefix}/{endpoint} 跳过: {e}")

    async def initialize(self):
        """插件初始化钩子（默认不自动连接，仅当 auto_connect 显式设为 True 时连接）"""
        auto_conn = bool(self.config.get("auto_connect", False))
        logger.info(
            f"[{PLUGIN_NAME}] 正在初始化屏幕控制插件 (模式: {self.controller.mode}, 启动自动连接: {auto_conn}, 无操作断连: {self.controller.idle_timeout}s)..."
        )
        if auto_conn:
            try:
                status = await self.controller.connect(source="startup")
                if status.get("connected"):
                    logger.info(
                        f"[{PLUGIN_NAME}] 目标屏幕自动连接成功: 分辨率 {status.get('screen', {}).get('width')}x{status.get('screen', {}).get('height')}"
                    )
                else:
                    logger.warning(f"[{PLUGIN_NAME}] 启动自动连接未就绪: {status.get('error')}")
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 自动连接探测异常: {e}")

    async def terminate(self):
        """插件卸载清理钩子"""
        logger.info(f"[{PLUGIN_NAME}] 正在释放屏幕控制器资源...")
        await self.controller.close()

    # =========================================================================
    #  视觉模型 / 图片转述模型 (default_image_caption_provider_id) 调用与坐标定位
    # =========================================================================

    def _get_astrbot_caption_provider_id(self, event: Optional[AstrMessageEvent] = None) -> str:
        """从 AstrBot 系统配置中读取默认图片转述模型 ID (default_image_caption_provider_id)"""
        if not hasattr(self.context, "get_config"):
            return ""
        try:
            umo = getattr(event, "unified_msg_origin", None) if event else None
            try:
                ab_cfg = self.context.get_config(umo=umo) if umo else self.context.get_config()
            except TypeError:
                ab_cfg = self.context.get_config()

            if isinstance(ab_cfg, dict):
                prov_settings = ab_cfg.get("provider_settings") or {}
                cid = (
                    prov_settings.get("default_image_caption_provider_id")
                    or ab_cfg.get("default_image_caption_provider_id")
                    or ""
                )
                return str(cid).strip()
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 读取 default_image_caption_provider_id 异常: {e}")
        return ""

    async def _resolve_vision_provider(self, event: Optional[AstrMessageEvent] = None) -> Tuple[Optional[Any], str, str]:
        """
        按优先级获取用于识别屏幕截图与坐标的模型提供商：
        1. 插件配置中的 vision_provider_id
        2. AstrBot 系统设置中的默认图片转述模型 (provider_settings.default_image_caption_provider_id)
        3. 当前会话正在使用的主对话模型 (get_using_provider_async / get_using_provider)
        返回: (provider_instance, provider_id, source_label)
        """
        ctx = self.context
        umo = getattr(event, "unified_msg_origin", None) if event else None

        # 1. 插件自定义视觉模型 ID
        custom_id = str(self.config.get("vision_provider_id") or "").strip()
        if custom_id and hasattr(ctx, "get_provider_by_id"):
            try:
                prov = ctx.get_provider_by_id(custom_id)
                if prov is not None:
                    return prov, custom_id, f"插件指定视觉模型({custom_id})"
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 获取插件指定视觉模型 {custom_id} 失败: {e}")

        # 2. AstrBot 全局配置的图片转述模型 (default_image_caption_provider_id)
        caption_id = self._get_astrbot_caption_provider_id(event)
        if caption_id and hasattr(ctx, "get_provider_by_id"):
            try:
                prov = ctx.get_provider_by_id(caption_id)
                if prov is not None:
                    return prov, caption_id, f"AstrBot图片转述模型({caption_id})"
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 获取 AstrBot 图片转述模型 {caption_id} 失败: {e}")

        # 3. 回退到当前对话模型
        prov = None
        prov_id = ""
        if hasattr(ctx, "get_current_chat_provider_id") and umo:
            try:
                prov_id = await ctx.get_current_chat_provider_id(umo=umo) or ""
            except Exception:
                pass

        if hasattr(ctx, "get_using_provider_async"):
            try:
                prov = await ctx.get_using_provider_async(umo=umo)
            except Exception:
                pass

        if prov is None and hasattr(ctx, "get_using_provider"):
            try:
                prov = ctx.get_using_provider(umo=umo)
            except TypeError:
                try:
                    prov = ctx.get_using_provider()
                except Exception:
                    pass
            except Exception:
                pass

        if prov is None and prov_id and hasattr(ctx, "get_provider_by_id"):
            try:
                prov = ctx.get_provider_by_id(prov_id)
            except Exception:
                pass

        return prov, prov_id, f"当前对话模型({prov_id or 'default'})"

    async def _call_vision_model(
        self,
        event: Optional[AstrMessageEvent],
        image_path: str,
        prompt: str,
        system_prompt: str,
    ) -> Tuple[Optional[str], str]:
        """调用选定的图片转述/视觉模型分析图片并返回结果文本与模型来源说明"""
        prov, prov_id, source_label = await self._resolve_vision_provider(event)
        if prov is None and not prov_id:
            return None, "未找到可用的视觉或图片转述模型提供商"

        # 方式 A: 直接通过 Provider.text_chat(..., image_urls=[image_path]) 调用（兼容 AstrBot v3.x & v4.x）
        if prov is not None and hasattr(prov, "text_chat"):
            try:
                llm_resp = await prov.text_chat(
                    prompt=prompt,
                    context=[],
                    system_prompt=system_prompt,
                    image_urls=[image_path],
                )
                text = getattr(llm_resp, "completion_text", "") or ""
                if not text and getattr(llm_resp, "result_chain", None):
                    text = llm_resp.result_chain.get_plain_text()
                if text:
                    return text.strip(), source_label
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 通过 {source_label} text_chat 识图失败: {e}")
                caption_id = self._get_astrbot_caption_provider_id(event)
                if caption_id and caption_id != prov_id and hasattr(self.context, "get_provider_by_id"):
                    try:
                        fallback_prov = self.context.get_provider_by_id(caption_id)
                        if fallback_prov and hasattr(fallback_prov, "text_chat"):
                            llm_resp2 = await fallback_prov.text_chat(
                                prompt=prompt,
                                context=[],
                                system_prompt=system_prompt,
                                image_urls=[image_path],
                            )
                            text2 = getattr(llm_resp2, "completion_text", "") or ""
                            if text2:
                                return text2.strip(), f"AstrBot图片转述模型({caption_id})"
                    except Exception as e2:
                        logger.warning(f"[{PLUGIN_NAME}] 回退图片转述模型 {caption_id} 同样失败: {e2}")
                return None, f"{source_label} 识图调用异常: {e}"

        # 方式 B: 通过 AstrBot v4.5.7+ context.llm_generate 调用
        if prov_id and hasattr(self.context, "llm_generate"):
            try:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    image_urls=[image_path],
                )
                text = getattr(llm_resp, "completion_text", "") or ""
                if text:
                    return text.strip(), source_label
            except Exception as e:
                return None, f"{source_label} llm_generate 异常: {e}"

        return None, f"{source_label} 不支持多模态图片输入"

    async def _analyze_screenshot_for_bot(
        self,
        event: Optional[AstrMessageEvent],
        raw_img_path: str,
        img_bytes: bytes,
        width: int,
        height: int,
        target: str = "",
    ) -> str:
        """
        对截取的屏幕画面叠加坐标标尺网格，并调用图片转述模型/视觉模型提取画面内容与可点击元素坐标 (x, y)。
        """
        use_grid = bool(self.config.get("enable_coord_grid", True))
        analysis_img_path = raw_img_path
        if use_grid:
            try:
                analysis_img_path = _build_grid_annotated_image(img_bytes)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 绘制坐标网格失败，将使用原图识图: {e}")
                analysis_img_path = raw_img_path

        system_prompt = (
            "你是一个专业的电脑屏幕 GUI 视觉识别与坐标定位助手。\n"
            f"当前屏幕真实像素分辨率为：宽度 {width} px，高度 {height} px（左上角为 (0, 0)，右下角为 ({width - 1}, {height - 1})）。\n"
            "图片上已绘制了辅助定位的红色网格线与黄字像素坐标标签 `x,y`（例如 `400,200` 代表横坐标 X=400, 纵坐标 Y=200）。\n"
            "请务必参考画面上的坐标网格刻度，输出真实像素坐标 (x, y)，严禁编造超出分辨率的坐标。"
        )

        target_clean = (target or "").strip()
        if target_clean:
            user_prompt = (
                f"请仔细观察这张电脑屏幕截图（分辨率 {width}x{height}），重点寻找目标元素：【{target_clean}】。\n"
                "请严格按以下结构输出：\n"
                "1. **当前屏幕概况**：简述当前打开的窗口/软件及主要内容；\n"
                f"2. **目标元素【{target_clean}】定位**：说明该元素在画面中的位置特征，并参考网格刻度给出其中心点的精确像素坐标，格式必须包含：`TARGET_COORDS: (x, y)`；\n"
                "3. **其他关键可交互元素坐标列表**：列出画面上其他主要按钮、输入框、图标或菜单的名称及其中心像素坐标 `(x, y)`。"
            )
        else:
            user_prompt = (
                f"请仔细观察这张电脑屏幕截图（分辨率 {width}x{height}），为自动化操作 Agent 提供详尽的视觉与坐标转述：\n"
                "1. **当前窗口与画面内容**：当前焦点窗口是什么？画面中显示了哪些核心文字、弹窗或状态？\n"
                "2. **可点击/可交互元素坐标清单**：请参考画面上的 `x,y` 坐标网格刻度，列出画面上所有关键按钮、输入框、标签页、图标或菜单项的名称，以及它们中心点的精确像素坐标 `(x, y)`（例如：`- [搜索输入框]: (640, 120)`）。"
            )

        vision_text, source_label = await self._call_vision_model(
            event=event,
            image_path=analysis_img_path,
            prompt=user_prompt,
            system_prompt=system_prompt,
        )

        if vision_text:
            return (
                f"📸 屏幕截图成功 (分辨率: {width}x{height}，视觉识别模型: {source_label})\n"
                f"--- 屏幕视觉识别与元素坐标分析 ---\n"
                f"{vision_text}\n"
                f"----------------------------------\n"
                f"💡 提示：你可直接根据上述元素的 (x, y) 像素坐标调用 `screen_click` / `screen_double_click` / `screen_type`，或调用 `screen_click_element` 传入目标描述进行点击。"
            )
        else:
            return (
                f"📸 屏幕截图已捕获 (分辨率: {width}x{height})，但自动调用视觉/图片转述模型未返回描述 ({source_label})。\n"
                f"💡 请在 AstrBot【系统设置 -> 默认图片转述模型】或本插件设置【vision_provider_id】中配置一个支持多模态识图的模型（如 GPT-4o / Gemini / Qwen-VL / GLM-4V），即可自动识别画面元素与点击坐标。"
            )

    async def _locate_target_coordinates(
        self,
        event: Optional[AstrMessageEvent],
        target: str,
        quality: int = 80,
    ) -> Tuple[Optional[Tuple[int, int]], str, Dict[str, Any]]:
        """
        截取最新屏幕并通过带坐标网格的视觉/图片转述模型定位指定 target 元素的中心像素坐标 (x, y)
        返回: ((x, y) 或 None, 识别说明或错误原因, screenshot_res)
        """
        res = await self.controller.take_screenshot(quality=quality, source="llm", include_bytes=True)
        w, h = int(res["width"]), int(res["height"])

        try:
            grid_img_path = _build_grid_annotated_image(res["bytes"])
        except Exception:
            grid_img_path = _save_temp_screenshot(res["bytes"])

        system_prompt = (
            "你是一个高精度的电脑 GUI 视觉元素坐标定位器。\n"
            f"当前屏幕真实像素分辨率为：宽度 {w} px，高度 {h} px（X 范围 0~{w - 1}，Y 范围 0~{h - 1}）。\n"
            "截图中已绘制了红色网格线和黄字像素坐标标签 `x,y`（如 `400,200` 表示 X=400, Y=200）。\n"
            "请参考元素周边的 `x,y` 坐标网格刻度，计算出目标元素正中心的精确像素坐标。"
        )
        user_prompt = (
            f"请在截图中找到目标元素：【{target}】。\n"
            "请只输出一段合法的 JSON 对象（不要输出多余解释），格式如下：\n"
            '{"found": true, "x": 整数横坐标, "y": 整数纵坐标, "coord_type": "pixel", "element": "识别到的元素名称与位置说明"}\n'
            '如果画面中确实不存在该元素，请输出：{"found": false, "reason": "未找到的原因及当前画面实际内容简述"}'
        )

        vision_text, source_label = await self._call_vision_model(
            event=event,
            image_path=grid_img_path,
            prompt=user_prompt,
            system_prompt=system_prompt,
        )

        if not vision_text:
            return None, f"无法调用视觉/图片转述模型 ({source_label})", res

        parsed = _extract_coordinates_from_text(vision_text, w, h)
        if parsed is None:
            return None, f"视觉模型 ({source_label}) 未能在当前屏幕上定位到【{target}】。模型返回: {vision_text}", res

        px, py, desc = parsed
        return (px, py), f"{desc or target} [由 {source_label} 定位]", res

    # =========================================================================
    #  安全与权限鉴权机制
    # =========================================================================

    def _is_event_admin(self, event: AstrMessageEvent) -> bool:
        """全面判定发送者是否为 AstrBot 管理员"""
        if getattr(event, "role", None) == "admin":
            return True
        is_admin_fn = getattr(event, "is_admin", None)
        if callable(is_admin_fn):
            try:
                if is_admin_fn():
                    return True
            except Exception:
                pass
        elif isinstance(is_admin_fn, bool) and is_admin_fn:
            return True
        return False

    def _authorize(self, event: AstrMessageEvent, tool_name: str) -> Optional[str]:
        """权限鉴权：管理员放行；白名单放行；其余拒绝"""
        uid = str(event.get_sender_id())

        if self._is_event_admin(event):
            return None

        if not bool(self.config.get("whitelist_enabled", True)):
            return self._build_deny(event, tool_name, uid, reason="白名单未开启，仅管理员可用")

        whitelist = self.config.get("whitelist", []) or []
        whitelist = [str(x).strip() for x in whitelist if str(x).strip()]
        if uid in whitelist:
            return None

        return self._build_deny(event, tool_name, uid)

    def _build_deny(self, event: AstrMessageEvent, tool_name: str, uid: str, reason: str = "") -> str:
        template = self.config.get(
            "deny_message",
            "❌ 权限不足：{tool} 仅限管理员或白名单用户使用。\n你的用户 ID: {uid}",
        )
        try:
            msg = str(template).replace("{uid}", uid).replace("{tool}", tool_name)
        except Exception:
            msg = f"❌ 权限不足：{tool_name} 仅限管理员或白名单用户使用 (UID: {uid})"
        if reason:
            msg += f"\n({reason})"
        logger.info(f"[{PLUGIN_NAME}] 拦截未授权用户 {uid} 调用 {tool_name}")
        return msg

    # =========================================================================
    #  二级 WebUI API 接口实现 (Plugin View Backend)
    # =========================================================================

    def _resolve_request(self, args):
        if args and hasattr(args[0], "method"):
            return args[0]
        return global_web_request

    async def _read_req_json(self, req) -> dict:
        if req is None:
            return {}
        if hasattr(req, "json") and callable(req.json):
            res = req.json()
            return await res if hasattr(res, "__await__") else (res or {})
        if hasattr(req, "get_json") and callable(req.get_json):
            res = req.get_json()
            return await res if hasattr(res, "__await__") else (res or {})
        return {}

    async def _api_get_status(self, *args, **kwargs):
        status = await self.controller.get_status(probe=False)
        return json_response(status)

    async def _api_get_screenshot(self, *args, **kwargs):
        req = self._resolve_request(args)
        try:
            quality = int(self.config.get("screenshot_quality", 75) or 75)
            source = "webui_auto"
            if req:
                if hasattr(req, "query") and isinstance(req.query, dict):
                    quality = int(req.query.get("quality", quality))
                    source = str(req.query.get("source", source))
                elif hasattr(req, "args") and hasattr(req.args, "get"):
                    quality = int(req.args.get("quality", quality))
                    source = str(req.args.get("source", source))
            res = await self.controller.take_screenshot(
                quality=quality,
                source=source,
                include_bytes=False,
            )
            return json_response(res)
        except Exception as e:
            return error_response(f"截图失败: {e}", status_code=400)

    async def _api_post_action(self, *args, **kwargs):
        req = self._resolve_request(args)
        try:
            body = await self._read_req_json(req)
            action = body.get("action", "")
            source = body.get("source", "webui")

            if action == "click":
                res = await self.controller.click(
                    x=body.get("x", 0),
                    y=body.get("y", 0),
                    button=body.get("button", "left"),
                    clicks=int(body.get("clicks", 1)),
                    source=source,
                )
            elif action == "double_click":
                res = await self.controller.double_click(
                    x=body.get("x", 0),
                    y=body.get("y", 0),
                    source=source,
                )
            elif action == "right_click":
                res = await self.controller.right_click(
                    x=body.get("x", 0),
                    y=body.get("y", 0),
                    source=source,
                )
            elif action == "move":
                res = await self.controller.move(
                    x=body.get("x", 0),
                    y=body.get("y", 0),
                    duration=float(body.get("duration", 0.0)),
                    source=source,
                )
            elif action == "drag":
                res = await self.controller.drag(
                    x1=body.get("x1", 0),
                    y1=body.get("y1", 0),
                    x2=body.get("x2", 0),
                    y2=body.get("y2", 0),
                    duration=float(body.get("duration", 0.5)),
                    source=source,
                )
            elif action == "type":
                res = await self.controller.type_text(
                    text=body.get("text", ""),
                    source=source,
                )
            elif action == "press":
                raw_key = str(body.get("key", "enter")).lower()
                mapped_key = KEY_MAP.get(raw_key, raw_key)
                res = await self.controller.press_key(
                    key=mapped_key,
                    source=source,
                )
            elif action == "hotkey":
                mapped_keys = _parse_keys_input(body.get("keys", []))
                res = await self.controller.hotkey(
                    keys=mapped_keys,
                    source=source,
                )
            elif action == "scroll":
                res = await self.controller.scroll(
                    direction=body.get("direction", "down"),
                    amount=int(body.get("amount", 3)),
                    source=source,
                )
            else:
                return error_response(f"未知操作类型: {action}")

            return json_response(res)
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def _api_post_lock(self, *args, **kwargs):
        req = self._resolve_request(args)
        try:
            body = await self._read_req_json(req)
            locked = bool(body.get("locked", False))
            self.controller.is_locked = locked
            self.controller.touch_activity()
            self.controller.record_log("lock_toggle", f"状态切换为: {'已锁定' if locked else '已解锁'}", source="webui")
            return json_response({"is_locked": self.controller.is_locked})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def _api_handle_config(self, *args, **kwargs):
        req = self._resolve_request(args)
        if req and getattr(req, "method", "GET") == "POST":
            try:
                body = await self._read_req_json(req)
                if body.get("ssh_password") == "******":
                    body.pop("ssh_password", None)
                if body.get("remote_token") == "******":
                    body.pop("remote_token", None)

                self.config.update(body)
                self._persist_config()
                await self.controller.update_config_async(self.config)
                return json_response({"status": "ok", "message": "配置更新成功"})
            except Exception as e:
                return error_response(f"保存配置失败: {e}", status_code=500)
        else:
            safe_cfg = dict(self.config)
            if safe_cfg.get("ssh_password"):
                safe_cfg["ssh_password"] = "******"
            return json_response(safe_cfg)

    async def _api_post_connect(self, *args, **kwargs):
        try:
            status = await self.controller.connect(source="webui")
            return json_response(status)
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def _api_post_disconnect(self, *args, **kwargs):
        try:
            status = await self.controller.disconnect(reason="管理员在 WebUI 手动断开连接", source="webui")
            return json_response(status)
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def _api_get_logs(self, *args, **kwargs):
        return json_response(self.controller.action_logs)

    # =========================================================================
    #  LLM Agent 工具集 (Computer Use Tools - 支持视觉网格识图与精准点击)
    # =========================================================================

    @filter.llm_tool(name="screen_screenshot")
    async def tool_screenshot(self, event: AstrMessageEvent, target: str = "", quality: int = 75):
        """截取当前被控电脑屏幕画面，并自动调用图片转述/视觉模型识别画面内容与各可点击元素的像素坐标 (x, y)。当你需要观察屏幕内容、寻找某个按钮/输入框的坐标或确认操作结果时调用此工具（若未连接将自动尝试连接一次）。

        Args:
            target(string): 可选，需要重点寻找或定位的目标元素描述（例如 '浏览器搜索框'、'登录按钮'、'微信图标'），留空则全面分析整个屏幕的窗口与可点击元素坐标
            quality(number): 截图JPEG画质(10-100)，默认75
        """
        if err := self._authorize(event, "screen_screenshot"):
            return err
        try:
            res = await self.controller.take_screenshot(quality=quality, source="llm", include_bytes=True)
            img_path = _save_temp_screenshot(res["bytes"])

            # 根据配置决定是否将原始截图同步发到聊天窗口
            if bool(self.config.get("send_screenshot_in_tool", True)):
                try:
                    await event.send(
                        event.chain_result([
                            Comp.Plain(f"🖥️ 屏幕截图 ({res['width']}x{res['height']}):\n"),
                            Comp.Image.fromFileSystem(img_path),
                        ])
                    )
                except Exception as send_err:
                    logger.warning(f"[{PLUGIN_NAME}] LLM 截图主动发送图片异常: {send_err}")

            # 调用图片转述模型/视觉模型结合坐标网格分析画面内容与元素像素坐标
            analysis_report = await self._analyze_screenshot_for_bot(
                event=event,
                raw_img_path=img_path,
                img_bytes=res["bytes"],
                width=res["width"],
                height=res["height"],
                target=target,
            )
            return analysis_report
        except Exception as e:
            return f"❌ 屏幕截图失败: {e}"

    @filter.llm_tool(name="screen_click_element")
    async def tool_click_element(
        self,
        event: AstrMessageEvent,
        target: str,
        action: str = "click",
    ):
        """通过视觉识别自动在屏幕上寻找指定的目标元素（按钮、图标、输入框、文字链接等）并精准点击它。当你不知道元素的精确坐标、或者希望一步完成“看图定位+点击”时优先调用此工具。

        Args:
            target(string): 要点击的目标元素详细描述（例如 '右上角关闭按钮'、'Chrome浏览器图标'、'搜索输入框'、'确定按钮'）
            action(string): 点击方式，可选 'click'(单击左键，默认)、'double_click'(双击左键)、'right_click'(单击右键)
        """
        if err := self._authorize(event, "screen_click_element"):
            return err
        target_clean = (target or "").strip()
        if not target_clean:
            return "❌ 请提供要点击的目标元素描述 (target)"

        try:
            coords, detail, _shot = await self._locate_target_coordinates(event, target_clean)
            if coords is None:
                return f"❌ 未能点击【{target_clean}】：{detail}"

            x, y = coords
            act = (action or "click").strip().lower()
            if act in ("double", "double_click", "双击"):
                await self.controller.double_click(x=x, y=y, source="llm")
                act_label = "双击左键"
            elif act in ("right", "right_click", "右键"):
                await self.controller.right_click(x=x, y=y, source="llm")
                act_label = "单击右键"
            else:
                await self.controller.click(x=x, y=y, button="left", clicks=1, source="llm")
                act_label = "单击左键"

            return f"✅ 已通过视觉识别定位【{target_clean}】于像素坐标 ({x}, {y})，并成功执行【{act_label}】（{detail}）。"
        except Exception as e:
            return f"❌ 视觉定位点击【{target_clean}】失败: {e}"

    @filter.llm_tool(name="screen_click")
    async def tool_click(self, event: AstrMessageEvent, x: int, y: int, button: str = "left"):
        """点击电脑屏幕上指定的像素坐标 (x, y)。用于点击已知坐标的按钮、图标、链接等交互元素（若当前未连接将自动尝试连接一次）。

        Args:
            x(number): 屏幕横坐标(像素整数)
            y(number): 屏幕纵坐标(像素整数)
            button(string): 鼠标按键('left'为左键, 'right'为右键)
        """
        if err := self._authorize(event, "screen_click"):
            return err
        try:
            await self.controller.click(x=x, y=y, button=button, source="llm")
            return f"✅ 已成功在屏幕坐标 ({x}, {y}) 执行鼠标【{button}】点击"
        except Exception as e:
            return f"❌ 点击失败: {e}"

    @filter.llm_tool(name="screen_double_click")
    async def tool_double_click(self, event: AstrMessageEvent, x: int, y: int):
        """双击电脑屏幕上指定的像素坐标 (x, y)。通常用于打开文件、启动桌面软件图标等（若当前未连接将自动尝试连接一次）。

        Args:
            x(number): 屏幕横坐标
            y(number): 屏幕纵坐标
        """
        if err := self._authorize(event, "screen_double_click"):
            return err
        try:
            await self.controller.double_click(x=x, y=y, source="llm")
            return f"✅ 已在坐标 ({x}, {y}) 双击鼠标"
        except Exception as e:
            return f"❌ 双击失败: {e}"

    @filter.llm_tool(name="screen_right_click")
    async def tool_right_click(self, event: AstrMessageEvent, x: int, y: int):
        """在屏幕指定像素坐标 (x, y) 上右键点击，以弹出上下文菜单（若当前未连接将自动尝试连接一次）。

        Args:
            x(number): 屏幕横坐标
            y(number): 屏幕纵坐标
        """
        if err := self._authorize(event, "screen_right_click"):
            return err
        try:
            await self.controller.right_click(x=x, y=y, source="llm")
            return f"✅ 已在坐标 ({x}, {y}) 右键点击"
        except Exception as e:
            return f"❌ 右键点击失败: {e}"

    @filter.llm_tool(name="screen_type")
    async def tool_type(self, event: AstrMessageEvent, text: str):
        """在当前电脑焦点窗口中输入文本。支持中英文和符号（若当前未连接将自动尝试连接一次）。

        Args:
            text(string): 要输入的文本内容
        """
        if err := self._authorize(event, "screen_type"):
            return err
        try:
            res = await self.controller.type_text(text=text, source="llm")
            return f"✏️ 已输入文本 (长度: {res.get('length', len(text))})"
        except Exception as e:
            return f"❌ 输入文本失败: {e}"

    @filter.llm_tool(name="screen_press")
    async def tool_press(self, event: AstrMessageEvent, key: str):
        """按下一个指定的键盘单键。例如回车、Esc、退格、空格、Tab或功能键（若当前未连接将自动尝试连接一次）。

        Args:
            key(string): 键名(如 enter, escape, backspace, tab, space, win, f5, up, down, left, right 等)
        """
        if err := self._authorize(event, "screen_press"):
            return err
        mapped_key = KEY_MAP.get(str(key).strip().lower(), str(key).strip().lower())
        try:
            await self.controller.press_key(key=mapped_key, source="llm")
            return f"✅ 已按下按键: {mapped_key}"
        except Exception as e:
            return f"❌ 按键失败: {e}"

    @filter.llm_tool(name="screen_hotkey")
    async def tool_hotkey(self, event: AstrMessageEvent, keys: str):
        """执行键盘组合键。用于快捷操作，如复制粘贴、切换窗口、回到桌面等（若当前未连接将自动尝试连接一次）。

        Args:
            keys(string): 组合键名称，用空格或加号分隔，例如 'ctrl c' 或 'alt+tab' 或 'win d'
        """
        if err := self._authorize(event, "screen_hotkey"):
            return err
        mapped_keys = _parse_keys_input(keys)
        if not mapped_keys:
            return "❌ 组合键参数为空"
        try:
            await self.controller.hotkey(keys=mapped_keys, source="llm")
            return f"⚡ 已执行组合键: {' + '.join(mapped_keys)}"
        except Exception as e:
            return f"❌ 组合键执行失败: {e}"

    @filter.llm_tool(name="screen_scroll")
    async def tool_scroll(self, event: AstrMessageEvent, direction: str = "down", amount: int = 3):
        """滚动鼠标滚轮。用于上下翻页查看长内容（若当前未连接将自动尝试连接一次）。

        Args:
            direction(string): 滚动方向('up'向上, 'down'向下)
            amount(number): 滚动的步长格数(默认3)
        """
        if err := self._authorize(event, "screen_scroll"):
            return err
        try:
            await self.controller.scroll(direction=direction, amount=amount, source="llm")
            return f"📜 已向【{direction}】滚动 {amount} 格"
        except Exception as e:
            return f"❌ 滚动失败: {e}"

    @filter.llm_tool(name="screen_move")
    async def tool_move(self, event: AstrMessageEvent, x: int, y: int):
        """移动鼠标光标到屏幕指定坐标（若当前未连接将自动尝试连接一次）。

        Args:
            x(number): 横坐标
            y(number): 纵坐标
        """
        if err := self._authorize(event, "screen_move"):
            return err
        try:
            await self.controller.move(x=x, y=y, source="llm")
            return f"📍 鼠标已移动至 ({x}, {y})"
        except Exception as e:
            return f"❌ 移动鼠标失败: {e}"

    @filter.llm_tool(name="screen_drag")
    async def tool_drag(self, event: AstrMessageEvent, x1: int, y1: int, x2: int, y2: int):
        """鼠标拖拽操作。从起始坐标按下鼠标并拖动到目标坐标后释放（若当前未连接将自动尝试连接一次）。

        Args:
            x1(number): 起始横坐标
            y1(number): 起始纵坐标
            x2(number): 结束横坐标
            y2(number): 结束纵坐标
        """
        if err := self._authorize(event, "screen_drag"):
            return err
        try:
            await self.controller.drag(x1=x1, y1=y1, x2=x2, y2=y2, source="llm")
            return f"🎯 已将鼠标从 ({x1}, {y1}) 拖拽至 ({x2}, {y2})"
        except Exception as e:
            return f"❌ 拖拽失败: {e}"

    @filter.llm_tool(name="screen_status")
    async def tool_status(self, event: AstrMessageEvent):
        """获取当前屏幕控制器状态、目标屏幕分辨率、连接模式及运行环境信息（若未连接则自动尝试连接一次以探测目标信息）。"""
        if err := self._authorize(event, "screen_status"):
            return err
        try:
            status = await self.controller.get_status(probe=False)
            if not status.get("connected"):
                status = await self.controller.connect(source="llm")
            err_line = f"\n错误信息: {status.get('error')}" if status.get("error") else ""
            return (
                f"🖥️ AstrbotFishEyes 电脑控制状态:\n"
                f"运行模式: {status.get('mode')}\n"
                f"连接就绪: {'✅ 是' if status.get('connected') else '❌ 否'}\n"
                f"屏幕分辨率: {status.get('screen', {}).get('width')}x{status.get('screen', {}).get('height')}\n"
                f"操作系统平台: {status.get('platform')}\n"
                f"无操作自动断连: {status.get('idle_timeout')} 秒\n"
                f"紧急锁定状态: {'🔒 已锁定' if status.get('is_locked') else '🔓 已解锁'}"
                f"{err_line}"
            )
        except Exception as e:
            return f"❌ 获取状态失败: {e}"

    # =========================================================================
    #  聊天交互命令 (/screen, /cu, /电脑控制, /屏幕)
    # =========================================================================

    @filter.command("screen", alias={"cu", "屏幕", "电脑控制"})
    async def cmd_screen(self, event: AstrMessageEvent):
        """电脑控制聊天指令入口"""
        text = event.message_str.strip()
        parts = text.split()
        if len(parts) <= 1:
            yield event.plain_result(HELP_TEXT)
            return

        sub_cmd = parts[1].lower()
        args = parts[2:]

        # 帮助
        if sub_cmd in ["帮助", "help", "?", "-h"]:
            yield event.plain_result(HELP_TEXT)
            return

        # 状态查询（仅查看状态，不主动唤醒连接）
        if sub_cmd in ["状态", "status"]:
            status = await self.controller.get_status(probe=False)
            err_line = f"\n异常详情: {status.get('error')}" if status.get("error") else ""
            idle_line = (
                f"\n无操作断连剩余: {status.get('idle_remaining')} 秒 (阈值 {status.get('idle_timeout')} 秒)"
                if status.get("connected") and status.get("idle_remaining") is not None
                else f"\n无操作断连设置: {status.get('idle_timeout')} 秒"
            )
            yield event.plain_result(
                f"🖥️ AstrbotFishEyes 屏幕控制状态\n"
                f"运行模式: {status.get('mode')}\n"
                f"连接状态: {'🟢 已连接' if status.get('connected') else '⚪ 未连接'}\n"
                f"屏幕分辨率: {status.get('screen', {}).get('width')}x{status.get('screen', {}).get('height')}\n"
                f"系统环境: {status.get('platform')}\n"
                f"锁定状态: {'🔒 已锁定' if status.get('is_locked') else '🔓 已解锁'}"
                f"{idle_line}{err_line}"
            )
            return

        # 模式切换
        if sub_cmd in ["模式", "mode"]:
            if err := self._authorize(event, "screen mode"):
                yield event.plain_result(err)
                return
            if args:
                new_mode = args[0].lower()
                if new_mode in ["local", "remote"]:
                    self.config["mode"] = new_mode
                    self._persist_config()
                    await self.controller.update_config_async(self.config)
                    yield event.plain_result(f"✅ 已切换控制模式为: {new_mode}")
                    return
                else:
                    yield event.plain_result("❌ 模式可选值: local 或 remote")
                    return
            else:
                yield event.plain_result(f"当前模式: {self.controller.mode}")
                return

        # 手动连接
        if sub_cmd in ["连接", "connect"]:
            if err := self._authorize(event, "screen connect"):
                yield event.plain_result(err)
                return
            yield event.plain_result("🔄 正在连接被控目标...")
            try:
                status = await self.controller.connect(source="chat")
                if status.get("connected"):
                    yield event.plain_result(
                        f"✅ 连接成功!\n"
                        f"模式: {status.get('mode')}\n"
                        f"屏幕分辨率: {status.get('screen', {}).get('width')}x{status.get('screen', {}).get('height')}\n"
                        f"无操作自动断连: {status.get('idle_timeout')} 秒"
                    )
                else:
                    yield event.plain_result(f"❌ 连接失败: {status.get('error')}")
            except Exception as e:
                yield event.plain_result(f"❌ 连接异常: {e}")
            return

        # 手动断开连接
        if sub_cmd in ["断开", "断开连接", "disconnect"]:
            if err := self._authorize(event, "screen disconnect"):
                yield event.plain_result(err)
                return
            await self.controller.disconnect(reason="通过聊天命令手动断开连接", source="chat")
            yield event.plain_result("🔌 已断开与被控电脑的连接并释放内存资源。")
            return

        # 鉴权检查 (操作类命令 - 若未连接将在内部自动尝试一次连接)
        if err := self._authorize(event, f"screen {sub_cmd}"):
            yield event.plain_result(err)
            return

        # 1. 截图 (若带参数如 `/screen 截图 登录按钮` 则同时调用视觉模型分析坐标)
        if sub_cmd in ["截图", "screenshot", "screen"]:
            try:
                res = await self.controller.take_screenshot(
                    source=f"chat_{event.get_sender_id()}",
                    include_bytes=True,
                )
                img_path = _save_temp_screenshot(res["bytes"])
                target_query = " ".join(args).strip()

                yield event.chain_result([
                    Comp.Plain(f"🖥️ 屏幕截图 ({res['width']}x{res['height']}):\n"),
                    Comp.Image.fromFileSystem(img_path),
                ])

                if target_query:
                    analysis = await self._analyze_screenshot_for_bot(
                        event=event,
                        raw_img_path=img_path,
                        img_bytes=res["bytes"],
                        width=res["width"],
                        height=res["height"],
                        target=target_query,
                    )
                    yield event.plain_result(analysis)
            except Exception as e:
                yield event.plain_result(f"❌ 截图失败: {e}")
            return

        # 1.5 视觉识别并点击
        if sub_cmd in ["识别点击", "视觉点击", "find_click", "smart_click"]:
            target_desc = " ".join(args).strip()
            if not target_desc:
                yield event.plain_result("用法: /screen 识别点击 <目标元素描述>\n示例: /screen 识别点击 搜索按钮")
                return
            yield event.plain_result(f"🔍 正在截取屏幕并视觉定位【{target_desc}】...")
            try:
                coords, detail, _shot = await self._locate_target_coordinates(event, target_desc)
                if coords is None:
                    yield event.plain_result(f"❌ 未能定位【{target_desc}】：{detail}")
                    return
                x, y = coords
                await self.controller.click(x=x, y=y, source="chat")
                yield event.plain_result(f"✅ 已视觉定位【{target_desc}】于坐标 ({x}, {y}) 并完成点击！\n({detail})")
            except Exception as e:
                yield event.plain_result(f"❌ 视觉识别点击失败: {e}")
            return

        # 2. 点击
        if sub_cmd in ["点击", "click"]:
            if len(args) < 2:
                yield event.plain_result("用法: /screen 点击 <x> <y>\n示例: /screen 点击 500 300")
                return
            try:
                x, y = int(args[0]), int(args[1])
                await self.controller.click(x=x, y=y, source="chat")
                yield event.plain_result(f"✅ 已在坐标 ({x}, {y}) 点击鼠标")
            except Exception as e:
                yield event.plain_result(f"❌ 点击失败: {e}")
            return

        # 3. 双击
        if sub_cmd in ["双击", "double_click", "double"]:
            if len(args) < 2:
                yield event.plain_result("用法: /screen 双击 <x> <y>")
                return
            try:
                x, y = int(args[0]), int(args[1])
                await self.controller.double_click(x=x, y=y, source="chat")
                yield event.plain_result(f"✅ 已在坐标 ({x}, {y}) 双击鼠标")
            except Exception as e:
                yield event.plain_result(f"❌ 双击失败: {e}")
            return

        # 4. 右键
        if sub_cmd in ["右键", "right_click", "right"]:
            if len(args) < 2:
                yield event.plain_result("用法: /screen 右键 <x> <y>")
                return
            try:
                x, y = int(args[0]), int(args[1])
                await self.controller.right_click(x=x, y=y, source="chat")
                yield event.plain_result(f"✅ 已在坐标 ({x}, {y}) 右键点击")
            except Exception as e:
                yield event.plain_result(f"❌ 右键点击失败: {e}")
            return

        # 5. 输入文本
        if sub_cmd in ["输入", "type"]:
            input_content = text.split(maxsplit=2)
            if len(input_content) < 3:
                yield event.plain_result("用法: /screen 输入 <内容>\n示例: /screen 输入 你好世界")
                return
            val = input_content[2]
            try:
                await self.controller.type_text(text=val, source="chat")
                yield event.plain_result(f"✅ 已在当前焦点窗口输入: {repr(val)}")
            except Exception as e:
                yield event.plain_result(f"❌ 输入失败: {e}")
            return

        # 6. 单键按键
        if sub_cmd in ["按键", "press", "key"]:
            if not args:
                yield event.plain_result("用法: /screen 按键 <key>\n常用: enter, esc, tab, space, win, f5")
                return
            raw_key = args[0].lower()
            key = KEY_MAP.get(raw_key, raw_key)
            try:
                await self.controller.press_key(key=key, source="chat")
                yield event.plain_result(f"✅ 已按下按键: {key}")
            except Exception as e:
                yield event.plain_result(f"❌ 按键失败: {e}")
            return

        # 7. 组合键
        if sub_cmd in ["组合键", "hotkey"]:
            if not args:
                yield event.plain_result("用法: /screen 组合键 <k1> <k2> ...\n示例: /screen 组合键 ctrl c")
                return
            keys = _parse_keys_input(args)
            try:
                await self.controller.hotkey(keys=keys, source="chat")
                yield event.plain_result(f"⚡ 已执行组合键: {' + '.join(keys)}")
            except Exception as e:
                yield event.plain_result(f"❌ 组合键失败: {e}")
            return

        # 8. 滚轮
        if sub_cmd in ["滚动", "scroll"]:
            direction = args[0].lower() if args else "down"
            if direction in ["上", "up"]:
                direction = "up"
            else:
                direction = "down"
            amount = int(args[1]) if len(args) > 1 and args[1].lstrip("-").isdigit() else 3
            try:
                await self.controller.scroll(direction=direction, amount=amount, source="chat")
                yield event.plain_result(f"📜 已向【{direction}】滚动 {amount} 格")
            except Exception as e:
                yield event.plain_result(f"❌ 滚动失败: {e}")
            return

        # 未知子指令
        yield event.plain_result(f"❓ 未知子指令: {sub_cmd}\n输入 /screen 帮助 查看用法")


# 向下兼容旧类名导入
ScreenControlPlugin = AstrbotFishEyesPlugin
