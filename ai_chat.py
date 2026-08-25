# -*- coding: utf-8 -*-
"""
AI 创作工作台 —— Agnes 全家桶
================================
功能：
  1. 对话：连接 agnes-2.5-flash，支持加载本地 .md 文件作为系统提示词，
     支持上传本地照片让 AI 看图回答（多模态理解），流式输出。
  2. 生图：连接 agnes-image-2.1-flash，支持文生图 / 图生图（可传本地照片）。
  3. 生视频：连接 agnes-video-v2.0，支持文生视频 / 图生视频，完成后自动保存并播放。

纯 Python 标准库实现，无需安装任何第三方依赖。
运行：python ai_chat.py（或双击本文件）

说明：
  - 首次运行需输入 API 密钥并选择服务区（.com 国际区 / .cn 国区），
    密钥校验通过后加密保存到本地资源文件，之后每次启动自动加载。
  - 图生视频要求首帧图片是公网可访问的 URL。选择"本地照片"作为首帧时，
    程序会先免费调用图片接口把照片转存为公网图片，再用于生成视频。
  - 视频生成接口有每分钟 1 次的频率限制，连续生成时请稍等片刻。
"""

import base64
import hashlib
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from tkinter import filedialog, messagebox, ttk

# ======================== 配置区 ========================
# API 密钥与服务区不再硬编码：首次使用时弹窗输入密钥并选择服务区
# （.com 国际区 / .cn 国区），校验通过后加密保存到本地资源文件，
# 之后每次启动自动解密加载到下面的变量中。
API_KEY = ""          # 运行时变量：从加密资源文件读取
REGION = ""           # "com"（国际区）或 "cn"（国区）
BASE_URL = ""         # OpenAI 兼容接口，按服务区生成
ROOT_URL = ""         # 视频任务查询接口所在根地址
CHAT_MODEL = "agnes-2.5-flash"               # 对话/看图模型
IMAGE_MODEL = "agnes-image-2.1-flash"        # 图片生成模型
VIDEO_MODEL = "agnes-video-v2.0"             # 视频生成模型（当前免费）
DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的中文 AI 助手，回答准确、简洁、有条理。"
MAX_PHOTO_BYTES = 8 * 1024 * 1024            # 上传照片大小上限（8MB）
VIDEO_POLL_INTERVAL = 3                      # 视频轮询间隔（秒）
VIDEO_POLL_TIMEOUT = 20 * 60                 # 视频轮询总超时（秒）
# =======================================================

# 输出目录：打包为 EXE 时取可执行文件所在目录，脚本运行时取脚本所在目录
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_APP_DIR, "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(_APP_DIR, "ai_chat_config.dat")  # 加密后的密钥资源文件

