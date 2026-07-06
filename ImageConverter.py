from __future__ import annotations

import colorsys
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlparse

import requests
from PIL import Image, ImageTk, UnidentifiedImageError

APP_NAME = "图片富文本转换器"
APP_VERSION = "2.3.1"

DISCLAIMER_TEXT = (
    "本工具仅限娱乐、学习、个人创作与已授权的服务器测试使用。\n\n"
    "禁止将本工具用于炸服、刷屏、恶意生成超大文本、干扰服务器运行、规避服务器规则，"
    "或任何未经授权的破坏性用途。\n\n"
    "使用者应自行确认服务器规则、插件限制与相关授权。因不当使用造成的后果由使用者自行承担。"
)


# =========================
# 转换核心
# =========================

@dataclass
class ConvertOptions:
    scale: int = 0  # 0 = 自动
    compress: bool = True
    max_pixels: int = 50_000
    byte_limit: int = 32_767
    max_download_bytes: int = 4 * 1024 * 1024
    url_timeout: float = 8.0
    resize_enabled: bool = True
    max_width: int = 96
    max_height: int = 96
    auto_fit_byte_limit: bool = True
    auto_fit_min_width: int = 8
    auto_fit_min_height: int = 8
    alpha_threshold: int = 0
    transparent_as_space: bool = True
    newline_mode: str = "literal"  # literal = 输出两个字符 \n；actual = 输出真实换行
    block_char: str = "█"
    transparent_char: str = " "
    threshold_step: float = 0.5
    max_threshold: float = 5.0


@dataclass
class ConvertStats:
    original_size: tuple[int, int]
    final_size: tuple[int, int]
    scale: int
    threshold: float
    chars: int
    utf16_bytes: int
    elapsed_seconds: float
    resized_times: int


class ConversionError(Exception):
    pass


class ConversionOverflow(Exception):
    pass


def color_difference(c1: tuple[int, int, int, int], c2: tuple[int, int, int, int]) -> float:
    """按 PintTheDragon/Images 的 C# 逻辑近似计算 HSV 差异。

    注意：C# Color.GetSaturation/GetBrightness 返回 0~1，不能乘 100。
    """
    r1, g1, b1 = c1[:3]
    r2, g2, b2 = c2[:3]

    h1, s1, v1 = colorsys.rgb_to_hsv(r1 / 255.0, g1 / 255.0, b1 / 255.0)
    h2, s2, v2 = colorsys.rgb_to_hsv(r2 / 255.0, g2 / 255.0, b2 / 255.0)

    h1 *= 360.0
    h2 *= 360.0

    dh = abs(h1 - h2)
    if dh > 180.0:
        dh = 360.0 - dh

    ds = abs(s1 - s2)
    db = abs(v1 - v2)
    return (dh * 0.755 + ds * 2.0 + db * 0.7) / 3.0


def calculate_scale(width: int, height: int) -> int:
    avg = (width + height) / 2.0
    capped_avg = 45 if avg > 60 else avg
    value = int((-0.47 * capped_avg) + 28.72)
    return max(1, min(100, value))


def resize_to_fit(image: Image.Image, max_width: int, max_height: int) -> tuple[Image.Image, bool]:
    if max_width <= 0 or max_height <= 0:
        return image, False

    width, height = image.size
    if width <= max_width and height <= max_height:
        return image, False

    ratio = min(max_width / width, max_height / height)
    new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
    return image.resize(new_size, Image.Resampling.LANCZOS), True


def shrink_image(image: Image.Image, factor: float = 0.90) -> Image.Image:
    width, height = image.size
    new_size = (max(1, int(width * factor)), max(1, int(height * factor)))
    if new_size == image.size:
        new_size = (max(1, width - 1), max(1, height - 1))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def load_image_from_file(path: str) -> Image.Image:
    if not path:
        raise ConversionError("请选择图片文件。")
    file_path = Path(path)
    if not file_path.exists():
        raise ConversionError(f"图片文件不存在：{path}")
    if not file_path.is_file():
        raise ConversionError(f"路径不是文件：{path}")

    try:
        # 用 bytes 读取，避免 Image.open(path) 长时间锁文件。
        data = file_path.read_bytes()
        image = Image.open(BytesIO(data))
        image.load()
        return image
    except UnidentifiedImageError:
        raise ConversionError("无法识别该图片格式。")
    except Exception as exc:
        raise ConversionError(f"无法加载图片文件：{exc}")


