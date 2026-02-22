import os
import traceback
import threading
import queue
import hashlib
import tempfile
import math
import json
import re
import sys
import subprocess
import urllib.request
import urllib.error
from collections import defaultdict
from tkinter import *
from tkinter import filedialog
from tkinter import ttk
from tkinter.messagebox import showerror, showinfo, askyesno

from PIL import Image, ImageOps, ImageTk, ImageSequence
try:
    import rawpy
except Exception:
    rawpy = None

EXTS = (
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".dng", ".raw", ".arw", ".crw", ".cr2", ".nef",
    ".nrw", ".orf", ".raf", ".rw2", ".pef", ".srw", ".3fr", ".erf", ".kdc", ".dcr", ".mos", ".mef", ".mrw",
    ".x3f", ".iiq", ".tiff"
)
RAW_EXTS = (
    ".dng", ".raw", ".arw", ".crw", ".cr2", ".nef", ".nrw", ".orf", ".raf", ".rw2", ".pef", ".srw", ".3fr",
    ".erf", ".kdc", ".dcr", ".mos", ".mef", ".mrw", ".x3f", ".iiq"
)
SAVE_AS_RGB_EXTS = (".jpg", ".jpeg", ".bmp")
SAVE_AS_RGBA_EXTS = (".png", ".tif", ".tiff", ".gif")
PREVIEW_MIN_WIDTH = 180
POSITION_CANVAS_SIZE = 220
BLEND_SIMPLE = "simple"
BLEND_REPEAT_H = "repeat_h"
BLEND_REPEAT_V = "repeat_v"
BLEND_REPEAT_ALL = "repeat_all"
BLEND_OPTIONS = [
    ("Simple", BLEND_SIMPLE),
    ("Repeat horizontally", BLEND_REPEAT_H),
    ("Repeat vertically", BLEND_REPEAT_V),
    ("Repeat everywhere", BLEND_REPEAT_ALL),
]
SIMPLE_MAX_OVERFLOW_RATIO = 0.05
LEFT_PANEL_WIDTH = 320
PREVIEW_WORKERS = 2
PREVIEW_PROXY_CACHE_DIR = os.path.join(tempfile.gettempdir(), "filigrane_preview_cache")
GIF_PREVIEW_MAX_FRAMES = 20


def get_runtime_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_app_version():
    candidates = [
        os.path.join(get_runtime_base_dir(), "VERSION"),
        os.path.join(os.getcwd(), "VERSION"),
    ]
    for path in candidates:
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as version_file:
                    value = version_file.read().strip()
                if value:
                    return value
        except Exception:
            pass
    return "0.0.0"


APP_VERSION = load_app_version()
GITHUB_RELEASES_URL = "https://github.com/Palasthan/Filigrane/releases"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/Palasthan/Filigrane/releases/latest"
UPDATE_CHECK_TIMEOUT_SECONDS = 8
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 30
UPDATE_USER_AGENT = f"Filigrane/{APP_VERSION}"

# Initialize globals early because some Tk callbacks can fire during widget setup.
selected_images = []
preview_groups = []
selected_group = None
busy_processing = False
preview_group_id_seq = 0
preview_tasks = queue.Queue()
preview_results = queue.Queue()
preview_workers = []
preview_worker_stop = threading.Event()
preview_groups_by_id = {}
preview_request_seq = 0
preview_generation = 0
import_popup = None
import_popup_progress = None
import_popup_status = None
import_progress_style_ready = False
preview_backfill_index = 0
EXIF_ORIENTATION_TAG = 274
export_events = queue.Queue()
export_thread = None
update_events = queue.Queue()
update_check_thread = None
update_download_thread = None
update_check_started = False
update_download_popup = None
update_download_progress = None
update_download_status = None
pending_update_release = None


def is_image_file(filename):
    return filename.lower().endswith(EXTS)


def build_output_path(path_out, filename):
    ext = os.path.splitext(filename)[1].lower()
    base = os.path.splitext(filename)[0]
    if ext in SAVE_AS_RGB_EXTS or ext in SAVE_AS_RGBA_EXTS:
        return os.path.join(path_out, "wm_" + filename), ext
    # Unsupported write extension (e.g. RAW): fallback to JPEG.
    return os.path.join(path_out, "wm_" + base + ".jpg"), ".jpg"


def is_animated_gif_image(image, path):
    ext = os.path.splitext(path)[1].lower()
    return ext == ".gif" and bool(getattr(image, "is_animated", False)) and int(getattr(image, "n_frames", 1)) > 1


def build_save_kwargs(source_image, out_ext):
    kwargs = {}
    try:
        icc_profile = source_image.info.get("icc_profile")
        if icc_profile:
            kwargs["icc_profile"] = icc_profile
    except Exception:
        pass

    try:
        dpi = source_image.info.get("dpi")
        if dpi:
            kwargs["dpi"] = dpi
    except Exception:
        pass

    # Keep EXIF when possible. Orientation is normalized by exif_transpose,
    # so force Orientation=1 in output EXIF to avoid double-rotation in viewers.
    try:
        exif = source_image.getexif()
        if exif:
            exif[EXIF_ORIENTATION_TAG] = 1
            exif_bytes = exif.tobytes()
            if exif_bytes:
                kwargs["exif"] = exif_bytes
    except Exception:
        pass

    return kwargs


def save_result_with_metadata(result_image, source_image, out_path, out_ext):
    save_kwargs = build_save_kwargs(source_image, out_ext)
    image_to_save = result_image.convert("RGB") if out_ext in SAVE_AS_RGB_EXTS else result_image

    try:
        image_to_save.save(out_path, **save_kwargs)
        return
    except Exception:
        pass

    # Fallbacks: some format/codec combos reject EXIF or ICC.
    if "exif" in save_kwargs:
        save_kwargs.pop("exif", None)
        try:
            image_to_save.save(out_path, **save_kwargs)
            return
        except Exception:
            pass

    if "icc_profile" in save_kwargs:
        save_kwargs.pop("icc_profile", None)
        try:
            image_to_save.save(out_path, **save_kwargs)
            return
        except Exception:
            pass

    save_kwargs.pop("dpi", None)
    image_to_save.save(out_path)


def save_animated_gif_with_watermark(source_image, transformed_logo, center_x_ratio, center_y_ratio, blend_mode, repeat_spacing_percent, out_path):
    frames = []
    durations = []
    disposals = []
    default_duration = source_image.info.get("duration", 100)
    default_disposal = source_image.info.get("disposal", 2)

    for frame in ImageSequence.Iterator(source_image):
        frame_rgba = frame.convert("RGBA")
        watermarked = paste_watermark_on_image(
            frame_rgba,
            transformed_logo,
            center_x_ratio,
            center_y_ratio,
            blend_mode,
            repeat_spacing_percent,
        )
        alpha = watermarked.getchannel("A")
        rgb = Image.new("RGB", watermarked.size, (0, 0, 0))
        rgb.paste(watermarked, mask=alpha)
        # Keep one palette index reserved for transparent pixels.
        paletted = rgb.convert("P", palette=Image.ADAPTIVE, colors=255)
        transparent_mask = alpha.point(lambda a: 255 if a <= 8 else 0)
        paletted.paste(255, mask=transparent_mask)
        paletted.info["transparency"] = 255
        frames.append(paletted)
        durations.append(frame.info.get("duration", default_duration))
        disposals.append(frame.info.get("disposal", default_disposal))

    if not frames:
        raise RuntimeError("No GIF frames to save")

    save_kwargs = {
        "save_all": True,
        "append_images": frames[1:],
        "loop": source_image.info.get("loop", 0),
        "duration": durations,
        "disposal": disposals,
        "transparency": 255,
        "optimize": False,
    }

    frames[0].save(out_path, format="GIF", **save_kwargs)


def stop_group_preview_animation(group):
    after_id = group.get("photo_anim_after_id")
    if after_id is not None:
        try:
            window.after_cancel(after_id)
        except Exception:
            pass
        group["photo_anim_after_id"] = None


def animate_group_preview(group):
    if group is None or group.get("collapsed", False):
        return
    label = group.get("label")
    frames = group.get("photo_frames")
    durations = group.get("photo_durations")
    if label is None or frames is None or len(frames) == 0:
        return
    if not label.winfo_exists():
        return

    idx = int(group.get("photo_anim_index", 0)) % len(frames)
    label.configure(image=frames[idx], text="")
    label.image = frames[idx]
    delay = 90
    if durations and idx < len(durations):
        delay = max(20, int(durations[idx]))
    group["photo_anim_index"] = (idx + 1) % len(frames)
    group["photo_anim_after_id"] = window.after(delay, lambda g=group: animate_group_preview(g))


def build_gif_preview_frames(sample_path, target_width):
    frames = []
    durations = []
    with Image.open(sample_path) as gif_img:
        frame_count = max(1, int(getattr(gif_img, "n_frames", 1)))
        step = max(1, math.ceil(frame_count / GIF_PREVIEW_MAX_FRAMES))
        default_duration = gif_img.info.get("duration", 100)

        for frame_idx in range(0, frame_count, step):
            gif_img.seek(frame_idx)
            frame = gif_img.convert("RGBA")
            display_width = max(1, min(target_width, frame.width))
            if frame.width != display_width:
                target_height = max(1, int((display_width / frame.width) * frame.height))
                frame = frame.resize((display_width, target_height), Image.Resampling.BILINEAR)
            frames.append(frame)
            durations.append(gif_img.info.get("duration", default_duration))
    return frames, durations