IMAGE_FILETYPES = [("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("所有文件", "*.*")]

# 视频时长选项：标签 -> (帧数, 帧率)。帧数遵循 8n+1 且 ≤441
VIDEO_DURATIONS = {
    "约 3 秒": (81, 24),
    "约 5 秒": (121, 24),
    "约 10 秒": (241, 24),
    "约 18 秒": (441, 24),
}
# 视频画幅选项：标签 -> (宽, 高)
VIDEO_SIZES = {
    "横屏 16:9": (1152, 768),
    "竖屏 9:16": (768, 1152),
    "方形 1:1": (960, 960),
}
IMAGE_SIZES = ["1K", "2K"]
IMAGE_RATIOS = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"]


# ======================== 加密资源文件与首次设置 ========================
# 密钥不以明文保存：用固定口令派生密钥流，对配置做流式异或加密后
# 写入资源文件（ai_chat_config.dat），并附带校验和防止文件被改动。

_CONFIG_PASSPHRASE = "Agnes-AI-Workbench#LocalConfig#2026"
_CONFIG_SALT = b"agnes/ai/chat/workbench/v1"


def _config_seed():
    return hashlib.sha256(_CONFIG_PASSPHRASE.encode("utf-8") + _CONFIG_SALT).digest()


def _keystream(length, seed):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def _xor_bytes(data, seed):
    stream = _keystream(len(data), seed)
    return bytes(b ^ s for b, s in zip(data, stream))


def save_config(api_key, region):
    """加密保存密钥与服务区到资源文件。"""
    data = {
        "v": 1,
        "api_key": api_key,
        "region": region,
        "chk": hashlib.sha256((api_key + "|" + region).encode("utf-8")).hexdigest(),
    }
    plain = json.dumps(data, ensure_ascii=False).encode("utf-8")
    with open(CONFIG_PATH, "wb") as f:
        f.write(base64.b64encode(_xor_bytes(plain, _config_seed())))


def load_config():
    """读取并解密资源文件；文件缺失、损坏或被篡改时返回 None。"""
    try:
        with open(CONFIG_PATH, "rb") as f:
            blob = f.read()
        data = json.loads(_xor_bytes(base64.b64decode(blob), _config_seed()).decode("utf-8"))
        api_key = data["api_key"]
        region = data["region"]
        if region not in ("cn", "com"):
            return None
        chk = hashlib.sha256((api_key + "|" + region).encode("utf-8")).hexdigest()
        if chk != data.get("chk"):
            return None
        return {"api_key": api_key, "region": region}
    except Exception:
        return None


def apply_config(api_key, region):
    """把密钥与服务区写入运行时变量，并按服务区生成接口地址。"""
    global API_KEY, REGION, BASE_URL, ROOT_URL
    API_KEY = api_key
    REGION = region
    host = "api.agnes-ai.com" if region == "com" else "api.agnes-ai.cn"
    ROOT_URL = f"https://{host}"
    BASE_URL = ROOT_URL + "/v1"


def verify_api_key(api_key, region):
    """调用 /v1/models 校验密钥。返回 (是否通过, 原因)。
    原因："invalid" 表示密钥无效；"network:..." 表示网络问题。"""
    host = "api.agnes-ai.com" if region == "com" else "api.agnes-ai.cn"
    req = urllib.request.Request(f"https://{host}/v1/models",
                                 headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200, None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "invalid"
        return True, None  # 其他状态码不属于密钥问题
    except Exception as e:
        return False, f"network:{e}"


class SetupDialog(tk.Toplevel):
    """首次使用：输入 API 密钥并选择服务区（.com 国际区 / .cn 国区）。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("首次使用设置")
        self.resizable(False, False)
        self.result = None  # 成功后为 (api_key, region)
        self.grab_set()

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="请输入您的 API 密钥：").grid(row=0, column=0, columnspan=2,
                                                          sticky="w", pady=(0, 4))
        self.key_entry = ttk.Entry(body, show="*", width=52, font=("Consolas", 10))
        self.key_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        ttk.Label(body, text="请选择 API 服务区：").grid(row=2, column=0, columnspan=2,
                                                          sticky="w", pady=(0, 4))
        self.region_var = tk.StringVar(value="cn")
        ttk.Radiobutton(body, text=".cn 国区（api.agnes-ai.cn）", value="cn",
                        variable=self.region_var).grid(row=3, column=0, sticky="w", padx=(4, 12))
        ttk.Radiobutton(body, text=".com 国际区（api.agnes-ai.com）", value="com",
                        variable=self.region_var).grid(row=3, column=1, sticky="w")

        hint = ("密钥验证通过后将加密保存到本地资源文件，下次启动自动加载。\n"
                "取消将退出程序。")
        ttk.Label(body, text=hint, foreground="#888").grid(row=4, column=0, columnspan=2,
                                                            sticky="w", pady=(12, 10))

        self.confirm_btn = ttk.Button(body, text="验证并保存", command=self.on_confirm)
        self.confirm_btn.grid(row=5, column=0, sticky="e", padx=(0, 8))
        ttk.Button(body, text="退出程序", command=self.on_cancel).grid(row=5, column=1, sticky="w")

        self.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.key_entry.focus_set()

    def on_confirm(self):
        key = self.key_entry.get().strip()
        if not key:
            messagebox.showwarning("缺少密钥", "请输入 API 密钥。", parent=self)
            return
        region = self.region_var.get()
        self.confirm_btn.config(state="disabled", text="正在验证…")
        self.update_idletasks()
        ok, reason = verify_api_key(key, region)
        self.confirm_btn.config(state="normal", text="验证并保存")
        if not ok and reason == "invalid":
            messagebox.showerror("错误", "无效的API密钥", parent=self)
            return
        if not ok:
            messagebox.showerror("网络错误",
                                 "无法连接 API 服务器，请检查网络后重试。\n" + str(reason)[8:],
                                 parent=self)
            return
        save_config(key, region)
        self.result = (key, region)
        self.destroy()

    def on_cancel(self):
        self.result = None
        self.destroy()


def bootstrap_config(root):
    """启动时加载加密配置；首次使用则弹出设置窗口。成功返回 True。"""
    data = load_config()
    if data:
        apply_config(data["api_key"], data["region"])
        return True
    dlg = SetupDialog(root)
    root.wait_window(dlg)
    if dlg.result:
        apply_config(*dlg.result)
        return True
    return False


# ======================== 网络与工具函数 ========================

def http_request(method, url, payload=None, timeout=120):
    """发起请求，返回 (状态码, 解析后的JSON或文本)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        return e.code, detail or str(e)
    except Exception as e:
        return -1, str(e)


def download_bytes(url, timeout=300):
    """下载文件内容为字节。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def image_to_data_uri(path):
    """把本地图片读成 Data URI（base64），供接口直接接收。"""
    size = os.path.getsize(path)
    if size > MAX_PHOTO_BYTES:
        raise ValueError(f"图片过大（{size / 1024 / 1024:.1f}MB），上限 {MAX_PHOTO_BYTES // 1024 // 1024}MB")
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
    }.get(os.path.splitext(path)[1].lower(), "image/png")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def unique_path(prefix, ext):
    """在输出目录生成带时间戳的唯一文件名。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(OUTPUT_DIR, f"{prefix}_{stamp}{ext}")


def api_error_message(code, body):
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    hint = ""
    if code == 429:
        hint = "（请求过于频繁，视频接口限制每分钟 1 次，请稍后重试）"
    elif code in (401, 403):
        hint = "（密钥无效、权限不足或账户额度为 0）"
    return f"HTTP {code} {hint}\n{text[:500]}"


# ======================== 后台工作线程 ========================

def chat_worker(messages, ev):
    """流式对话。ev 为事件队列：(类型, 数据)。"""
    payload = {"model": CHAT_MODEL, "messages": messages, "stream": True}
    status, body = None, None
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    piece = chunk["choices"][0].get("delta", {}).get("content")
                    if piece:
                        ev.put(("chat_piece", piece))
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        ev.put(("chat_done", None))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        ev.put(("chat_error", f"请求失败 HTTP {e.code}：{detail}"))
    except Exception as e:
        ev.put(("chat_error", f"请求失败：{e}"))


def image_worker(prompt, size, ratio, ref_images, ev):
    """
    生成图片。
    ref_images：Data URI 或公网 URL 列表（图生图/多图合成），可为空。
    成功事件：("img_done", {"bytes":..., "url":..., "prompt":...})
    """
    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "size": size,
        "ratio": ratio,
        "extra_body": {"response_format": "url"},
    }
    if ref_images:
        payload["extra_body"]["image"] = ref_images

    ev.put(("img_status", "正在生成图片，通常需要几秒到一分钟…"))
    status, body = http_request("POST", BASE_URL + "/images/generations", payload, timeout=360)
    if status != 200 or not isinstance(body, dict):
        ev.put(("img_error", api_error_message(status, body)))
        return

    url = None
    try:
        url = body["data"][0].get("url")
        b64 = body["data"][0].get("b64_json")
    except (KeyError, IndexError, TypeError):
        pass
    try:
        if url:
            img_bytes = download_bytes(url)
        elif b64:
            img_bytes = base64.b64decode(b64)
        else:
            ev.put(("img_error", "接口返回中没有图片数据"))
            return
    except Exception as e:
        ev.put(("img_error", f"下载生成的图片失败：{e}"))
        return

    save_path = unique_path("image", ".png")
    with open(save_path, "wb") as f:
        f.write(img_bytes)
    ev.put(("img_done", {"bytes": img_bytes, "url": url or "", "path": save_path, "prompt": prompt}))


def host_local_image(local_path, ev):
    """
    把本地照片转存为公网图片 URL（免费走图片接口的图生图）。
    用于满足视频接口"首帧必须公网可访问"的要求。
    """
    ev.put(("vid_status", "正在将本地照片转存为在线图片（生视频的必要步骤）…"))
    payload = {
        "model": IMAGE_MODEL,
        "prompt": "请原样保留这张图片的全部内容、构图和色彩，不要做任何修改",
        "size": "1K",
        "extra_body": {
            "image": [image_to_data_uri(local_path)],
            "response_format": "url",
        },
    }
    status, body = http_request("POST", BASE_URL + "/images/generations", payload, timeout=360)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError("转存照片失败：" + api_error_message(status, body))
    url = body.get("data", [{}])[0].get("url")
    if not url:
        raise RuntimeError("转存照片失败：接口未返回图片地址")
    return url


def video_worker(prompt, duration_label, size_label, first_frame, ev):
    """
    生成视频（异步任务：创建 -> 轮询 -> 下载）。
    first_frame：None / ("url", 公网地址) / ("file", 本地路径)
    成功事件：("vid_done", {"path":..., "url":...})
    """
    try:
        num_frames, frame_rate = VIDEO_DURATIONS[duration_label]
        width, height = VIDEO_SIZES[size_label]

        image_url = None
        if first_frame and first_frame[0] == "url":
            image_url = first_frame[1].strip()
            if not image_url.startswith("http"):
                raise RuntimeError("请填写以 http 开头的公网图片地址")
        elif first_frame and first_frame[0] == "file":
            image_url = host_local_image(first_frame[1], ev)

        payload = {
            "model": VIDEO_MODEL,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "frame_rate": frame_rate,
        }
        if image_url:
            payload["image"] = image_url

        ev.put(("vid_status", "正在提交视频生成任务…"))
        status, body = http_request("POST", BASE_URL + "/videos", payload, timeout=60)
        if status != 200 or not isinstance(body, dict):
            ev.put(("vid_error", "创建视频任务失败：" + api_error_message(status, body)))
            return
        video_id = body.get("video_id")
        if not video_id:
            ev.put(("vid_error", f"创建视频任务失败：响应中没有 video_id\n{str(body)[:300]}"))
            return

        # 轮询任务状态
        ev.put(("vid_progress", 0))
        query_url = f"{ROOT_URL}/agnesapi?video_id={urllib.request.quote(video_id)}"
        deadline = time.time() + VIDEO_POLL_TIMEOUT
        last_poll = 0.0
        while time.time() < deadline:
            time.sleep(max(0.5, VIDEO_POLL_INTERVAL - (time.time() - last_poll)))
            last_poll = time.time()
            s, d = http_request("GET", query_url, timeout=30)
            if s == 429:  # 查询限流，退避等待
                ev.put(("vid_status", "查询太频繁，稍候继续…"))
                time.sleep(8)
                continue
            if s != 200 or not isinstance(d, dict):
                ev.put(("vid_status", f"查询异常（{s}），将重试…"))
                time.sleep(5)
                continue
            st = d.get("status")
            progress = d.get("progress") or 0
            ev.put(("vid_progress", int(progress)))
            ev.put(("vid_status", f"生成中… {progress}%（状态：{st}）"))
            if st == "completed":
                url = d.get("url") or (d.get("metadata") or {}).get("url")
                if not url:
                    ev.put(("vid_error", "任务完成但未返回视频地址"))
                    return
                ev.put(("vid_status", "视频生成完成，正在下载…"))
                video_bytes = download_bytes(url, timeout=600)
                save_path = unique_path("video", ".mp4")
                with open(save_path, "wb") as f:
                    f.write(video_bytes)
                ev.put(("vid_done", {"path": save_path, "url": url}))
                return
            if st == "failed":
                err = d.get("error") or {}
                ev.put(("vid_error", "视频生成失败：" + str(err)[:300]))
                return
        ev.put(("vid_error", f"视频生成超时（超过 {VIDEO_POLL_TIMEOUT // 60} 分钟未完成）"))
    except Exception as e:
        ev.put(("vid_error", str(e)))


# ======================== 图形界面 ========================

class App:
    def __init__(self, root):
        self.root = root
        root.title("AI 创作工作台 · Agnes")
        root.geometry("900x640")
        root.minsize(720, 520)

        self.events = queue.Queue()          # 所有后台线程共用的事件队列
        self.system_prompt = DEFAULT_SYSTEM_PROMPT
        self.md_source = None                # 当前系统提示词来源文件
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.chat_busy = False
        self.pending_photo = None            # 聊天待发送的照片路径

        self._build_topbar()
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._build_chat_tab()
        self._build_image_tab()
        self._build_video_tab()
        self._build_statusbar()

        self.root.after(60, self.poll_events)

    # ---------- 顶栏：系统提示词 ----------
    def _build_topbar(self):
        bar = ttk.LabelFrame(self.root, text="系统提示词（作用于对话）")
        bar.pack(fill="x", padx=8, pady=6)
        self.md_label = ttk.Label(bar, text="当前：默认提示词", foreground="#555")
        self.md_label.pack(side="left", padx=8, pady=4)
        ttk.Button(bar, text="加载 .md 文件", command=self.load_md).pack(side="right", padx=4, pady=4)
        ttk.Button(bar, text="恢复默认", command=self.reset_md).pack(side="right", padx=4, pady=4)

    def load_md(self):
        path = filedialog.askopenfilename(
            title="选择 Markdown 文件",
            filetypes=[("Markdown 文件", "*.md *.markdown *.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            text = self._read_text_file(path)
        except Exception as e:
            messagebox.showerror("读取失败", f"无法读取该文件：{e}")
            return
        if not text.strip():
            messagebox.showwarning("文件为空", "所选文件没有内容，请选择其他文件。")
            return
        self.system_prompt = text.strip()
        self.md_source = path
        self.messages[0] = {"role": "system", "content": self.system_prompt}
        name = os.path.basename(path)
        self.md_label.config(text=f"当前：{name}（{len(self.system_prompt)} 字）")
        self.set_status(f"已加载系统提示词：{name}")

    def reset_md(self):
        self.system_prompt = DEFAULT_SYSTEM_PROMPT
        self.md_source = None
        self.messages[0] = {"role": "system", "content": self.system_prompt}
        self.md_label.config(text="当前：默认提示词")
        self.set_status("已恢复默认系统提示词")

    @staticmethod
    def _read_text_file(path):
        for enc in ("utf-8", "utf-8-sig", "gbk"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        raise ValueError("无法识别文件编码")

    # ---------- 标签页 1：对话 ----------
    def _build_chat_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  对话  ")

        self.chat_text = tk.Text(tab, wrap="word", state="disabled",
                                 font=("Microsoft YaHei UI", 11), padx=12, pady=10,
                                 spacing1=4, spacing3=4)
        self.chat_text.tag_configure("user", foreground="#0b57d0",
                                     font=("Microsoft YaHei UI", 10, "bold"))
        self.chat_text.tag_configure("assistant", foreground="#188038",
                                     font=("Microsoft YaHei UI", 10, "bold"))
        self.chat_text.tag_configure("body", foreground="#1f1f1f")
        self.chat_text.tag_configure("error", foreground="#c5221f")
        self.chat_text.pack(fill="both", expand=True, padx=6, pady=(6, 2))

        attach_row = ttk.Frame(tab)
        attach_row.pack(fill="x", padx=6)
        self.attach_label = ttk.Label(attach_row, text="", foreground="#777")
        self.attach_label.pack(side="left")
        ttk.Button(attach_row, text="移除照片", width=8,
                   command=self.clear_attachment).pack(side="right")
        ttk.Button(attach_row, text="上传照片", width=8,
                   command=self.attach_photo).pack(side="right", padx=4)

        input_row = ttk.Frame(tab)
        input_row.pack(fill="x", padx=6, pady=6)
        self.chat_entry = tk.Text(input_row, height=3, wrap="word",
                                  font=("Microsoft YaHei UI", 11))
        self.chat_entry.pack(side="left", fill="both", expand=True)
        self.chat_entry.bind("<Return>", self.on_chat_enter)
        self.chat_send_btn = ttk.Button(input_row, text="发送", width=8,
                                        command=self.send_chat)
        self.chat_send_btn.pack(side="right", padx=(8, 0), fill="y")

        self.chat_append("已连接 " + CHAT_MODEL + "。可先加载 .md 系统提示词，"
                         "或上传照片后提问。\n", "assistant")

    def attach_photo(self):
        path = filedialog.askopenfilename(title="选择照片", filetypes=IMAGE_FILETYPES)
        if not path:
            return
        try:
            if os.path.getsize(path) > MAX_PHOTO_BYTES:
                messagebox.showwarning("照片过大", "请选择 8MB 以内的图片。")
                return
            image_to_data_uri(path)  # 预检：确认可读
        except Exception as e:
            messagebox.showerror("无法使用", f"这张图片无法读取：{e}")
            return
        self.pending_photo = path
        self.attach_label.config(text=f"📷 已附加照片：{os.path.basename(path)}（发送时一并发出）")

    def clear_attachment(self):
        self.pending_photo = None
        self.attach_label.config(text="")

    def on_chat_enter(self, event):
        if event.state & 0x0001:  # Shift+Enter 换行
            return None
        self.send_chat()
        return "break"

    def send_chat(self):
        if self.chat_busy:
            return
        text = self.chat_entry.get("1.0", "end").strip()
        photo = self.pending_photo
        if not text and not photo:
            return
        self.chat_entry.delete("1.0", "end")
        self.clear_attachment()

        # 组装消息内容（纯文本或多模态）
        if photo:
            try:
                data_uri = image_to_data_uri(photo)
            except Exception as e:
                self.chat_append(f"[无法读取照片：{e}]\n", "error")
                return
            content = [
                {"type": "text", "text": text or "请描述并分析这张图片。"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
            self.chat_append(f"你：{text or '（看图）'} [已附照片 {os.path.basename(photo)}]\n", "user")
        else:
            content = text
            self.chat_append("你：" + text + "\n", "user")

        self.messages.append({"role": "user", "content": content})
        self.chat_append("AI：", "assistant")
        self.chat_busy = True
        self.chat_send_btn.config(state="disabled")
        threading.Thread(target=chat_worker, args=(list(self.messages), self.events),
                         daemon=True).start()

    # ---------- 标签页 2：生成图片 ----------
    def _build_image_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  生成图片  ")

        form = ttk.LabelFrame(tab, text="生成设置")
        form.pack(fill="x", padx=6, pady=6)

        ttk.Label(form, text="图片描述：").grid(row=0, column=0, sticky="nw", padx=6, pady=4)
        self.img_prompt = tk.Text(form, height=3, wrap="word", font=("Microsoft YaHei UI", 10))
        self.img_prompt.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=4)

        ttk.Label(form, text="清晰度：").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.img_size = ttk.Combobox(form, values=IMAGE_SIZES, state="readonly", width=8)
        self.img_size.set("1K")
        self.img_size.grid(row=1, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(form, text="比例：").grid(row=1, column=2, sticky="e", padx=6, pady=4)
        self.img_ratio = ttk.Combobox(form, values=IMAGE_RATIOS, state="readonly", width=8)
        self.img_ratio.set("1:1")
        self.img_ratio.grid(row=1, column=3, sticky="w", padx=6, pady=4)

        ttk.Button(form, text="选择参考照片（可选）", command=self.pick_img_ref).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=6, pady=4)
        self.img_ref_label = ttk.Label(form, text="未选择：将按文字描述生成", foreground="#777")
        self.img_ref_label.grid(row=3, column=1, columnspan=3, sticky="w", padx=6)

        form.columnconfigure(1, weight=1)
        self.img_ref_path = None

        self.img_gen_btn = ttk.Button(tab, text="开始生成图片", command=self.generate_image)
        self.img_gen_btn.pack(pady=4)

        result = ttk.LabelFrame(tab, text="生成结果")
        result.pack(fill="both", expand=True, padx=6, pady=6)
        self.img_canvas = tk.Canvas(result, bg="#f5f5f5", highlightthickness=0)
        self.img_canvas.pack(fill="both", expand=True)
        self.img_photo = None  # 防止图片对象被回收
        self.last_image_path = None

        btns = ttk.Frame(result)
        btns.pack(fill="x", padx=6, pady=4)
        self.img_url_label = ttk.Label(btns, text="", foreground="#777")
        self.img_url_label.pack(side="left")
        ttk.Button(btns, text="打开输出文件夹", command=lambda: os.startfile(OUTPUT_DIR)).pack(side="right")

    def pick_img_ref(self):
        path = filedialog.askopenfilename(title="选择参考照片", filetypes=IMAGE_FILETYPES)
        if not path:
            return
        self.img_ref_path = path
        self.img_ref_label.config(text=f"参考照片：{os.path.basename(path)}（将以它为底图进行编辑）")

    def generate_image(self):
        prompt = self.img_prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("缺少描述", "请先填写图片描述。")
            return
        refs = []
        if self.img_ref_path:
            try:
                refs = [image_to_data_uri(self.img_ref_path)]
            except Exception as e:
                messagebox.showerror("参考照片不可用", str(e))
                return
        self.img_gen_btn.config(state="disabled", text="生成中…")
        self.img_canvas.delete("all")
        self.img_url_label.config(text="")
        threading.Thread(
            target=image_worker,
            args=(prompt, self.img_size.get(), self.img_ratio.get(), refs, self.events),
            daemon=True).start()

    def show_generated_image(self, info):
        self.last_image_path = info["path"]
        try:
            photo = tk.PhotoImage(data=info["bytes"])
            scale = max(1, math.ceil(max(photo.width(), photo.height()) / 560))
            if scale > 1:
                photo = photo.subsample(scale, scale)
            self.img_photo = photo
            self.img_canvas.delete("all")
            self.img_canvas.create_image(self.img_canvas.winfo_width() // 2 or 300,
                                         self.img_canvas.winfo_height() // 2 or 200,
                                         image=photo, anchor="center")
        except tk.TclError:
            self.img_canvas.delete("all")
            self.img_canvas.create_text(200, 40, anchor="w",
                                        text="预览渲染失败，图片已保存到输出文件夹")
        url = info.get("url") or ""
        self.img_url_label.config(text=f"已保存：{info['path']}")
        self.set_status("图片生成完成：" + info["path"])

    # ---------- 标签页 3：生成视频 ----------
    def _build_video_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  生成视频  ")

        form = ttk.LabelFrame(tab, text="视频设置")
        form.pack(fill="x", padx=6, pady=6)

        ttk.Label(form, text="视频描述：").grid(row=0, column=0, sticky="nw", padx=6, pady=4)
        self.vid_prompt = tk.Text(form, height=3, wrap="word", font=("Microsoft YaHei UI", 10))
        self.vid_prompt.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=4)

        ttk.Label(form, text="时长：").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.vid_duration = ttk.Combobox(form, values=list(VIDEO_DURATIONS), state="readonly", width=10)
        self.vid_duration.set("约 5 秒")
        self.vid_duration.grid(row=1, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(form, text="画幅：").grid(row=1, column=2, sticky="e", padx=6, pady=4)
        self.vid_size = ttk.Combobox(form, values=list(VIDEO_SIZES), state="readonly", width=10)
        self.vid_size.set("横屏 16:9")
        self.vid_size.grid(row=1, column=3, sticky="w", padx=6, pady=4)

        ttk.Label(form, text="首帧：").grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self.vid_frame_mode = tk.StringVar(value="none")
        ttk.Radiobutton(form, text="无（纯文字生成）", value="none",
                        variable=self.vid_frame_mode,
                        command=self.update_frame_ui).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Radiobutton(form, text="本地照片", value="file",
                        variable=self.vid_frame_mode,
                        command=self.update_frame_ui).grid(row=2, column=2, sticky="w", padx=6)
        ttk.Radiobutton(form, text="图片网址", value="url",
                        variable=self.vid_frame_mode,
                        command=self.update_frame_ui).grid(row=2, column=3, sticky="w", padx=6)

        ttk.Button(form, text="选择本地照片", command=self.pick_vid_frame).grid(
            row=3, column=1, sticky="w", padx=6, pady=4)
        self.vid_frame_label = ttk.Label(form, text="未选择", foreground="#777")
        self.vid_frame_label.grid(row=3, column=2, columnspan=2, sticky="w", padx=6)
        self.vid_frame_path = None

        ttk.Label(form, text="图片网址：").grid(row=4, column=0, sticky="e", padx=6, pady=4)
        self.vid_url_entry = ttk.Entry(form, font=("Consolas", 9))
        self.vid_url_entry.grid(row=4, column=1, columnspan=3, sticky="ew", padx=6, pady=4)
        self.vid_url_entry.config(state="disabled")

        hint = ("提示：生视频要求首帧是在线图片。选本地照片时，程序会先免费把照片"
                "转存为在线图片再生成视频；视频接口每分钟限 1 次，请耐心等待。")
        ttk.Label(form, text=hint, foreground="#999", wraplength=760,
                  justify="left").grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=4)
        form.columnconfigure(1, weight=1)

        self.vid_gen_btn = ttk.Button(tab, text="开始生成视频", command=self.generate_video)
        self.vid_gen_btn.pack(pady=4)

        prog = ttk.LabelFrame(tab, text="生成进度")
        prog.pack(fill="x", padx=6, pady=6)
        self.vid_progressbar = ttk.Progressbar(prog, maximum=100, value=0)
        self.vid_progressbar.pack(fill="x", padx=8, pady=(6, 2))
        self.vid_status_label = ttk.Label(prog, text="等待开始", foreground="#555")
        self.vid_status_label.pack(padx=8, pady=(0, 6), anchor="w")

        self.last_video_path = None
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=(0, 8))
        self.vid_open_btn = ttk.Button(btns, text="播放视频", state="disabled",
                                       command=self.play_last_video)
        self.vid_open_btn.pack(side="left")
        ttk.Button(btns, text="打开输出文件夹",
                   command=lambda: os.startfile(OUTPUT_DIR)).pack(side="left", padx=6)

    def update_frame_ui(self):
        mode = self.vid_frame_mode.get()
        self.vid_url_entry.config(state="normal" if mode == "url" else "disabled")

    def pick_vid_frame(self):
        path = filedialog.askopenfilename(title="选择首帧照片", filetypes=IMAGE_FILETYPES)
        if not path:
            return
        self.vid_frame_path = path
        self.vid_frame_label.config(text=os.path.basename(path))
        self.vid_frame_mode.set("file")
        self.update_frame_ui()

    def generate_video(self):
        prompt = self.vid_prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("缺少描述", "请先填写视频描述。")
            return
        mode = self.vid_frame_mode.get()
        first_frame = None
        if mode == "file":
            if not self.vid_frame_path:
                messagebox.showwarning("缺少首帧", "请先选择本地照片。")
                return
            first_frame = ("file", self.vid_frame_path)
        elif mode == "url":
            url = self.vid_url_entry.get().strip()
            if not url:
                messagebox.showwarning("缺少网址", "请填写图片网址。")
                return
            first_frame = ("url", url)

        self.vid_gen_btn.config(state="disabled", text="生成中…")
        self.vid_open_btn.config(state="disabled")
        self.vid_progressbar.config(value=0)
        threading.Thread(
            target=video_worker,
            args=(prompt, self.vid_duration.get(), self.vid_size.get(), first_frame, self.events),
            daemon=True).start()

    def play_last_video(self):
        if self.last_video_path and os.path.exists(self.last_video_path):
            os.startfile(self.last_video_path)

    # ---------- 状态栏 ----------
    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="就绪")
        bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                        anchor="w", padding=(8, 3))
        bar.pack(fill="x", side="bottom")

    def set_status(self, text):
        self.status_var.set(text)

    # ---------- 事件分发 ----------
    def poll_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                self.handle_event(kind, data)
        except queue.Empty:
            pass
        self.root.after(60, self.poll_events)

    def handle_event(self, kind, data):
        if kind == "chat_piece":
            self.chat_append(data, "body")
        elif kind == "chat_done":
            reply = self.get_last_reply()
            if reply.strip():
                self.messages.append({"role": "assistant", "content": reply})
            self.chat_append("\n", "body")
            self.chat_busy = False
            self.chat_send_btn.config(state="normal")
        elif kind == "chat_error":
            self.chat_append(f"\n[{data}]\n", "error")
            self.messages.pop()  # 移除失败的用户消息，便于重试
            self.chat_busy = False
            self.chat_send_btn.config(state="normal")
        elif kind == "img_status":
            self.set_status(data)
        elif kind == "img_done":
            self.show_generated_image(data)
            self.img_gen_btn.config(state="normal", text="开始生成图片")
        elif kind == "img_error":
            self.img_gen_btn.config(state="normal", text="开始生成图片")
            self.set_status("图片生成失败")
            messagebox.showerror("图片生成失败", data)
        elif kind == "vid_status":
            self.vid_status_label.config(text=data)
            self.set_status(data)
        elif kind == "vid_progress":
            self.vid_progressbar.config(value=data)
        elif kind == "vid_done":
            self.last_video_path = data["path"]
            self.vid_progressbar.config(value=100)
            self.vid_status_label.config(text="完成：" + data["path"])
            self.vid_gen_btn.config(state="normal", text="开始生成视频")
            self.vid_open_btn.config(state="normal")
            self.set_status("视频生成完成")
            if messagebox.askyesno("视频已完成", "视频已保存，是否立即播放？"):
                self.play_last_video()
        elif kind == "vid_error":
            self.vid_gen_btn.config(state="normal", text="开始生成视频")
            self.vid_status_label.config(text="失败")
            self.set_status("视频生成失败")
            messagebox.showerror("视频生成失败", data)

    # ---------- 聊天文本工具 ----------
    def chat_append(self, text, tag):
        self.chat_text.config(state="normal")
        self.chat_text.insert("end", text, tag)
        self.chat_text.config(state="disabled")
        self.chat_text.see("end")

    def get_last_reply(self):
        ranges = self.chat_text.tag_ranges("assistant")
        if not ranges:
            return ""
        start = ranges[-2]
        text = self.chat_text.get(start, "end-1c")
        label = "AI："
        return text[len(label):] if text.startswith(label) else text


def main():
    root = tk.Tk()
    root.withdraw()  # 先隐藏主窗口，完成密钥加载/首次设置
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    if not bootstrap_config(root):
        root.destroy()
        return
    root.deiconify()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