def load_image_from_url(url: str, options: ConvertOptions) -> Image.Image:
    if not url:
        raise ConversionError("请输入图片 URL。")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConversionError("URL 只允许 http 或 https。")

    try:
        with requests.get(url, stream=True, timeout=options.url_timeout, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"}) as response:
            response.raise_for_status()

            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > options.max_download_bytes:
                raise ConversionError(f"图片文件过大：Content-Length 超过 {options.max_download_bytes} 字节。")

            chunks: list[bytes] = []
            downloaded = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > options.max_download_bytes:
                    raise ConversionError(f"图片文件过大：下载内容超过 {options.max_download_bytes} 字节。")
                chunks.append(chunk)

        data = b"".join(chunks)
        image = Image.open(BytesIO(data))
        image.load()
        return image
    except ConversionError:
        raise
    except requests.RequestException as exc:
        raise ConversionError(f"无法从 URL 下载图片：{exc}")
    except UnidentifiedImageError:
        raise ConversionError("URL 内容不是可识别的图片。")
    except Exception as exc:
        raise ConversionError(f"无法从 URL 加载图片：{exc}")


def convert_once(image: Image.Image, options: ConvertOptions, progress=None) -> tuple[str, int, float]:
    width, height = image.size
    total_pixels = width * height
    if total_pixels > options.max_pixels:
        raise ConversionError(
            f"图片像素过大：{width}x{height} = {total_pixels}，超过上限 {options.max_pixels}。"
        )

    scale = calculate_scale(width, height) if options.scale == 0 else int(options.scale)
    scale = max(1, min(100, scale))
    line_height = max(1, 100 - scale)
    prefix = f"<size={scale}%><line-height={line_height}%>"
    suffix = "</line-height></size>"
    newline = "\\n" if options.newline_mode == "literal" else "\n"

    rgba = image.convert("RGBA")
    pixels = rgba.load()

    threshold = 0.0
    while threshold < options.max_threshold:
        if progress:
            progress(f"正在转换：{width}x{height}，压缩阈值 {threshold:.1f}")

        parts: list[str] = [prefix]
        last_color: tuple[int, int, int, int] | None = None

        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]

                if options.transparent_as_space and a <= options.alpha_threshold:
                    if last_color is not None:
                        parts.append("</color>")
                        last_color = None
                    parts.append(options.transparent_char)
                    continue

                current = (r, g, b, a)
                if last_color is None:
                    parts.append(f"<color=#{r:02X}{g:02X}{b:02X}{a:02X}>{options.block_char}")
                    last_color = current
                    continue

                if current == last_color:
                    parts.append(options.block_char)
                    continue

                diff = color_difference(current, last_color)
                if options.compress and diff > threshold:
                    parts.append(f"</color><color=#{r:02X}{g:02X}{b:02X}{a:02X}>{options.block_char}")
                    last_color = current
                else:
                    parts.append(options.block_char)

            parts.append(newline)

        if last_color is not None:
            parts.append("</color>")
        parts.append(suffix)

        result = "".join(parts)
        byte_len = len(result.encode("utf-16-le"))
        if byte_len <= options.byte_limit:
            return result, byte_len, threshold

        threshold += options.threshold_step

    raise ConversionOverflow(f"转换结果超过 UTF-16 字节上限 {options.byte_limit}。")


def convert_image_to_text(image: Image.Image, options: ConvertOptions, progress=None) -> tuple[str, ConvertStats]:
    start = time.perf_counter()
    original_size = image.size
    resized_times = 0

    working = image.convert("RGBA")

    if options.resize_enabled:
        resized, changed = resize_to_fit(working, options.max_width, options.max_height)
        if changed:
            working = resized
            resized_times += 1
            if progress:
                progress(f"已按最大尺寸缩放为 {working.width}x{working.height}")

    while True:
        try:
            text, byte_len, threshold = convert_once(working, options, progress)
            scale = calculate_scale(working.width, working.height) if options.scale == 0 else int(options.scale)
            stats = ConvertStats(
                original_size=original_size,
                final_size=working.size,
                scale=scale,
                threshold=threshold,
                chars=len(text),
                utf16_bytes=byte_len,
                elapsed_seconds=time.perf_counter() - start,
                resized_times=resized_times,
            )
            return text, stats
        except ConversionOverflow:
            if not options.auto_fit_byte_limit:
                raise
            if working.width <= options.auto_fit_min_width or working.height <= options.auto_fit_min_height:
                raise ConversionError(
                    f"图片即使缩小到 {working.width}x{working.height} 仍超过字节上限，请换更简单的图或降低最大尺寸。"
                )
            working = shrink_image(working, 0.90)
            resized_times += 1
            if progress:
                progress(f"结果超限，自动缩小为 {working.width}x{working.height} 后重试")


