import colorsys
import threading
import tkinter as tk
from io import BytesIO
from tkinter import filedialog, messagebox, ttk

import requests
from PIL import Image


# ========== 转换核心逻辑 ==========
def color_difference(c1, c2):
    """计算两个RGB颜色在HSV空间的差异（与原C#逻辑一致）"""
    r1, g1, b1 = c1[:3]
    r2, g2, b2 = c2[:3]
    h1, s1, v1 = colorsys.rgb_to_hsv(r1/255.0, g1/255.0, b1/255.0)
    h2, s2, v2 = colorsys.rgb_to_hsv(r2/255.0, g2/255.0, b2/255.0)
    h1 *= 360
    h2 *= 360
    s1 *= 100
    s2 *= 100
    v1 *= 100
    v2 *= 100

    dh = abs(h1 - h2)
    if dh > 180:
        dh = 360 - dh
    ds = abs(s1 - s2)
    db = abs(v1 - v2)
    return (dh * 0.755 + ds * 2.0 + db * 0.7) / 3.0

def calculate_scale(width, height):
    """自动计算缩放值"""
    avg = (width + height) / 2.0
    capped_avg = 45 if avg > 60 else avg
    return int((-0.47 * capped_avg) + 28.72)

def convert_image_to_text(image, scale, compress):
    """
    将PIL Image对象转换为富文本字符串
    返回: (成功标志, 文本字符串 或 错误信息)
    """
    # 检查像素总数
    if image.width * image.height > 50000:
        return False, "图片过大（像素超过50000），请缩小图片尺寸。建议≤100x100。"

    # 确定缩放值
    if scale == 0:
        scale = calculate_scale(image.width, image.height)

    line_height = 100 - scale
    size_tag = f"<size={scale}%><line-height={line_height}%>"

    threshold = 0.0
    max_attempts = 5

    # 确保图像支持透明度检测
    original_mode = image.mode
    if 'A' not in original_mode:
        # 如果没有 Alpha 通道，则视为完全不透明
        image_rgba = image.convert('RGBA')
    else:
        image_rgba = image.convert('RGBA')

    pixels = image_rgba.load()

    while threshold < max_attempts * 0.5:  # 每次+0.5，最多5次
        lines = []
        last_color = None  # 上一个非透明像素的颜色 (r,g,b,a)

        for y in range(image.height):
            line_chars = []
            for x in range(image.width):
                r, g, b, a = pixels[x, y]

                # 处理透明像素：输出空格，不保留颜色
                if a == 0:
                    # 如果上一个像素有颜色，需要先闭合颜色标签
                    if last_color is not None:
                        line_chars.append('</color>')
                        last_color = None
                    line_chars.append(' ')  # 空格占位
                    continue

                # 非透明像素
                current_color = (r, g, b, a)

                if last_color is None:
                    # 上一个像素是透明或无颜色，开始新颜色
                    line_chars.append(f'<color=#{r:02X}{g:02X}{b:02X}{a:02X}>█')
                    last_color = current_color
                else:
                    # 比较与上一个颜色的差异
                    if current_color != last_color:
                        diff = color_difference(current_color, last_color)
                        if compress and diff > threshold:
                            # 差异大于阈值，换颜色
                            line_chars.append(f'</color><color=#{r:02X}{g:02X}{b:02X}{a:02X}>█')
                            last_color = current_color
                        else:
                            # 差异小于等于阈值，沿用上一个颜色
                            line_chars.append('█')
                    else:
                        line_chars.append('█')
            lines.append(''.join(line_chars) + '\\n')

        # 合并所有行，并添加结束标签
        full_text = size_tag + ''.join(lines)
        # 如果最后还有未闭合的颜色标签，需要闭合
        if last_color is not None:
            full_text += '</color>'
        full_text += '</line-height></size>'

        # 检查字节长度（UTF-16 LE）
        byte_len = len(full_text.encode('utf-16-le'))
        if byte_len <= 32768:
            return True, full_text

        threshold += 0.5
        # 重置 last_color，重新生成

    # 如果始终超限，返回纯标签（降级）
    return True, size_tag

def load_image_from_file(path):
    """从本地文件加载PIL Image"""
    try:
        return Image.open(path)
    except Exception as e:
        raise Exception(f"无法加载图片文件: {e}")

def load_image_from_url(url):
    """从URL加载PIL Image"""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return Image.open(BytesIO(response.content))
    except Exception as e:
        raise Exception(f"无法从URL下载图片: {e}")