def open_image_compatible(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTS:
        if rawpy is None:
            raise RuntimeError(
                "RAW image support requires the 'rawpy' package. Install it with: pip install rawpy"
            )
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(output_bps=8, use_camera_wb=True)
        return Image.fromarray(rgb)
    return Image.open(path)


def ensure_preview_cache_dir():
    os.makedirs(PREVIEW_PROXY_CACHE_DIR, exist_ok=True)


def build_proxy_cache_path(sample_path, target_width):
    abs_path = os.path.abspath(sample_path)
    try:
        stat = os.stat(abs_path)
        stamp = f"{abs_path}|{stat.st_mtime_ns}|{stat.st_size}|{target_width}"
    except OSError:
        stamp = f"{abs_path}|missing|{target_width}"
    digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()
    return os.path.join(PREVIEW_PROXY_CACHE_DIR, digest + ".jpg")


def build_proxy_image_file(sample_path, target_width):
    ensure_preview_cache_dir()
    cache_path = build_proxy_cache_path(sample_path, target_width)
    if os.path.isfile(cache_path):
        return cache_path

    with open_image_compatible(sample_path) as img:
        sample = ImageOps.exif_transpose(img).convert("RGB")
        display_width = max(1, min(target_width, sample.width))
        if sample.width != display_width:
            target_height = max(1, int((display_width / sample.width) * sample.height))
            sample = sample.resize((display_width, target_height), Image.Resampling.LANCZOS)
        sample.save(cache_path, "JPEG", quality=88, optimize=True)
    return cache_path


def preview_worker_loop():
    while not preview_worker_stop.is_set():
        try:
            task = preview_tasks.get(timeout=0.2)
        except queue.Empty:
            continue
        if task is None:
            break
        try:
            proxy_path = build_proxy_image_file(task["sample"], task["target_width"])
            preview_results.put(
                {
                    "group_id": task["group_id"],
                    "generation": task["generation"],
                    "request_id": task["request_id"],
                    "proxy_path": proxy_path,
                    "target_width": task["target_width"],
                    "error": None,
                }
            )
        except Exception as exc:
            preview_results.put(
                {
                    "group_id": task["group_id"],
                    "generation": task["generation"],
                    "request_id": task["request_id"],
                    "proxy_path": None,
                    "target_width": task["target_width"],
                    "error": str(exc),
                }
            )


def start_preview_workers():
    if preview_workers:
        return
    preview_worker_stop.clear()
    for _ in range(PREVIEW_WORKERS):
        worker = threading.Thread(target=preview_worker_loop, daemon=True)
        worker.start()
        preview_workers.append(worker)


def stop_preview_workers():
    preview_worker_stop.set()
    for _ in preview_workers:
        preview_tasks.put(None)
    preview_workers.clear()


def clear_queue(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break


def hard_refresh_groups_and_previews(reset_scroll=True):
    global preview_generation
    preview_generation += 1
    # Drop stale background preview tasks/results before a full rebuild.
    clear_queue(preview_tasks)
    clear_queue(preview_results)
    build_resolution_groups()
    if reset_scroll:
        preview_canvas.yview_moveto(0.0)
    refresh_previews()
    sync_preview_scrollregion()


def resize_watermark(prev_logo, wanted_width, size_factor):
    wanted_width = max(1, int(wanted_width * size_factor))
    wanted_height = max(1, int((wanted_width / prev_logo.width) * prev_logo.height))
    return prev_logo.resize((wanted_width, wanted_height), Image.Resampling.LANCZOS)


def with_opacity(logo, transparency):
    result = logo.copy()
    alpha = result.getchannel("A")
    alpha = alpha.point(lambda value: int(value * transparency))
    result.putalpha(alpha)
    return result


def build_transformed_watermark(logo, image_width, size_factor, angle):
    resized_logo = resize_watermark(logo, image_width / 2, size_factor)
    if angle != 0:
        resized_logo = resized_logo.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    return resized_logo


def build_transformed_watermark_preview(logo, image_width, size_factor, angle):
    wanted_width = max(1, int((image_width / 2) * size_factor))
    wanted_height = max(1, int((wanted_width / logo.width) * logo.height))
    resized_logo = logo.resize((wanted_width, wanted_height), Image.Resampling.BILINEAR)
    if angle != 0:
        resized_logo = resized_logo.rotate(angle, resample=Image.Resampling.BILINEAR, expand=True)
    return resized_logo


def estimate_transformed_watermark_size(logo_w, logo_h, image_width, size_factor, angle):
    wanted_width = max(1, int((image_width / 2) * size_factor))
    wanted_height = max(1, int((wanted_width / max(1, logo_w)) * logo_h))
    if angle == 0:
        return wanted_width, wanted_height
    rad = math.radians(angle)
    cos_a = abs(math.cos(rad))
    sin_a = abs(math.sin(rad))
    rotated_w = max(1, int((wanted_width * cos_a) + (wanted_height * sin_a)))
    rotated_h = max(1, int((wanted_width * sin_a) + (wanted_height * cos_a)))
    return rotated_w, rotated_h


def paste_watermark_on_image(image, transformed_logo, center_x_ratio, center_y_ratio, blend_mode, repeat_spacing_percent):
    image = image.convert("RGBA")
    center_x_ratio = min(1.0, max(0.0, center_x_ratio))
    center_y_ratio = min(1.0, max(0.0, center_y_ratio))
    if blend_mode == BLEND_SIMPLE:
        center_x_ratio, center_y_ratio = clamp_center_ratios(
            center_x_ratio,
            center_y_ratio,
            image.width,
            image.height,
            transformed_logo.width,
            transformed_logo.height,
            SIMPLE_MAX_OVERFLOW_RATIO,
        )
    watermark_center_x = int(center_x_ratio * image.width)
    watermark_center_y = int(center_y_ratio * image.height)
    anchor_x = watermark_center_x - int(transformed_logo.width / 2)
    anchor_y = watermark_center_y - int(transformed_logo.height / 2)

    if blend_mode == BLEND_SIMPLE:
        image.paste(transformed_logo, (anchor_x, anchor_y), transformed_logo)
        return image

    spacing_px_x = int(transformed_logo.width * (repeat_spacing_percent / 100.0))
    spacing_px_y = int(transformed_logo.height * (repeat_spacing_percent / 100.0))
    step_x = max(1, transformed_logo.width + spacing_px_x)
    step_y = max(1, transformed_logo.height + spacing_px_y)

    def repeated_positions(start, step, max_value):
        positions = [start]
        cur = start - step
        while cur + step > -step:
            positions.append(cur)
            cur -= step
        cur = start + step
        while cur < max_value + step:
            positions.append(cur)
            cur += step
        return positions

    if blend_mode == BLEND_REPEAT_H:
        for x in repeated_positions(anchor_x, step_x, image.width):
            image.paste(transformed_logo, (x, anchor_y), transformed_logo)
        return image

    if blend_mode == BLEND_REPEAT_V:
        for y in repeated_positions(anchor_y, step_y, image.height):
            image.paste(transformed_logo, (anchor_x, y), transformed_logo)
        return image

    if blend_mode == BLEND_REPEAT_ALL:
        xs = repeated_positions(anchor_x, step_x, image.width)
        ys = repeated_positions(anchor_y, step_y, image.height)
        for y in ys:
            for x in xs:
                image.paste(transformed_logo, (x, y), transformed_logo)
        return image

    image.paste(transformed_logo, (anchor_x, anchor_y), transformed_logo)
    return image


def clamp_center_ratios(center_x_ratio, center_y_ratio, image_w, image_h, wm_w, wm_h, overflow_ratio):
    overflow_x = image_w * overflow_ratio
    overflow_y = image_h * overflow_ratio
    min_cx = (wm_w / 2.0) - overflow_x
    max_cx = image_w - (wm_w / 2.0) + overflow_x
    min_cy = (wm_h / 2.0) - overflow_y
    max_cy = image_h - (wm_h / 2.0) + overflow_y

    if min_cx > max_cx:
        min_cx, max_cx = image_w / 2.0, image_w / 2.0
    if min_cy > max_cy:
        min_cy, max_cy = image_h / 2.0, image_h / 2.0

    cx = min(max(center_x_ratio * image_w, min_cx), max_cx)
    cy = min(max(center_y_ratio * image_h, min_cy), max_cy)
    return cx / image_w if image_w else 0.5, cy / image_h if image_h else 0.5


def set_group_selection_buttons_state(state):
    for group in preview_groups:
        if group.get("select_button") is not None:
            group["select_button"]["state"] = state
        if group.get("collapse_button") is not None:
            group["collapse_button"]["state"] = state


def set_settings_controls_state():
    state = "normal" if (selected_group is not None and not busy_processing) else "disabled"
    selectedOpacityScale["state"] = state
    selectedSizeScale["state"] = state
    selectedAngleScale["state"] = state
    blendCombobox["state"] = "readonly" if state == "normal" else "disabled"
    spacingScale["state"] = state if (state == "normal" and selected_group is not None and selected_group.get("blend_mode") != BLEND_SIMPLE) else "disabled"
    positionCanvas["state"] = state


def set_settings_controls_force_state(state):
    selectedOpacityScale["state"] = state
    selectedSizeScale["state"] = state
    selectedAngleScale["state"] = state
    blendCombobox["state"] = "disabled" if state == "disabled" else "readonly"
    spacingScale["state"] = state
    positionCanvas["state"] = state


def compute_position_box(group):
    canvas_size = POSITION_CANVAS_SIZE
    pad = 12
    inner_size = canvas_size - (pad * 2)
    width, height = group["resolution"]
    ratio = width / height if height else 1.0
    if ratio >= 1:
        box_w = inner_size
        box_h = max(1, int(inner_size / ratio))
    else:
        box_h = inner_size
        box_w = max(1, int(inner_size * ratio))
    x0 = int((canvas_size - box_w) / 2)
    y0 = int((canvas_size - box_h) / 2)
    x1 = x0 + box_w
    y1 = y0 + box_h
    return (x0, y0, x1, y1)


def draw_position_selector(group):
    positionCanvas.delete("all")
    if group is None:
        return
    x0, y0, x1, y1 = compute_position_box(group)
    positionCanvas.create_rectangle(x0, y0, x1, y1, outline="#5a5a5a", width=2)
    px = x0 + (group["center_x"] * (x1 - x0))
    py = y0 + (group["center_y"] * (y1 - y0))
    point_radius = 5
    positionCanvas.create_oval(px - point_radius, py - point_radius, px + point_radius, py + point_radius, fill="#d82f2f", outline="")
    positionCanvas.create_line(px - 10, py, px + 10, py, fill="#d82f2f")
    positionCanvas.create_line(px, py - 10, px, py + 10, fill="#d82f2f")
    selectedPositionText.set("x: " + f"{group['center_x']:.2f}" + "  y: " + f"{group['center_y']:.2f}")


def on_position_canvas_drag(event):
    if selected_group is None or busy_processing:
        return
    x0, y0, x1, y1 = compute_position_box(selected_group)
    clamped_x = min(x1, max(x0, event.x))
    clamped_y = min(y1, max(y0, event.y))
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    selected_group["center_x"] = round((clamped_x - x0) / width, 4)
    selected_group["center_y"] = round((clamped_y - y0) / height, 4)
    enforce_group_simple_position_limit(selected_group)
    draw_position_selector(selected_group)
    refresh_preview_group(selected_group)


def reset_watermark_cache():
    global watermark_cache_generation
    watermark_cache["path"] = None
    watermark_cache["base_logo"] = None
    watermark_cache["opacity_logos"] = {}
    watermark_cache_generation += 1


def get_base_watermark():
    path = watermark_path.get()
    if watermark_cache["base_logo"] is None or watermark_cache["path"] != path:
        watermark_cache["path"] = path
        watermark_cache["base_logo"] = Image.open(path).convert("RGBA")
        watermark_cache["opacity_logos"] = {}
    return watermark_cache["base_logo"]


def get_watermark_with_opacity(opacity_value):
    key = round(opacity_value, 3)
    cached = watermark_cache["opacity_logos"].get(key)
    if cached is not None:
        return cached
    logo = with_opacity(get_base_watermark(), opacity_value)
    watermark_cache["opacity_logos"][key] = logo
    return logo


def get_group_transformed_watermark(group):
    cache_key = (
        watermark_cache_generation,
        round(group["opacity"], 3),
        round(group["size"], 3),
        round(group["angle"], 1),
        group["resolution"][0],
    )
    if group.get("wm_cache_key") == cache_key and group.get("wm_cache") is not None:
        return group["wm_cache"]

    logo = get_watermark_with_opacity(group["opacity"])
    transformed = build_transformed_watermark(logo, group["resolution"][0], group["size"], group["angle"])
    group["wm_cache_key"] = cache_key
    group["wm_cache"] = transformed
    return transformed


def get_group_preview_transformed_watermark(group):
    if group.get("sample_preview") is None:
        return None
    preview_width = max(1, group["sample_preview"].width)
    cache_key = (
        watermark_cache_generation,
        round(group["opacity"], 3),
        round(group["size"], 3),
        round(group["angle"], 1),
        preview_width,
    )
    if group.get("preview_wm_cache_key") == cache_key and group.get("preview_wm_cache") is not None:
        return group["preview_wm_cache"]

    logo = get_watermark_with_opacity(group["opacity"])
    transformed = build_transformed_watermark_preview(logo, preview_width, group["size"], group["angle"])
    group["preview_wm_cache_key"] = cache_key
    group["preview_wm_cache"] = transformed
    return transformed


def get_preview_target_width():
    width = preview_canvas.winfo_width() - 26
    return max(PREVIEW_MIN_WIDTH, width)


def get_preview_display_size(group, target_width):
    img_w, img_h = group["resolution"]
    if img_w <= 0 or img_h <= 0:
        return (target_width, 120)
    display_width = max(1, min(target_width, img_w))
    display_height = max(1, int((display_width / img_w) * img_h))
    return (display_width, display_height)


def ensure_group_preview_placeholder(group):
    label = group.get("label")
    if label is None:
        return
    target_width = get_preview_target_width()
    display_width, display_height = get_preview_display_size(group, target_width)
    ph_key = (display_width, display_height)
    if group.get("placeholder_key") != ph_key or group.get("placeholder_photo") is None:
        placeholder = PhotoImage(width=display_width, height=display_height)
        placeholder.put("#ececec", to=(0, 0, display_width, display_height))
        group["placeholder_photo"] = placeholder
        group["placeholder_key"] = ph_key
    if group.get("photo") is None:
        label.configure(image=group["placeholder_photo"], text="Loading preview...", compound="center")
        label.image = group["placeholder_photo"]


def ensure_group_preview_image(group):
    target_width = get_preview_target_width()
    if group.get("sample_is_animated_gif", False):
        if (
            group.get("sample_preview_frames") is not None
            and group.get("sample_preview_request_width") == target_width
        ):
            return True
        try:
            frames, durations = build_gif_preview_frames(group["sample"], target_width)
            if not frames:
                group["proxy_error"] = "No preview frames"
                return False
            group["sample_preview_frames"] = frames
            group["sample_preview_durations"] = durations
            group["sample_preview"] = frames[0]
            group["sample_preview_width"] = frames[0].width
            group["sample_preview_request_width"] = target_width
            group["preview_wm_cache_key"] = None
            group["preview_wm_cache"] = None
            group["preview_render_key"] = None
            group["proxy_error"] = None
            return True
        except Exception as exc:
            group["proxy_error"] = str(exc)
            return False
    if group.get("sample_preview") is not None and group.get("sample_preview_request_width") == target_width:
        return True
    request_group_preview_proxy(group, target_width)
    return False


def request_group_preview_proxy(group, target_width):
    global preview_request_seq
    request_key = (group["sample"], target_width)
    if group.get("proxy_pending") and group.get("proxy_request_key") == request_key:
        return
    preview_request_seq += 1
    group["proxy_pending"] = True
    group["proxy_request_key"] = request_key
    group["proxy_request_id"] = preview_request_seq
    group["proxy_error"] = None
    preview_tasks.put(
        {
            "group_id": group["id"],
            "generation": group["generation"],
            "request_id": preview_request_seq,
            "sample": group["sample"],
            "target_width": target_width,
        }
    )


def process_preview_results():
    updated_groups = []
    while True:
        try:
            result = preview_results.get_nowait()
        except queue.Empty:
            break
        group = preview_groups_by_id.get(result["group_id"])
        if group is None:
            continue
        if result.get("generation") != preview_generation or group.get("generation") != preview_generation:
            continue
        if group.get("proxy_request_id") != result["request_id"]:
            if group.get("sample_preview") is None and result.get("target_width") == get_preview_target_width():
                pass
            else:
                continue
        group["proxy_pending"] = False
        if result["error"] is not None:
            group["proxy_error"] = result["error"]
            continue
        try:
            with Image.open(result["proxy_path"]) as proxy:
                group["sample_preview"] = proxy.convert("RGBA")
                group["sample_preview_width"] = group["sample_preview"].width
                group["sample_preview_request_width"] = result["target_width"]
                group["preview_wm_cache_key"] = None
                group["preview_wm_cache"] = None
                group["preview_render_key"] = None
                group["proxy_error"] = None
                updated_groups.append(group)
        except Exception as exc:
            group["proxy_error"] = str(exc)

    for group in updated_groups:
        refresh_preview_group(group)

    window.after(50, process_preview_results)


def backfill_missing_previews():
    global preview_backfill_index
    if preview_groups:
        batch = 0
        total = len(preview_groups)
        attempts = 0
        while batch < 4 and attempts < total:
            group = preview_groups[preview_backfill_index % total]
            preview_backfill_index += 1
            attempts += 1
            if group.get("label") is None:
                continue
            if group.get("photo") is not None or group.get("proxy_pending"):
                continue
            refresh_preview_group(group)
            batch += 1
    window.after(120, backfill_missing_previews)


def disable_interactions():
    global busy_processing
    busy_processing = True
    button_explore_in["state"] = "disabled"
    button_explore_out["state"] = "disabled"
    button_explore_wm["state"] = "disabled"
    button_start["state"] = "disabled"
    include_subfolders_check["state"] = "disabled"
    set_group_selection_buttons_state("disabled")
    set_settings_controls_force_state("disabled")
    window.update_idletasks()


def enable_interactions():
    global busy_processing
    busy_processing = False
    button_explore_in["state"] = "normal"
    button_explore_out["state"] = "normal"
    button_explore_wm["state"] = "normal"
    include_subfolders_check["state"] = "normal"
    set_group_selection_buttons_state("normal")
    set_settings_controls_state()
    update_start_button_state()
    window.update_idletasks()


def update_progression(filename="", nb=0, started=True):
    if started:
        txtCurrentOperation.set('Watermarking : "' + filename + '"')
    text_counter.set(str(nb) + "/" + str(nbImageToEdit.get()))
    if nbImageToEdit.get() > 0:
        progressbar_operation["value"] = nb * 100 / nbImageToEdit.get()
    window.update_idletasks()


def finish_operations(success=True):
    enable_interactions()
    progressbar_operation["value"] = 0
    txtCurrentOperation.set("")
    if success:
        showinfo("Watermarking completed", "Watermarks added. Results are available in " + folderOutPath.get())


def filigrane_worker(path_out, wm_path, files, group_by_resolution):
    try:
        os.makedirs(path_out, exist_ok=True)
        with Image.open(wm_path).convert("RGBA") as base_logo:
            opacity_cache = {}
            transformed_cache = {}

            def get_logo_with_opacity(opacity_value):
                key = round(opacity_value, 3)
                if key not in opacity_cache:
                    opacity_cache[key] = with_opacity(base_logo, opacity_value)
                return opacity_cache[key]

            def get_transformed(resolution, opacity_value, size_value, angle_value):
                cache_key = (resolution[0], round(opacity_value, 3), round(size_value, 3), round(angle_value, 1))
                if cache_key in transformed_cache:
                    return transformed_cache[cache_key]
                logo = get_logo_with_opacity(opacity_value)
                transformed = build_transformed_watermark(logo, resolution[0], size_value, angle_value)
                transformed_cache[cache_key] = transformed
                return transformed

            total = len(files)
            for idx, filepath in enumerate(files, start=1):
                filename = os.path.basename(filepath)
                export_events.put({"type": "progress", "filename": filename, "index": idx, "total": total})

                with open_image_compatible(filepath) as image:
                    out_path, out_ext = build_output_path(path_out, filename)

                    if is_animated_gif_image(image, filepath) and out_ext == ".gif":
                        resolution = (image.width, image.height)
                        settings = group_by_resolution.get(
                            resolution,
                            {"opacity": 1.0, "size": 1.0, "angle": 0.0, "center_x": 0.5, "center_y": 0.5, "blend_mode": BLEND_SIMPLE, "repeat_spacing": 0},
                        )
                        transformed_logo = get_transformed(
                            resolution,
                            settings["opacity"],
                            settings["size"],
                            settings["angle"],
                        )
                        save_animated_gif_with_watermark(
                            image,
                            transformed_logo,
                            settings["center_x"],
                            settings["center_y"],
                            settings["blend_mode"],
                            settings.get("repeat_spacing", 0),
                            out_path,
                        )
                    else:
                        fixed = ImageOps.exif_transpose(image)
                        resolution = (fixed.width, fixed.height)
                        settings = group_by_resolution.get(
                            resolution,
                            {"opacity": 1.0, "size": 1.0, "angle": 0.0, "center_x": 0.5, "center_y": 0.5, "blend_mode": BLEND_SIMPLE, "repeat_spacing": 0},
                        )
                        transformed_logo = get_transformed(
                            resolution,
                            settings["opacity"],
                            settings["size"],
                            settings["angle"],
                        )
                        result = paste_watermark_on_image(
                            fixed,
                            transformed_logo,
                            settings["center_x"],
                            settings["center_y"],
                            settings["blend_mode"],
                            settings.get("repeat_spacing", 0),
                        )
                        save_result_with_metadata(result, image, out_path, out_ext)
                print("Added watermark to " + filepath + " in " + out_path)

        export_events.put({"type": "done", "success": True})
    except Exception:
        export_events.put({"type": "done", "success": False, "trace": traceback.format_exc()})


def poll_export_events():
    global export_thread
    done_event = None
    while True:
        try:
            event = export_events.get_nowait()
        except queue.Empty:
            break
        etype = event.get("type")
        if etype == "progress":
            update_progression(filename=event.get("filename", ""), nb=event.get("index", 0), started=True)
        elif etype == "done":
            done_event = event

    if done_event is not None:
        export_thread = None
        if done_event.get("success"):
            finish_operations(True)
        else:
            print(done_event.get("trace", ""))
            showerror(title="Error", message="An error occurred. Please check your paths.\n\nTrace:\n" + done_event.get("trace", ""))
            finish_operations(False)
        return

    if export_thread is not None:
        window.after(50, poll_export_events)


def filigrane(path_out_var, lgo_var):
    global export_thread
    try:
        if export_thread is not None:
            return
        disable_interactions()
        path_out = path_out_var.get()
        files = [p for p in selected_images if is_image_file(os.path.basename(p))]
        nbImageToEdit.set(len(files))
        if not files:
            finish_operations(False)
            return
        nb = 0
        update_progression(started=False, nb=nb)

        while True:
            try:
                export_events.get_nowait()
            except queue.Empty:
                break

        group_by_resolution = {
            group["resolution"]: {
                "opacity": group["opacity"],
                "size": group["size"],
                "angle": group["angle"],
                "center_x": group["center_x"],
                "center_y": group["center_y"],
                "blend_mode": group.get("blend_mode", BLEND_SIMPLE),
                "repeat_spacing": group.get("repeat_spacing", 0),
            }
            for group in preview_groups
        }

        export_thread = threading.Thread(
            target=filigrane_worker,
            args=(path_out, lgo_var.get(), files, group_by_resolution),
            daemon=True,
        )
        export_thread.start()
        window.after(50, poll_export_events)
    except Exception:
        print(traceback.format_exc())
        showerror(title="Error", message="An error occurred. Please check your paths.\n\nTrace:\n" + traceback.format_exc())
        finish_operations(False)


def update_start_button_state():
    if os.path.isdir(folderInPath.get()) and os.path.isdir(folderOutPath.get()) and os.path.isfile(watermark_path.get()) and nbImageToEdit.get() > 0:
        button_start["state"] = "normal"
        txtButtonStart.set("Watermark " + str(nbImageToEdit.get()) + " images")
    else:
        button_start["state"] = "disabled"
        txtButtonStart.set("Select In/Out folders and watermark file")


def is_subpath(child_path, parent_path):
    try:
        return os.path.commonpath([os.path.abspath(child_path), os.path.abspath(parent_path)]) == os.path.abspath(parent_path)
    except Exception:
        return False


def iter_source_image_paths(source_folder):
    output_folder = folderOutPath.get()
    ignore_output_tree = bool(output_folder) and is_subpath(output_folder, source_folder)

    if includeSubfolders.get():
        for root, dirs, files in os.walk(source_folder):
            if ignore_output_tree and is_subpath(root, output_folder):
                dirs[:] = []
                continue
            for filename in files:
                if is_image_file(filename):
                    yield os.path.join(root, filename)
    else:
        for filename in os.listdir(source_folder):
            if is_image_file(filename):
                yield os.path.join(source_folder, filename)


def on_include_subfolders_change():
    show_import_popup()
    try:
        hard_refresh_groups_and_previews(reset_scroll=True)
    finally:
        hide_import_popup()
    update_start_button_state()


def should_rebuild_groups_on_output_change(old_output, new_output):
    source = folderInPath.get()
    if not includeSubfolders.get() or not os.path.isdir(source):
        return False
    old_in_source = bool(old_output) and is_subpath(old_output, source)
    new_in_source = bool(new_output) and is_subpath(new_output, source)
    return old_output != new_output and (old_in_source or new_in_source)


def browseFolder(var):
    if var == 1:
        new_path = filedialog.askdirectory()
        if bool(new_path):
            folderInPath.set(new_path)
            show_import_popup()
            try:
                hard_refresh_groups_and_previews(reset_scroll=True)
            finally:
                hide_import_popup()
    elif var == 2:
        new_path = filedialog.askdirectory()
        if bool(new_path):
            old_output = folderOutPath.get()
            folderOutPath.set(new_path)
            if should_rebuild_groups_on_output_change(old_output, new_path):
                show_import_popup()
                try:
                    hard_refresh_groups_and_previews(reset_scroll=False)
                finally:
                    hide_import_popup()
    elif var == 3:
        new_path = filedialog.askopenfilename(
            initialdir="/",
            title="Select a File",
            filetypes=(("Images", "*.png *.jpg *.jpeg"),),
        )
        if bool(new_path):
            watermark_path.set(new_path)
            reset_watermark_cache()
            refresh_previews()

    update_start_button_state()


def build_resolution_groups():
    global selected_group, preview_group_id_seq, preview_backfill_index
    previous_settings = {
        group["resolution"]: (
            group["opacity"],
            group["size"],
            group["angle"],
            group["center_x"],
            group["center_y"],
            group.get("blend_mode", BLEND_SIMPLE),
            group.get("repeat_spacing", 0),
        )
        for group in preview_groups
    }
    previous_collapsed = {
        group["resolution"]: group.get("collapsed", False)
        for group in preview_groups
    }
    previous_selected_resolution = selected_group["resolution"] if selected_group is not None else None

    preview_groups.clear()
    preview_groups_by_id.clear()
    preview_backfill_index = 0
    selected_images.clear()
    nbImageToEdit.set(0)
    text_counter.set("0/0")
    progressbar_operation["value"] = 0

    folder = folderInPath.get()
    if not os.path.isdir(folder):
        render_preview_groups()
        set_selected_group(None)
        return

    watermark_abs = os.path.abspath(watermark_path.get()) if os.path.isfile(watermark_path.get()) else None
    grouped = defaultdict(list)
    animated_gif_by_path = {}
    source_paths = list(iter_source_image_paths(folder))
    total_to_scan = len(source_paths)
    update_import_popup_progress(0, total_to_scan)

    for idx, full_path in enumerate(source_paths, start=1):
        update_import_popup_progress(idx, total_to_scan)
        filename = os.path.basename(full_path)
        if watermark_abs and os.path.abspath(full_path) == watermark_abs:
            continue
        try:
            with open_image_compatible(full_path) as img:
                fixed = ImageOps.exif_transpose(img)
                resolution = (fixed.width, fixed.height)
                animated_gif_by_path[full_path] = is_animated_gif_image(img, full_path)
            grouped[resolution].append(full_path)
            selected_images.append(full_path)
        except Exception:
            continue

    nbImageToEdit.set(len(selected_images))
    update_progression(started=False)
    update_import_popup_progress(total_to_scan, total_to_scan)

    for resolution in sorted(
        grouped.keys(),
        key=lambda item: (-len(grouped[item]), -(item[0] * item[1]), -item[0], -item[1]),
    ):
        files = sorted(grouped[resolution])
        opacity_value, size_value, angle_value, center_x, center_y, blend_mode, repeat_spacing = previous_settings.get(
            resolution, (1.0, 1.0, 0.0, 0.5, 0.5, BLEND_SIMPLE, 0)
        )
        preview_group_id_seq += 1
        group_data = {
            "id": preview_group_id_seq,
            "generation": preview_generation,
            "resolution": resolution,
            "count": len(files),
            "sample": files[0],
            "sample_is_animated_gif": animated_gif_by_path.get(files[0], False),
            "label": None,
            "photo": None,
            "photo_frames": None,
            "photo_durations": None,
            "photo_anim_index": 0,
            "photo_anim_after_id": None,
            "select_button": None,
            "collapse_button": None,
            "title_var": None,
            "opacity": opacity_value,
            "size": size_value,
            "angle": angle_value,
            "center_x": center_x,
            "center_y": center_y,
            "blend_mode": blend_mode,
            "repeat_spacing": repeat_spacing,
            "sample_preview": None,
            "sample_preview_frames": None,
            "sample_preview_durations": None,
            "sample_preview_width": None,
            "sample_preview_request_width": None,
            "placeholder_photo": None,
            "placeholder_key": None,
            "wm_cache_key": None,
            "wm_cache": None,
            "preview_wm_cache_key": None,
            "preview_wm_cache": None,
            "preview_render_key": None,
            "proxy_pending": False,
            "proxy_request_key": None,
            "proxy_request_id": None,
            "proxy_error": None,
            "collapsed": previous_collapsed.get(resolution, False),
        }
        preview_groups.append(group_data)
        preview_groups_by_id[group_data["id"]] = group_data

    render_preview_groups()

    if preview_groups:
        target = None
        if previous_selected_resolution is not None:
            for group in preview_groups:
                if group["resolution"] == previous_selected_resolution:
                    target = group
                    break
        if target is None:
            target = preview_groups[0]
        set_selected_group(target)
    else:
        set_selected_group(None)

    refresh_previews()


def clear_preview_groups_ui():
    for group in preview_groups:
        stop_group_preview_animation(group)
    for child in preview_inner.winfo_children():
        child.destroy()


def group_title(group):
    width, height = group["resolution"]
    marker = "> " if group is selected_group else ""
    label = "image" if group["count"] == 1 else "images"
    return marker + str(width) + "x" + str(height) + " (" + str(group["count"]) + " " + label + ")"


def refresh_group_title(group):
    if group.get("title_var") is not None:
        group["title_var"].set(group_title(group))


def refresh_group_collapse_button(group):
    btn = group.get("collapse_button")
    if btn is None:
        return
    btn.configure(text="Expand" if group.get("collapsed", False) else "Collapse")


def update_group_collapsed_ui(group):
    label = group.get("label")
    if label is None:
        return
    if group.get("collapsed", False):
        if label.winfo_manager() == "grid":
            label.grid_remove()
    else:
        if label.winfo_manager() != "grid":
            label.grid(column=0, row=1, columnspan=3, sticky=(W, E), pady=(4, 0))
    refresh_group_collapse_button(group)
    sync_preview_scrollregion()


def toggle_group_collapsed(group):
    group["collapsed"] = not group.get("collapsed", False)
    update_group_collapsed_ui(group)
    if not group["collapsed"]:
        refresh_preview_group(group)


def refresh_group_style(group):
    card = group.get("card")
    title_label = group.get("title_label")
    img_label = group.get("label")
    if card is None:
        return

    bg = "#e0e0e0" if group is selected_group else "#f7f7f7"
    card.configure(bg=bg, highlightthickness=1, highlightbackground="#c4c4c4")
    if title_label is not None:
        title_label.configure(bg=bg)
    if img_label is not None:
        img_label.configure(bg=bg)


def set_selected_group(group):
    global selected_group
    selected_group = group

    if selected_group is None:
        selectedGroupText.set("No group selected")
        selectedOpacityValue.set(1.0)
        selectedSizeValue.set(1.0)
        selectedAngleValue.set(0.0)
        selectedPositionText.set("x: 0.50  y: 0.50")
        blendDisplayValue.set(BLEND_OPTIONS[0][0])
        spacingValue.set(0.0)
        spacingScale.set(0)
        update_spacing_visibility()
        draw_position_selector(None)
        set_settings_controls_state()
        for g in preview_groups:
            refresh_group_title(g)
            refresh_group_style(g)
        return

    width, height = selected_group["resolution"]
    selectedGroupText.set("Group: " + str(width) + "x" + str(height))
    selectedOpacityValue.set(selected_group["opacity"])
    selectedSizeValue.set(selected_group["size"])
    selectedAngleValue.set(selected_group["angle"])
    selectedOpacityScale.set(selected_group["opacity"] * 100)
    selectedSizeScale.set(selected_group["size"] * 100)
    selectedAngleScale.set(selected_group["angle"])
    blendDisplayValue.set(blend_mode_label_from_value(selected_group.get("blend_mode", BLEND_SIMPLE)))
    spacingValue.set(selected_group.get("repeat_spacing", 0))
    spacingScale.set(selected_group.get("repeat_spacing", 0))
    enforce_group_simple_position_limit(selected_group)
    update_spacing_visibility()
    draw_position_selector(selected_group)
    set_settings_controls_state()

    for g in preview_groups:
        refresh_group_title(g)
        refresh_group_style(g)


def on_selected_opacity_change(value):
    if selected_group is None:
        return
    selected_group["opacity"] = round(float(value) / 100.0, 2)
    selectedOpacityValue.set(selected_group["opacity"])
    refresh_preview_group(selected_group)


def on_selected_size_change(value):
    if selected_group is None:
        return
    selected_group["size"] = round(float(value) / 100.0, 2)
    selectedSizeValue.set(selected_group["size"])
    enforce_group_simple_position_limit(selected_group)
    refresh_preview_group(selected_group)


def on_selected_angle_change(value):
    if selected_group is None:
        return
    selected_group["angle"] = round(float(value), 1)
    selectedAngleValue.set(selected_group["angle"])
    enforce_group_simple_position_limit(selected_group)
    refresh_preview_group(selected_group)


def blend_mode_label_from_value(value):
    for label, mode in BLEND_OPTIONS:
        if mode == value:
            return label
    return BLEND_OPTIONS[0][0]


def blend_mode_value_from_label(label):
    for display, mode in BLEND_OPTIONS:
        if display == label:
            return mode
    return BLEND_SIMPLE


def on_blend_mode_change(event=None):
    if selected_group is None:
        return
    selected_group["blend_mode"] = blend_mode_value_from_label(blendDisplayValue.get())
    enforce_group_simple_position_limit(selected_group)
    update_spacing_visibility()
    refresh_preview_group(selected_group)


def on_spacing_change(value):
    if selected_group is None:
        return
    selected_group["repeat_spacing"] = round(float(value), 1)
    spacingValue.set(selected_group["repeat_spacing"])
    refresh_preview_group(selected_group)


def update_spacing_visibility():
    visible = selected_group is not None and selected_group.get("blend_mode", BLEND_SIMPLE) != BLEND_SIMPLE
    if visible:
        spacingTitleLabel.grid(column=1, row=10, sticky=W, pady=(10, 0))
        spacingScale.grid(column=1, row=11, sticky=(W, E))
        spacingValueLabel.grid(column=2, row=11, sticky=E, padx=(8, 0))
    else:
        spacingTitleLabel.grid_remove()
        spacingScale.grid_remove()
        spacingValueLabel.grid_remove()
    set_settings_controls_state()


def enforce_group_simple_position_limit(group):
    if group is None or group.get("blend_mode", BLEND_SIMPLE) != BLEND_SIMPLE:
        return
    try:
        if watermark_cache["path"] != watermark_path.get():
            reset_watermark_cache()
        logo = get_base_watermark()
        wm_w, wm_h = estimate_transformed_watermark_size(
            logo.width,
            logo.height,
            group["resolution"][0],
            group["size"],
            group["angle"],
        )
        cx, cy = clamp_center_ratios(
            group["center_x"],
            group["center_y"],
            group["resolution"][0],
            group["resolution"][1],
            wm_w,
            wm_h,
            SIMPLE_MAX_OVERFLOW_RATIO,
        )
        group["center_x"] = round(cx, 4)
        group["center_y"] = round(cy, 4)
    except Exception:
        pass


def render_preview_groups():
    clear_preview_groups_ui()

    if not preview_groups:
        ttk.Label(preview_inner, text="No image groups to display.").grid(column=1, row=1, sticky=(W, E), padx=6, pady=6)
        sync_preview_scrollregion()
        return

    for idx, group in enumerate(preview_groups, start=1):
        card = Frame(preview_inner, bg="#f7f7f7", padx=4, pady=4)
        card.grid(column=1, row=idx, sticky=(W, E), padx=6, pady=4)
        card.columnconfigure(0, weight=1)
        group["card"] = card

        title_var = StringVar(value=group_title(group))
        title_label = Label(card, textvariable=title_var, font=("Segoe UI", 9, "bold"), bg="#f7f7f7")
        title_label.grid(column=0, row=0, sticky=W, padx=(2, 6))
        group["title_var"] = title_var
        group["title_label"] = title_label

        select_button = ttk.Button(card, text="Edit", command=lambda g=group: set_selected_group(g))
        select_button.grid(column=1, row=0, sticky=E)
        group["select_button"] = select_button

        collapse_button = ttk.Button(card, text="Collapse", command=lambda g=group: toggle_group_collapsed(g))
        collapse_button.grid(column=2, row=0, sticky=E, padx=(6, 0))
        group["collapse_button"] = collapse_button

        img_label = Label(card, bg="#f7f7f7")
        img_label.grid(column=0, row=1, columnspan=3, sticky=(W, E), pady=(4, 0))
        group["label"] = img_label
        ensure_group_preview_placeholder(group)
        update_group_collapsed_ui(group)

        for widget in (card, title_label, img_label):
            widget.bind("<Button-1>", lambda event, g=group: set_selected_group(g))
            bind_preview_wheel(widget)
        bind_preview_wheel(select_button)
        bind_preview_wheel(collapse_button)

        refresh_group_style(group)

    sync_preview_scrollregion()
    set_group_selection_buttons_state("disabled" if busy_processing else "normal")


def refresh_preview_group(group):
    if group.get("collapsed", False):
        stop_group_preview_animation(group)
        return
    label = group.get("label")
    if label is None:
        return
    ensure_group_preview_placeholder(group)

    if not os.path.isfile(watermark_path.get()):
        label.configure(text="Select a valid watermark file", image=group.get("placeholder_photo", ""))
        refresh_group_title(group)
        return

    try:
        if watermark_cache["path"] != watermark_path.get():
            reset_watermark_cache()
        if not ensure_group_preview_image(group):
            if group.get("proxy_error"):
                label.configure(text="Preview unavailable", image=group.get("placeholder_photo", ""))
            return

        if group.get("sample_is_animated_gif", False):
            render_key = (
                watermark_cache_generation,
                group.get("sample_preview_width"),
                round(group["opacity"], 3),
                round(group["size"], 3),
                round(group["angle"], 1),
                round(group["center_x"], 4),
                round(group["center_y"], 4),
                group.get("blend_mode", BLEND_SIMPLE),
                round(group.get("repeat_spacing", 0), 2),
                len(group.get("sample_preview_frames") or []),
            )
            if group.get("photo_frames") is not None and group.get("preview_render_key") == render_key:
                if group.get("photo_anim_after_id") is None:
                    animate_group_preview(group)
                return

            transformed_logo = get_group_preview_transformed_watermark(group)
            if transformed_logo is None:
                return

            stop_group_preview_animation(group)
            photo_frames = []
            source_frames = group.get("sample_preview_frames") or []
            for frame in source_frames:
                watermarked = paste_watermark_on_image(
                    frame.copy(),
                    transformed_logo,
                    group["center_x"],
                    group["center_y"],
                    group.get("blend_mode", BLEND_SIMPLE),
                    group.get("repeat_spacing", 0),
                )
                photo_frames.append(ImageTk.PhotoImage(watermarked))

            if not photo_frames:
                label.configure(text="Preview unavailable", image=group.get("placeholder_photo", ""))
                return
            group["photo_frames"] = photo_frames
            group["photo_durations"] = group.get("sample_preview_durations") or []
            group["photo_anim_index"] = 0
            group["photo"] = photo_frames[0]
            group["preview_render_key"] = render_key
            animate_group_preview(group)
            refresh_group_title(group)
            return

        render_key = (
            watermark_cache_generation,
            group.get("sample_preview_width"),
            round(group["opacity"], 3),
            round(group["size"], 3),
            round(group["angle"], 1),
            round(group["center_x"], 4),
            round(group["center_y"], 4),
            group.get("blend_mode", BLEND_SIMPLE),
            round(group.get("repeat_spacing", 0), 2),
        )
        if group.get("photo") is not None and group.get("preview_render_key") == render_key:
            return
        stop_group_preview_animation(group)
        group["photo_frames"] = None
        group["photo_durations"] = None
        group["photo_anim_index"] = 0
        transformed_logo = get_group_preview_transformed_watermark(group)
        if transformed_logo is None:
            return
        watermarked = paste_watermark_on_image(
            group["sample_preview"].copy(),
            transformed_logo,
            group["center_x"],
            group["center_y"],
            group.get("blend_mode", BLEND_SIMPLE),
            group.get("repeat_spacing", 0),
        )
        photo = ImageTk.PhotoImage(watermarked)
        label.configure(image=photo, text="")
        label.image = photo
        group["photo"] = photo
        group["preview_render_key"] = render_key
    except Exception:
        label.configure(text="Preview unavailable", image=group.get("placeholder_photo", ""))

    refresh_group_title(group)


def refresh_visible_previews():
    if not preview_groups:
        return
    preview_canvas.update_idletasks()
    top = preview_canvas.canvasy(0)
    bottom = top + preview_canvas.winfo_height()

    for group in preview_groups:
        card = group.get("card")
        if card is None:
            continue
        y = card.winfo_y()
        h = card.winfo_height()
        margin = 120
        is_visible = (y + h) >= (top - margin) and y <= (bottom + margin)
        if is_visible:
            refresh_preview_group(group)
        else:
            stop_group_preview_animation(group)
    # Some cards change height after image render; refresh scroll bounds right after.
    preview_canvas.update_idletasks()
    sync_preview_scrollregion()


def refresh_previews():
    sync_preview_scrollregion()
    refresh_visible_previews()


def sync_preview_scrollregion(event=None):
    preview_canvas.configure(scrollregion=preview_canvas.bbox("all"))


def on_preview_canvas_resize(event):
    global preview_canvas_width
    preview_canvas.itemconfigure(preview_canvas_window, width=event.width)
    if preview_canvas_width != event.width:
        preview_canvas_width = event.width
        for group in preview_groups:
            group["sample_preview"] = None
            group["sample_preview_frames"] = None
            group["sample_preview_durations"] = None
            group["sample_preview_width"] = None
            group["sample_preview_request_width"] = None
            group["preview_wm_cache_key"] = None
            group["preview_wm_cache"] = None
            stop_group_preview_animation(group)
            group["photo_frames"] = None
            group["photo_durations"] = None
            group["photo_anim_index"] = 0
            group["photo"] = None
            group["preview_render_key"] = None
            group["proxy_pending"] = False
            group["proxy_request_key"] = None
            group["proxy_request_id"] = None
            ensure_group_preview_placeholder(group)
        refresh_previews()
    else:
        refresh_visible_previews()


def on_preview_scroll(*args):
    preview_canvas.yview(*args)
    refresh_visible_previews()


def on_preview_mousewheel(event):
    if event.delta != 0:
        preview_canvas.yview_scroll(int(-event.delta / 120), "units")
    elif getattr(event, "num", None) == 4:
        preview_canvas.yview_scroll(-1, "units")
    elif getattr(event, "num", None) == 5:
        preview_canvas.yview_scroll(1, "units")
    refresh_visible_previews()


def bind_preview_wheel(widget):
    widget.bind("<MouseWheel>", on_preview_mousewheel)
    widget.bind("<Button-4>", on_preview_mousewheel)
    widget.bind("<Button-5>", on_preview_mousewheel)


def show_import_popup():
    global import_popup, import_popup_progress, import_popup_status, import_progress_style_ready
    if import_popup is not None and import_popup.winfo_exists():
        return
    import_popup = Toplevel(window)
    import_popup.title("Importing")
    import_popup.transient(window)
    import_popup.grab_set()
    import_popup.resizable(False, False)
    ttk.Label(import_popup, text="Importing photos...\nPlease wait.").grid(column=1, row=1, padx=18, pady=(14, 8))
    if not import_progress_style_ready:
        style = ttk.Style(import_popup)
        style.configure("Import.Horizontal.TProgressbar", thickness=16, troughcolor="#d8d8d8", background="#2f7dd1")
        import_progress_style_ready = True
    import_popup_progress = ttk.Progressbar(
        import_popup,
        orient="horizontal",
        mode="indeterminate",
        length=280,
        style="Import.Horizontal.TProgressbar",
    )
    import_popup_progress.grid(column=1, row=2, padx=18, pady=(0, 6), sticky=(W, E))
    import_popup_progress.start(20)
    import_popup_status = StringVar(value="0/0")
    ttk.Label(import_popup, textvariable=import_popup_status).grid(column=1, row=3, pady=(0, 12))
    import_popup.update_idletasks()
    win_x = window.winfo_rootx()
    win_y = window.winfo_rooty()
    win_w = window.winfo_width()
    win_h = window.winfo_height()
    pop_w = import_popup.winfo_width()
    pop_h = import_popup.winfo_height()
    x = win_x + max(0, (win_w - pop_w) // 2)
    y = win_y + max(0, (win_h - pop_h) // 2)
    import_popup.geometry(f"+{x}+{y}")
    import_popup.update_idletasks()
    # Force full paint before heavy import work starts.
    import_popup.update()


def hide_import_popup():
    global import_popup, import_popup_progress, import_popup_status
    if import_popup is not None and import_popup.winfo_exists():
        if import_popup_progress is not None:
            import_popup_progress.stop()
        import_popup.grab_release()
        import_popup.destroy()
    import_popup = None
    import_popup_progress = None
    import_popup_status = None


def update_import_popup_progress(current, total):
    if import_popup is None or not import_popup.winfo_exists():
        return
    if import_popup_progress is not None:
        if total > 0:
            import_popup_progress.stop()
            clamped = max(0, min(current, total))
            import_popup_progress.configure(mode="determinate", maximum=total, value=clamped)
        else:
            import_popup_progress.configure(mode="indeterminate")
            import_popup_progress.start(20)
    if import_popup_status is not None:
        if total > 0:
            import_popup_status.set(f"{current}/{total}")
        else:
            import_popup_status.set("Scanning...")
    # Need full event processing here, not only geometry tasks,
    # otherwise popup content may stay blank during long scans.
    import_popup.update()


def parse_version_key(version_text):
    if not version_text:
        return ()
    cleaned = str(version_text).strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:]
    parts = re.findall(r"\d+|[A-Za-z]+", cleaned)
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return tuple(key)


def is_version_newer(candidate_version, current_version):
    return parse_version_key(candidate_version) > parse_version_key(current_version)


def github_get_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": UPDATE_USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=UPDATE_CHECK_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def pick_release_installer_asset(release_data):
    assets = release_data.get("assets") or []
    if not assets:
        return None

    exe_assets = [a for a in assets if str(a.get("name", "")).lower().endswith(".exe")]
    if not exe_assets:
        return None

    preferred = [a for a in exe_assets if "setup" in str(a.get("name", "")).lower()]
    if preferred:
        return preferred[0]
    return exe_assets[0]


def check_updates_worker():
    try:
        release = github_get_json(GITHUB_LATEST_RELEASE_API)
        latest_version = (release.get("tag_name") or release.get("name") or "").strip()
        if not latest_version:
            update_events.put({"type": "check_error", "error": "Missing release version"})
            return

        if not is_version_newer(latest_version, APP_VERSION):
            update_events.put({"type": "check_done", "available": False, "latest_version": latest_version})
            return

        asset = pick_release_installer_asset(release)
        update_events.put(
            {
                "type": "check_done",
                "available": True,
                "current_version": APP_VERSION,
                "latest_version": latest_version,
                "release_name": release.get("name") or latest_version,
                "release_url": release.get("html_url") or GITHUB_RELEASES_URL,
                "release_body": release.get("body") or "",
                "asset_name": asset.get("name") if asset else None,
                "asset_url": asset.get("browser_download_url") if asset else None,
            }
        )
    except Exception as exc:
        update_events.put({"type": "check_error", "error": str(exc)})


def format_bytes_count(byte_count):
    value = float(max(0, int(byte_count)))
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(byte_count)} B"


def ensure_update_download_popup():
    global update_download_popup, update_download_progress, update_download_status
    if update_download_popup is not None and update_download_popup.winfo_exists():
        return

    update_download_popup = Toplevel(window)
    update_download_popup.title("Update")
    update_download_popup.transient(window)
    update_download_popup.resizable(False, False)
    update_download_popup.protocol("WM_DELETE_WINDOW", lambda: None)

    ttk.Label(update_download_popup, text="Downloading update...\nPlease wait.").grid(column=1, row=1, padx=18, pady=(14, 8))
    update_download_progress = ttk.Progressbar(update_download_popup, orient="horizontal", mode="indeterminate", length=320)
    update_download_progress.grid(column=1, row=2, padx=18, pady=(0, 6), sticky=(W, E))
    update_download_progress.start(25)
    update_download_status = StringVar(value="")
    ttk.Label(update_download_popup, textvariable=update_download_status).grid(column=1, row=3, padx=18, pady=(0, 12), sticky=(W, E))

    update_download_popup.update_idletasks()
    x = window.winfo_rootx() + max(0, (window.winfo_width() - update_download_popup.winfo_width()) // 2)
    y = window.winfo_rooty() + max(0, (window.winfo_height() - update_download_popup.winfo_height()) // 2)
    update_download_popup.geometry(f"+{x}+{y}")


def hide_update_download_popup():
    global update_download_popup, update_download_progress, update_download_status
    if update_download_popup is not None and update_download_popup.winfo_exists():
        try:
            if update_download_progress is not None:
                update_download_progress.stop()
        except Exception:
            pass
        update_download_popup.destroy()
    update_download_popup = None
    update_download_progress = None
    update_download_status = None


def update_download_popup_progress(downloaded_bytes, total_bytes):
    if update_download_popup is None or not update_download_popup.winfo_exists():
        return
    if update_download_progress is None:
        return

    if total_bytes and total_bytes > 0:
        try:
            update_download_progress.stop()
        except Exception:
            pass
        update_download_progress.configure(mode="determinate", maximum=total_bytes, value=min(downloaded_bytes, total_bytes))
        if update_download_status is not None:
            update_download_status.set(f"{format_bytes_count(downloaded_bytes)} / {format_bytes_count(total_bytes)}")
    else:
        update_download_progress.configure(mode="indeterminate")
        update_download_progress.start(25)
        if update_download_status is not None:
            update_download_status.set(format_bytes_count(downloaded_bytes))
    update_download_popup.update_idletasks()


def download_update_worker(asset_url, asset_name):
    try:
        os.makedirs(os.path.join(tempfile.gettempdir(), "filigrane_updates"), exist_ok=True)
        safe_name = os.path.basename(asset_name or "Filigrane-Setup.exe")
        dest_path = os.path.join(tempfile.gettempdir(), "filigrane_updates", safe_name)

        request = urllib.request.Request(asset_url, headers={"User-Agent": UPDATE_USER_AGENT})
        with urllib.request.urlopen(request, timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS) as response, open(dest_path, "wb") as out_file:
            total_bytes = 0
            try:
                total_bytes = int(response.headers.get("Content-Length") or 0)
            except Exception:
                total_bytes = 0

            downloaded = 0
            while True:
                chunk = response.read(1024 * 128)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                update_events.put(
                    {
                        "type": "download_progress",
                        "downloaded_bytes": downloaded,
                        "total_bytes": total_bytes,
                    }
                )

        update_events.put({"type": "download_done", "path": dest_path})
    except Exception:
        update_events.put({"type": "download_error", "trace": traceback.format_exc()})


def launch_installer_and_close(installer_path):
    try:
        try:
            subprocess.Popen([installer_path], cwd=os.path.dirname(installer_path) or None)
        except Exception:
            if hasattr(os, "startfile"):
                os.startfile(installer_path)
            else:
                raise
        showinfo(
            "Update",
            "The installer has been launched.\nFiligrane will now close so the update can proceed.",
        )
        window.after(200, on_app_close)
    except Exception:
        showerror(title="Update", message="Unable to launch the installer.\n\nTrace:\n" + traceback.format_exc())


def prompt_update_available(release_info):
    global pending_update_release
    pending_update_release = release_info

    latest_version = release_info.get("latest_version", "?")
    current_version = release_info.get("current_version", APP_VERSION)
    asset_name = release_info.get("asset_name")
    release_url = release_info.get("release_url") or GITHUB_RELEASES_URL

    if asset_name:
        message = (
            "A new version of Filigrane is available.\n\n"
            f"Current version: {current_version}\n"
            f"Available version: {latest_version}\n\n"
            "Do you want to download and install it now?"
        )
    else:
        message = (
            "A new version of Filigrane is available, but no installer asset (.exe) was found on the release.\n\n"
            f"Current version: {current_version}\n"
            f"Available version: {latest_version}\n\n"
            "Open the releases page now?"
        )

    if not askyesno("Update available", message):
        pending_update_release = None
        return

    if not asset_name or not release_info.get("asset_url"):
        try:
            import webbrowser
            webbrowser.open(release_url)
        except Exception:
            showerror(title="Update", message="Unable to open the releases page.\n" + release_url)
        pending_update_release = None
        return

    ensure_update_download_popup()
    if update_download_status is not None:
        update_download_status.set("Starting download...")

    global update_download_thread
    update_download_thread = threading.Thread(
        target=download_update_worker,
        args=(release_info["asset_url"], asset_name),
        daemon=True,
    )
    update_download_thread.start()


def poll_update_events():
    global update_check_thread, update_download_thread, pending_update_release

    while True:
        try:
            event = update_events.get_nowait()
        except queue.Empty:
            break

        etype = event.get("type")
        if etype == "check_done":
            update_check_thread = None
            if event.get("available"):
                prompt_update_available(event)
        elif etype == "check_error":
            update_check_thread = None
            print("Update check failed:", event.get("error"))
        elif etype == "download_progress":
            update_download_popup_progress(event.get("downloaded_bytes", 0), event.get("total_bytes", 0))
        elif etype == "download_done":
            update_download_thread = None
            hide_update_download_popup()
            launch_installer_and_close(event.get("path"))
            pending_update_release = None
        elif etype == "download_error":
            update_download_thread = None
            hide_update_download_popup()
            showerror(title="Update", message="Update download failed.\n\nTrace:\n" + event.get("trace", ""))
            pending_update_release = None

    if update_check_started:
        window.after(250, poll_update_events)


def start_background_update_check():
    global update_check_started, update_check_thread
    if update_check_started:
        return
    update_check_started = True
    update_check_thread = threading.Thread(target=check_updates_worker, daemon=True)
    update_check_thread.start()
    window.after(250, poll_update_events)


window = Tk()
window.title("Watermark")
window.config(background="white")
window.columnconfigure(0, weight=0)
window.columnconfigure(0, minsize=LEFT_PANEL_WIDTH)
window.columnconfigure(1, weight=6)
window.columnconfigure(2, weight=2)
window.rowconfigure(0, weight=1)

mainframe = ttk.Frame(window, padding="3 3 12 12", width=LEFT_PANEL_WIDTH)
mainframe.grid(column=0, row=0, sticky=(N, W, E, S))
mainframe.grid_propagate(False)
mainframe.columnconfigure(1, weight=1)
mainframe.rowconfigure(19, weight=1)

preview_frame = ttk.LabelFrame(window, text="Previews by resolution", padding="6 6 6 6")
preview_frame.grid(column=1, row=0, sticky=(N, W, E, S))
preview_frame.columnconfigure(0, weight=1)
preview_frame.rowconfigure(0, weight=1)

preview_canvas = Canvas(preview_frame, highlightthickness=0)
preview_canvas.grid(column=0, row=0, sticky=(N, W, E, S))
preview_scrollbar = ttk.Scrollbar(preview_frame, orient="vertical", command=on_preview_scroll)
preview_scrollbar.grid(column=1, row=0, sticky=(N, S))
preview_canvas.configure(yscrollcommand=preview_scrollbar.set)

preview_inner = ttk.Frame(preview_canvas)
preview_canvas_window = preview_canvas.create_window((0, 0), window=preview_inner, anchor="nw")
preview_inner.bind("<Configure>", sync_preview_scrollregion)
preview_canvas.bind("<Configure>", on_preview_canvas_resize)
bind_preview_wheel(preview_frame)
bind_preview_wheel(preview_canvas)
bind_preview_wheel(preview_inner)

settings_frame = ttk.LabelFrame(window, text="Selected group settings", padding="8 8 8 8")
settings_frame.grid(column=2, row=0, sticky=(N, W, E, S))
settings_frame.columnconfigure(1, weight=1)

selectedGroupText = StringVar(value="No group selected")
selectedOpacityValue = DoubleVar(value=1.0)
selectedSizeValue = DoubleVar(value=1.0)
selectedAngleValue = DoubleVar(value=0.0)
selectedPositionText = StringVar(value="x: 0.50  y: 0.50")
blendDisplayValue = StringVar(value=BLEND_OPTIONS[0][0])
spacingValue = DoubleVar(value=0.0)

ttk.Label(settings_frame, textvariable=selectedGroupText, font=("Segoe UI", 9, "bold")).grid(column=1, row=1, columnspan=2, sticky=(W, E), pady=(0, 8))

ttk.Label(settings_frame, text="Opacity").grid(column=1, row=2, sticky=W)
selectedOpacityScale = ttk.Scale(settings_frame, from_=1, to=100, command=on_selected_opacity_change)
selectedOpacityScale.set(100)
selectedOpacityScale.grid(column=1, row=3, sticky=(W, E))
ttk.Label(settings_frame, textvariable=selectedOpacityValue).grid(column=2, row=3, sticky=E, padx=(8, 0))

ttk.Label(settings_frame, text="Watermark size").grid(column=1, row=4, sticky=W, pady=(10, 0))
selectedSizeScale = ttk.Scale(settings_frame, from_=1, to=200, command=on_selected_size_change)
selectedSizeScale.set(100)
selectedSizeScale.grid(column=1, row=5, sticky=(W, E))
ttk.Label(settings_frame, textvariable=selectedSizeValue).grid(column=2, row=5, sticky=E, padx=(8, 0))

ttk.Label(settings_frame, text="Rotation (deg)").grid(column=1, row=6, sticky=W, pady=(10, 0))
selectedAngleScale = ttk.Scale(settings_frame, from_=-180, to=180, command=on_selected_angle_change)
selectedAngleScale.set(0)
selectedAngleScale.grid(column=1, row=7, sticky=(W, E))
ttk.Label(settings_frame, textvariable=selectedAngleValue).grid(column=2, row=7, sticky=E, padx=(8, 0))

ttk.Label(settings_frame, text="Blend type").grid(column=1, row=8, sticky=W, pady=(10, 0))
blendCombobox = ttk.Combobox(
    settings_frame,
    textvariable=blendDisplayValue,
    values=[label for label, _ in BLEND_OPTIONS],
    state="readonly",
)
blendCombobox.grid(column=1, row=9, columnspan=2, sticky=(W, E))
blendCombobox.bind("<<ComboboxSelected>>", on_blend_mode_change)

spacingTitleLabel = ttk.Label(settings_frame, text="Repeat spacing (%)")
spacingScale = ttk.Scale(settings_frame, from_=-90, to=300, command=on_spacing_change)
spacingScale.set(0)
spacingValueLabel = ttk.Label(settings_frame, textvariable=spacingValue)
spacingTitleLabel.grid_remove()
spacingScale.grid_remove()
spacingValueLabel.grid_remove()

ttk.Label(settings_frame, text="Watermark center position").grid(column=1, row=12, sticky=W, pady=(10, 0))
positionCanvas = Canvas(settings_frame, width=POSITION_CANVAS_SIZE, height=POSITION_CANVAS_SIZE, bg="#f5f5f5", highlightthickness=1, highlightbackground="#b8b8b8")
positionCanvas.grid(column=1, row=13, columnspan=2, sticky=(W, E))
positionCanvas.bind("<Button-1>", on_position_canvas_drag)
positionCanvas.bind("<B1-Motion>", on_position_canvas_drag)
ttk.Label(settings_frame, textvariable=selectedPositionText).grid(column=1, row=14, columnspan=2, sticky=(W, E))

ttk.Label(settings_frame, text="Click a group in the middle panel\nto edit its settings.").grid(column=1, row=15, columnspan=2, sticky=(W, E), pady=(12, 0))

nbImageToEdit = IntVar(0)
watermark_cache_generation = 0
watermark_cache = {"path": None, "base_logo": None, "opacity_logos": {}}
preview_canvas_width = 0

# ROW 0
app_logo_image = None
try:
    app_logo_image = PhotoImage(file="./img/default_watermark.png")
    app_logo_image = app_logo_image.subsample(4, 4)
    app_logo_label = ttk.Label(mainframe, image=app_logo_image)
    app_logo_label.grid(column=1, row=0)
except Exception:
    app_logo_label = ttk.Label(mainframe, text="Watermark")
    app_logo_label.grid(column=1, row=0)

# ROW 1
folderInPath = StringVar()
folderInPath.set("Select a folder")
includeSubfolders = BooleanVar(value=False)
label_explore_in = ttk.Label(mainframe, text="Source images path (in)")
label_explore_in.configure(font=("Segoe UI", 10, "bold"))
label_explore_in.grid(column=1, row=1)
include_subfolders_check = ttk.Checkbutton(
    mainframe,
    text="Include subfolders",
    variable=includeSubfolders,
    command=on_include_subfolders_change,
)
include_subfolders_check.grid(column=1, row=3)
entry_explore_in = ttk.Entry(mainframe, textvariable=folderInPath, state="disabled", width=30)
entry_explore_in.configure(justify="left")
entry_explore_in.grid(column=1, row=2)
button_explore_in = Button(mainframe, text="Browse folders", command=lambda: browseFolder(1))
button_explore_in.grid(column=1, row=4)
separator_in = ttk.Separator(mainframe, orient="horizontal")
separator_in.grid(column=1, row=5, sticky=(W, E), padx=24)

# ROW 6
folderOutPath = StringVar()
folderOutPath.set("Select a folder")
label_explore_out = ttk.Label(mainframe, text="Watermarked images path (out)")
label_explore_out.configure(font=("Segoe UI", 10, "bold"))
label_explore_out.grid(column=1, row=6)
entry_explore_out = ttk.Entry(mainframe, textvariable=folderOutPath, state="disabled", width=30)
entry_explore_out.configure(justify="left")
entry_explore_out.grid(column=1, row=7)
button_explore_out = Button(mainframe, text="Browse folders", command=lambda: browseFolder(2))
button_explore_out.grid(column=1, row=8)
separator_out = ttk.Separator(mainframe, orient="horizontal")
separator_out.grid(column=1, row=9, sticky=(W, E), padx=24)

# ROW 10
watermark_path = StringVar()
watermark_path.set("./img/default_watermark.png")
label_explore_wm = ttk.Label(mainframe, text="Watermark")
label_explore_wm.configure(font=("Segoe UI", 10, "bold"))
label_explore_wm.grid(column=1, row=10)
entry_explore_wm = ttk.Entry(mainframe, textvariable=watermark_path, state="disabled", width=30)
entry_explore_wm.configure(justify="left")
entry_explore_wm.grid(column=1, row=11)
button_explore_wm = Button(mainframe, text="Browse files", command=lambda: browseFolder(3))
button_explore_wm.grid(column=1, row=12)
separator_wm = ttk.Separator(mainframe, orient="horizontal")
separator_wm.grid(column=1, row=13, sticky=(W, E), padx=24)

# ROW 14
txtButtonStart = StringVar()
txtButtonStart.set("Select In/Out folders and watermark file")
button_start = Button(mainframe, textvariable=txtButtonStart, command=lambda: filigrane(folderOutPath, watermark_path), state="disabled")
button_start.grid(column=1, row=14)

# ROW 8 (variables only; widgets are displayed in the bottom progress area)
txtCurrentOperation = StringVar()
text_counter = StringVar()
txtCurrentOperation.set("")

progress_frame = ttk.Frame(mainframe, padding="6 2 6 8")
progress_frame.grid(column=1, row=20, sticky=(S, W, E))
progress_frame.columnconfigure(0, weight=1)
progress_frame.columnconfigure(1, weight=0)
progress_frame.columnconfigure(2, weight=1)

progress_content = ttk.Frame(progress_frame)
progress_content.grid(column=1, row=0, sticky=(W, E))
progress_content.columnconfigure(0, weight=0)
progress_content.columnconfigure(1, weight=1)

labelCounter = ttk.Label(progress_content, textvariable=text_counter)
labelCounter.grid(column=0, row=0, sticky=W, padx=(0, 8))
label_current = ttk.Label(progress_content, textvariable=txtCurrentOperation, anchor="center")
label_current.grid(column=1, row=0, sticky=(W, E))

progressbar_operation = ttk.Progressbar(progress_content, orient="horizontal", mode="determinate", length=420)
progressbar_operation.grid(column=0, row=1, columnspan=2, sticky=(W, E), pady=(3, 0))

update_progression(started=False)
hard_refresh_groups_and_previews(reset_scroll=True)
set_settings_controls_state()
update_start_button_state()

for child in mainframe.winfo_children():
    child.grid_configure(padx=5, pady=5)

for child in settings_frame.winfo_children():
    child.grid_configure(padx=5, pady=3)

update_spacing_visibility()

def on_app_close():
    for group in preview_groups:
        stop_group_preview_animation(group)
    stop_preview_workers()
    window.destroy()


start_preview_workers()
process_preview_results()
backfill_missing_previews()
window.after(1200, start_background_update_check)
window.protocol("WM_DELETE_WINDOW", on_app_close)

window.mainloop()