# =========================
# GUI
# =========================

class ImageConverterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("1100x760")
        self.root.minsize(960, 650)

        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_output: str = ""
        self.last_stats: ConvertStats | None = None
        self.preview_photo = None

        # 变量
        self.source_type = tk.StringVar(value="file")
        self.file_path = tk.StringVar()
        self.url_path = tk.StringVar()
        self.output_path = tk.StringVar()

        self.auto_scale = tk.BooleanVar(value=True)
        self.custom_scale = tk.IntVar(value=26)
        self.compress = tk.BooleanVar(value=True)
        self.resize_enabled = tk.BooleanVar(value=True)
        self.max_width = tk.IntVar(value=96)
        self.max_height = tk.IntVar(value=96)
        self.max_pixels = tk.IntVar(value=50000)
        self.byte_limit = tk.IntVar(value=32767)
        self.auto_fit = tk.BooleanVar(value=True)
        self.transparent_as_space = tk.BooleanVar(value=True)
        self.alpha_threshold = tk.IntVar(value=0)
        self.newline_mode = tk.StringVar(value="literal")
        self.block_char = tk.StringVar(value="█")

        self._setup_style()
        self._create_widgets()
        self._poll_queue()

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Sub.TLabel", foreground="#555555")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Stats.TLabel", foreground="#1b5e20")

    def _create_widgets(self):
        root_frame = ttk.Frame(self.root, padding=14)
        root_frame.pack(fill=tk.BOTH, expand=True)
        root_frame.columnconfigure(0, weight=0)
        root_frame.columnconfigure(1, weight=1)
        root_frame.rowconfigure(1, weight=1)

        header = ttk.Frame(root_frame)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        title_area = ttk.Frame(header)
        title_area.grid(row=0, column=0, sticky="w")
        ttk.Label(title_area, text="图片富文本转换器", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(title_area, text="  适用于 EXILED TextToy / Hint / Broadcast 富文本", style="Sub.TLabel").pack(side=tk.LEFT, padx=8)

        disclaimer_area = ttk.Frame(header)
        disclaimer_area.grid(row=0, column=1, sticky="e")
        ttk.Label(
            disclaimer_area,
            text="⚠ 仅限娱乐 / 禁止炸服",
            foreground="#B45309",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(disclaimer_area, text="免责提示", command=self._show_disclaimer).pack(side=tk.LEFT)
        self.top_convert_btn = ttk.Button(disclaimer_area, text="开始转换", style="Accent.TButton", command=self._start_conversion)
        self.top_convert_btn.pack(side=tk.LEFT, padx=(10, 0))

        # 左侧参数较多，使用可滚动面板，避免小窗口下控件堆叠或被挤出。
        left_outer = ttk.Frame(root_frame, width=410)
        left_outer.grid(row=1, column=0, sticky="ns", padx=(0, 12))
        left_outer.grid_propagate(False)
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)

        self.left_canvas = tk.Canvas(left_outer, highlightthickness=0, borderwidth=0)
        left_scrollbar = ttk.Scrollbar(left_outer, orient=tk.VERTICAL, command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=left_scrollbar.set)
        self.left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scrollbar.grid(row=0, column=1, sticky="ns")

        left = ttk.Frame(self.left_canvas)
        self.left_canvas_window = self.left_canvas.create_window((0, 0), window=left, anchor="nw")

        def _update_scroll_region(_event=None):
            self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all"))

        def _match_canvas_width(event):
            self.left_canvas.itemconfigure(self.left_canvas_window, width=event.width)

        left.bind("<Configure>", _update_scroll_region)
        self.left_canvas.bind("<Configure>", _match_canvas_width)
        left_outer.bind("<Enter>", self._bind_left_mousewheel)
        left_outer.bind("<Leave>", self._unbind_left_mousewheel)

        right = ttk.Frame(root_frame)
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_source_frame(left)
        self._build_output_frame(left)
        self._build_option_frame(left)
        self._build_preview_frame(left)

        self._build_result_frame(right)
        self._build_log_frame(right)


    def _bind_left_mousewheel(self, _event=None):
        """鼠标进入左侧参数区后，滚轮控制左侧面板上下滚动。"""
        self.root.bind_all("<MouseWheel>", self._on_left_mousewheel)
        self.root.bind_all("<Button-4>", self._on_left_mousewheel)
        self.root.bind_all("<Button-5>", self._on_left_mousewheel)

    def _unbind_left_mousewheel(self, _event=None):
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_left_mousewheel(self, event):
        if not hasattr(self, "left_canvas"):
            return
        if getattr(event, "num", None) == 4:
            self.left_canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.left_canvas.yview_scroll(1, "units")
        else:
            self.left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _show_disclaimer(self):
        messagebox.showwarning("使用免责声明", DISCLAIMER_TEXT)

    def _build_source_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="1. 图片来源", padding=10)
        frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Radiobutton(frame, text="使用本地图片", variable=self.source_type, value="file", command=self._toggle_source).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(frame, text="使用 URL 图片", variable=self.source_type, value="url", command=self._toggle_source).grid(row=0, column=1, sticky="w", padx=12)

        ttk.Label(frame, text="本地图片路径：").grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.file_entry = ttk.Entry(frame, textvariable=self.file_path, width=48)
        self.file_entry.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        ttk.Button(frame, text="浏览图片", command=self._browse_file).grid(row=2, column=2, padx=(8, 0), pady=(2, 4))

        ttk.Label(frame, text="URL 图片地址：").grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.url_entry = ttk.Entry(frame, textvariable=self.url_path, width=48)
        self.url_entry.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 2))
        ttk.Button(frame, text="清空 URL", command=lambda: self.url_path.set("")).grid(row=4, column=2, padx=(8, 0), pady=(2, 2))

        self.source_hint_label = ttk.Label(frame, text="当前使用：本地图片路径", style="Sub.TLabel")
        self.source_hint_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        frame.columnconfigure(1, weight=1)
        self._toggle_source()

    def _build_option_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="3. 转换参数", padding=10)
        frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Checkbutton(frame, text="自动计算 <size>", variable=self.auto_scale, command=self._toggle_scale).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="手动 size%：").grid(row=1, column=0, sticky="w", pady=3)
        self.scale_spin = ttk.Spinbox(frame, from_=1, to=100, textvariable=self.custom_scale, width=8)
        self.scale_spin.grid(row=1, column=1, sticky="w", pady=3)

        ttk.Checkbutton(frame, text="启用颜色压缩", variable=self.compress).grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="透明像素输出空格", variable=self.transparent_as_space).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(frame, text="Alpha 阈值：").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Spinbox(frame, from_=0, to=255, textvariable=self.alpha_threshold, width=8).grid(row=4, column=1, sticky="w", pady=3)

        ttk.Separator(frame).grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)

        ttk.Checkbutton(frame, text="转换前自动缩放图片", variable=self.resize_enabled).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="最大宽度：").grid(row=7, column=0, sticky="w", pady=3)
        ttk.Spinbox(frame, from_=1, to=512, textvariable=self.max_width, width=8).grid(row=7, column=1, sticky="w", pady=3)
        ttk.Label(frame, text="最大高度：").grid(row=8, column=0, sticky="w", pady=3)
        ttk.Spinbox(frame, from_=1, to=512, textvariable=self.max_height, width=8).grid(row=8, column=1, sticky="w", pady=3)
        ttk.Label(frame, text="像素总数上限：").grid(row=9, column=0, sticky="w", pady=3)
        ttk.Spinbox(frame, from_=1, to=500000, increment=1000, textvariable=self.max_pixels, width=10).grid(row=9, column=1, sticky="w", pady=3)

        ttk.Separator(frame).grid(row=10, column=0, columnspan=2, sticky="ew", pady=8)

        ttk.Label(frame, text="UTF-16 字节上限：").grid(row=11, column=0, sticky="w", pady=3)
        ttk.Spinbox(frame, from_=1000, to=200000, increment=1000, textvariable=self.byte_limit, width=10).grid(row=11, column=1, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="超限时自动缩小重试", variable=self.auto_fit).grid(row=12, column=0, columnspan=2, sticky="w", pady=3)

        ttk.Label(frame, text="换行输出：").grid(row=13, column=0, sticky="w", pady=3)
        ttk.Combobox(frame, textvariable=self.newline_mode, width=14, state="readonly", values=("literal", "actual")).grid(row=13, column=1, sticky="w", pady=3)
        ttk.Label(frame, text="literal = 输出 \\n；actual = 真实换行", style="Sub.TLabel").grid(row=14, column=0, columnspan=2, sticky="w")

        ttk.Label(frame, text="像素字符：").grid(row=15, column=0, sticky="w", pady=3)
        ttk.Entry(frame, textvariable=self.block_char, width=8).grid(row=15, column=1, sticky="w", pady=3)
        self._toggle_scale()

    def _build_output_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="2. 输出与转换", padding=10)
        frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(frame, text="输出文本保存路径：").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(frame, textvariable=self.output_path, width=44).grid(row=1, column=0, sticky="ew", pady=(2, 0))
        ttk.Button(frame, text="保存到...", command=self._browse_output).grid(row=1, column=1, padx=(8, 0), pady=(2, 0))
        frame.columnconfigure(0, weight=1)

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.convert_btn = ttk.Button(btns, text="开始转换", style="Accent.TButton", command=self._start_conversion)
        self.convert_btn.pack(side=tk.LEFT)
        self.copy_btn = ttk.Button(btns, text="复制结果", command=self._copy_result, state=tk.DISABLED)
        self.copy_btn.pack(side=tk.LEFT, padx=6)
        self.save_btn = ttk.Button(btns, text="保存当前结果", command=self._save_current_result, state=tk.DISABLED)
        self.save_btn.pack(side=tk.LEFT)

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _build_preview_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="图片预览", padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        self.preview_label = ttk.Label(frame, text="选择本地图片后会显示预览", anchor="center")
        self.preview_label.pack(fill=tk.BOTH, expand=True)

    def _build_result_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="转换结果预览", padding=10)
        frame.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        self.stats_label = ttk.Label(frame, text="暂无结果", style="Stats.TLabel")
        self.stats_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.result_text = ScrolledText(frame, height=15, wrap=tk.NONE, font=("Consolas", 9))
        self.result_text.grid(row=1, column=0, sticky="nsew")

    def _build_log_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="日志", padding=10)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.log_text = ScrolledText(frame, height=10, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        btns = ttk.Frame(frame)
        btns.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="清空日志", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(btns, text="打开输出目录", command=self._open_output_folder).pack(side=tk.LEFT, padx=6)

    def _toggle_source(self):
        is_file = self.source_type.get() == "file"
        self.file_entry.configure(state=tk.NORMAL if is_file else tk.DISABLED)
        self.url_entry.configure(state=tk.DISABLED if is_file else tk.NORMAL)
        if hasattr(self, "source_hint_label"):
            self.source_hint_label.configure(
                text="当前使用：本地图片路径" if is_file else "当前使用：URL 图片地址"
            )

    def _toggle_scale(self):
        self.scale_spin.configure(state=tk.DISABLED if self.auto_scale.get() else tk.NORMAL)

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="选择图片文件",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.file_path.set(path)
        if not self.output_path.get():
            self.output_path.set(str(Path(path).with_suffix(".txt")))
        self._show_preview(path)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="保存文本文件",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if path:
            self.output_path.set(path)

    def _show_preview(self, path: str):
        try:
            image = Image.open(path)
            image.thumbnail((260, 220), Image.Resampling.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self.preview_photo, text="")
        except Exception as exc:
            self.preview_label.configure(image="", text=f"预览失败：{exc}")

    def _read_options(self) -> ConvertOptions:
        block = self.block_char.get() or "█"
        block = block[0]
        return ConvertOptions(
            scale=0 if self.auto_scale.get() else int(self.custom_scale.get()),
            compress=bool(self.compress.get()),
            max_pixels=int(self.max_pixels.get()),
            byte_limit=int(self.byte_limit.get()),
            resize_enabled=bool(self.resize_enabled.get()),
            max_width=int(self.max_width.get()),
            max_height=int(self.max_height.get()),
            auto_fit_byte_limit=bool(self.auto_fit.get()),
            alpha_threshold=int(self.alpha_threshold.get()),
            transparent_as_space=bool(self.transparent_as_space.get()),
            newline_mode=self.newline_mode.get(),
            block_char=block,
        )

    def _start_conversion(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("正在转换", "当前已有转换任务正在运行。")
            return

        source = self.source_type.get()
        file_path = self.file_path.get().strip()
        url = self.url_path.get().strip()

        if source == "file" and not file_path:
            messagebox.showerror("缺少输入", "请选择图片文件。")
            return
        if source == "url" and not url:
            messagebox.showerror("缺少输入", "请输入图片 URL。")
            return

        try:
            options = self._read_options()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self._set_busy(True)
        self._log("开始转换任务。")
        self.last_output = ""
        self.last_stats = None
        self.copy_btn.configure(state=tk.DISABLED)
        self.save_btn.configure(state=tk.DISABLED)
        self.result_text.delete("1.0", tk.END)
        self.stats_label.configure(text="正在转换...")

        self.worker = threading.Thread(
            target=self._worker_convert,
            args=(source, file_path, url, options, self.output_path.get().strip()),
            daemon=True,
        )
        self.worker.start()

    def _worker_convert(self, source: str, file_path: str, url: str, options: ConvertOptions, output_path: str):
        try:
            self.queue.put(("log", "正在加载图片..."))
            image = load_image_from_file(file_path) if source == "file" else load_image_from_url(url, options)
            self.queue.put(("log", f"图片加载成功：{image.width}x{image.height}，模式 {image.mode}"))

            def progress(msg: str):
                self.queue.put(("log", msg))

            text, stats = convert_image_to_text(image, options, progress=progress)

            if output_path:
                Path(output_path).write_text(text, encoding="utf-8")
                self.queue.put(("log", f"已保存到：{output_path}"))

            self.queue.put(("done", text, stats))
        except Exception as exc:
            self.queue.put(("error", str(exc)))

    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "done":
                    _, text, stats = msg
                    self._on_done(text, stats)
                elif kind == "error":
                    self._on_error(msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _on_done(self, text: str, stats: ConvertStats):
        self._set_busy(False)
        self.last_output = text
        self.last_stats = stats

        preview = text if len(text) <= 20000 else text[:20000] + "\n\n...【预览已截断，复制/保存仍为完整内容】"
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, preview)

        self.stats_label.configure(
            text=(
                f"原图 {stats.original_size[0]}x{stats.original_size[1]} → 输出 {stats.final_size[0]}x{stats.final_size[1]} | "
                f"size={stats.scale}% | 阈值={stats.threshold:.1f} | 字符={stats.chars} | UTF-16={stats.utf16_bytes} bytes | "
                f"耗时={stats.elapsed_seconds:.2f}s | 缩放次数={stats.resized_times}"
            )
        )
        self.copy_btn.configure(state=tk.NORMAL)
        self.save_btn.configure(state=tk.NORMAL)
        self._log("转换完成。")
        messagebox.showinfo("转换完成", "图片已成功转换为富文本。")

    def _on_error(self, error: str):
        self._set_busy(False)
        self.stats_label.configure(text="转换失败")
        self._log(error, level="ERROR")
        messagebox.showerror("转换失败", error)

    def _set_busy(self, busy: bool):
        self.root.configure(cursor="watch" if busy else "")
        state = tk.DISABLED if busy else tk.NORMAL
        self.convert_btn.configure(state=state)
        if hasattr(self, "top_convert_btn"):
            self.top_convert_btn.configure(state=state)
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    def _copy_result(self):
        if not self.last_output:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_output)
        self._log("已复制完整结果到剪贴板。")

    def _save_current_result(self):
        if not self.last_output:
            return
        path = self.output_path.get().strip()
        if not path:
            path = filedialog.asksaveasfilename(
                title="保存文本文件",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            )
            if not path:
                return
            self.output_path.set(path)
        try:
            Path(path).write_text(self.last_output, encoding="utf-8")
            self._log(f"已保存当前结果到：{path}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _open_output_folder(self):
        path = self.output_path.get().strip()
        folder = Path(path).parent if path else Path.cwd()
        if not folder.exists():
            messagebox.showwarning("目录不存在", str(folder))
            return
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except AttributeError:
            webbrowser.open(folder.as_uri())

    def _log(self, message: str, level: str = "INFO"):
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] [{level}] {message}\n")
        self.log_text.see(tk.END)


def main():
    root = tk.Tk()
    app = ImageConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