# ========== GUI界面 ==========
class ImageConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("图片转文本转换器")
        self.root.geometry("600x500")
        self.root.resizable(True, True)

        # 变量
        self.source_type = tk.StringVar(value="file")
        self.file_path = tk.StringVar()
        self.url_path = tk.StringVar()
        self.auto_scale = tk.BooleanVar(value=True)
        self.custom_scale = tk.IntVar(value=26)
        self.compress = tk.BooleanVar(value=True)
        self.output_path = tk.StringVar()

        # 控件引用
        self.file_entry = None
        self.url_entry = None

        # 创建界面
        self.create_widgets()

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 图片来源选择
        source_frame = ttk.LabelFrame(main_frame, text="图片来源", padding="5")
        source_frame.pack(fill=tk.X, pady=5)

        ttk.Radiobutton(source_frame, text="本地文件", variable=self.source_type,
                        value="file", command=self.toggle_source).grid(row=0, column=0, sticky=tk.W)
        ttk.Radiobutton(source_frame, text="URL", variable=self.source_type,
                        value="url", command=self.toggle_source).grid(row=0, column=1, sticky=tk.W)

        # 文件选择
        file_frame = ttk.Frame(source_frame)
        file_frame.grid(row=1, column=0, columnspan=3, sticky=tk.W+tk.E, pady=2)
        self.file_entry = ttk.Entry(file_frame, textvariable=self.file_path, width=50)
        self.file_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(file_frame, text="浏览...", command=self.browse_file).pack(side=tk.LEFT)

        # URL输入
        url_frame = ttk.Frame(source_frame)
        url_frame.grid(row=2, column=0, columnspan=3, sticky=tk.W+tk.E, pady=2)
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url_path, width=50)
        self.url_entry.pack(side=tk.LEFT, padx=5)

        # 初始状态
        self.toggle_source()

        # 缩放选项
        scale_frame = ttk.LabelFrame(main_frame, text="缩放设置", padding="5")
        scale_frame.pack(fill=tk.X, pady=5)

        ttk.Checkbutton(scale_frame, text="自动计算缩放", variable=self.auto_scale,
                        command=self.toggle_scale).grid(row=0, column=0, columnspan=2, sticky=tk.W)

        ttk.Label(scale_frame, text="自定义缩放值 (%):").grid(row=1, column=0, sticky=tk.W, padx=20)
        self.scale_entry = ttk.Entry(scale_frame, textvariable=self.custom_scale, width=10, state='disabled')
        self.scale_entry.grid(row=1, column=1, sticky=tk.W)

        # 压缩选项
        ttk.Checkbutton(scale_frame, text="启用颜色压缩 (减小文本大小)", variable=self.compress).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=5)

        # 输出文件
        output_frame = ttk.LabelFrame(main_frame, text="输出文件", padding="5")
        output_frame.pack(fill=tk.X, pady=5)

        self.output_entry = ttk.Entry(output_frame, textvariable=self.output_path, width=50)
        self.output_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(output_frame, text="浏览...", command=self.browse_output).pack(side=tk.LEFT)

        # 转换按钮
        ttk.Button(main_frame, text="开始转换", command=self.start_conversion).pack(pady=10)

        # 日志输出
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = tk.Text(log_frame, height=10, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(self.log_text, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=scrollbar.set)

    def toggle_source(self):
        """根据图片来源类型启用/禁用相应输入"""
        if self.source_type.get() == "file":
            self.file_entry.config(state='normal')
            self.url_entry.config(state='disabled')
        else:
            self.file_entry.config(state='disabled')
            self.url_entry.config(state='normal')

    def toggle_scale(self):
        """根据自动缩放选项启用/禁用自定义缩放输入"""
        if self.auto_scale.get():
            self.scale_entry.config(state='disabled')
        else:
            self.scale_entry.config(state='normal')

    def browse_file(self):
        path = filedialog.askopenfilename(
            title="选择图片文件",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif"), ("所有文件", "*.*")]
        )
        if path:
            self.file_path.set(path)

    def browse_output(self):
        path = filedialog.asksaveasfilename(
            title="保存文本文件",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if path:
            self.output_path.set(path)

    def log(self, message, level="info"):
        """向日志区域添加信息"""
        self.log_text.insert(tk.END, f"[{level.upper()}] {message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def start_conversion(self):
        """启动转换（在子线程中执行，避免界面卡死）"""
        # 验证输入
        source = self.source_type.get()
        if source == "file" and not self.file_path.get():
            messagebox.showerror("错误", "请选择图片文件")
            return
        if source == "url" and not self.url_path.get():
            messagebox.showerror("错误", "请输入图片URL")
            return
        if not self.output_path.get():
            messagebox.showerror("错误", "请指定输出文件路径")
            return

        # 获取参数
        scale = 0 if self.auto_scale.get() else self.custom_scale.get()
        compress = self.compress.get()

        # 禁用按钮，防止重复点击
        self.root.config(cursor="watch")
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Button):
                child.config(state='disabled')

        self.log("开始转换任务...")

        # 启动线程
        thread = threading.Thread(target=self.conversion_thread,
                                   args=(source, self.file_path.get(), self.url_path.get(),
                                         scale, compress, self.output_path.get()))
        thread.daemon = True
        thread.start()

    def conversion_thread(self, source, file_path, url, scale, compress, output_path):
        try:
            # 加载图片
            self.log("正在加载图片...")
            if source == "file":
                image = load_image_from_file(file_path)
            else:
                image = load_image_from_url(url)

            self.log(f"图片加载成功: {image.width}x{image.height}")

            # 转换
            self.log("正在转换图片为文本...")
            success, result = convert_image_to_text(image, scale, compress)

            if not success:
                self.log(f"转换失败: {result}", "error")
                messagebox.showerror("转换失败", result)
                return

            # 写入文件
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(result)

            self.log(f"转换完成！文本已保存至: {output_path}")
            self.log(f"文本长度: {len(result)} 字符, UTF-16字节数: {len(result.encode('utf-16-le'))}")
            messagebox.showinfo("完成", f"转换成功！\n输出文件: {output_path}")

        except Exception as e:
            self.log(f"发生错误: {str(e)}", "error")
            messagebox.showerror("错误", str(e))
        finally:
            # 恢复界面
            self.root.after(0, self.finish_conversion)

    def finish_conversion(self):
        self.root.config(cursor="")
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Button):
                child.config(state='normal')
        self.log("任务结束。\n")

# ========== 程序入口 ==========
if __name__ == "__main__":
    root = tk.Tk()
    app = ImageConverterApp(root)
    root.mainloop()