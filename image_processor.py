"""图片处理工具 — 生成三级图片（原图/缩略图/预览）

从旧版 app/utils/image_processor.py 移植（纯函数，无架构依赖）。
"""

import os
import io
import logging

logger = logging.getLogger(__name__)

# 图片处理配置
THUMB_SIZE = (200, 200)
PREVIEW_WIDTH = 800
ALLOWED_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB


def process_image_file(file_path: str, order_id: int, upload_orders_dir: str) -> dict:
    """处理本地图片文件，生成三级图片（原图 + 缩略图 + 预览图）

    Args:
        file_path: 源图片绝对路径
        order_id: 订单ID
        upload_orders_dir: uploads/orders/ 目录路径

    Returns:
        {'image_url': str, 'image_path': str, 'thumb_url': str, 'original_url': str}
    """
    from PIL import Image

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"不支持的图片格式: {ext}")

    # 创建订单专属目录
    order_dir = os.path.join(upload_orders_dir, str(order_id))
    os.makedirs(order_dir, exist_ok=True)

    # 打开图片
    img = Image.open(file_path)

    # RGBA/调色板 → RGB
    if img.mode in ('RGBA', 'P'):
        img_rgb = img.convert('RGBA')
    else:
        img_rgb = img.convert('RGB')

    # 保存原图（复制到目标目录，保留原始格式）
    original_name = f'original{ext}'
    original_path = os.path.join(order_dir, original_name)
    img.save(original_path)

    # 生成缩略图 (200x200, center crop, WebP)
    thumb_img = img_rgb.copy()
    w, h = thumb_img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    thumb_img = thumb_img.crop((left, top, left + side, top + side))
    thumb_img = thumb_img.resize(THUMB_SIZE, Image.LANCZOS)
    thumb_path = os.path.join(order_dir, 'thumbnail.webp')
    thumb_img.save(thumb_path, 'WEBP', quality=85)

    # 生成预览图 (800px宽, 等比缩放, WebP)
    preview_img = img_rgb.copy()
    pw, ph = preview_img.size
    if pw > PREVIEW_WIDTH:
        ratio = PREVIEW_WIDTH / pw
        new_h = int(ph * ratio)
        preview_img = preview_img.resize((PREVIEW_WIDTH, new_h), Image.LANCZOS)
    preview_path = os.path.join(order_dir, 'preview.webp')
    preview_img.save(preview_path, 'WEBP', quality=88)

    # 构建 URL 路径
    image_url = f'/uploads/orders/{order_id}/preview.webp'
    thumb_url = f'/uploads/orders/{order_id}/thumbnail.webp'
    image_path = f'orders/{order_id}/original{ext}'
    original_url = f'/uploads/{image_path}'

    logger.info("订单 #%d 图片处理完成: %s", order_id, image_path)

    return {
        'image_url': image_url,
        'image_path': image_path,
        'thumb_url': thumb_url,
        'original_url': original_url,
    }


def process_uploaded_file(file_storage, order_id: int, upload_orders_dir: str) -> dict:
    """处理 Flask FileStorage 上传的图片

    Args:
        file_storage: Flask request.files['image'] 对象
        order_id: 订单ID
        upload_orders_dir: uploads/orders/ 目录路径

    Returns:
        与 process_image_file 相同的 dict 结构
    """
    from PIL import Image

    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"不支持的图片格式: {ext}")

    # 检查文件大小
    file_storage.seek(0, 2)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > MAX_UPLOAD_SIZE:
        raise ValueError(f"文件过大 ({size // 1024 // 1024}MB)，最大 10MB")

    # 创建订单专属目录
    order_dir = os.path.join(upload_orders_dir, str(order_id))
    os.makedirs(order_dir, exist_ok=True)

    # 读取图片数据
    img_data = file_storage.read()
    img = Image.open(io.BytesIO(img_data))

    # RGBA/调色板 → RGB
    if img.mode in ('RGBA', 'P'):
        img_rgb = img.convert('RGBA')
    else:
        img_rgb = img.convert('RGB')

    # 保存原图（保留原始格式）
    original_name = f'original{ext}'
    original_path = os.path.join(order_dir, original_name)
    with open(original_path, 'wb') as f:
        f.write(img_data)

    # 生成缩略图 (200x200, center crop, WebP)
    thumb_img = img_rgb.copy()
    w, h = thumb_img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    thumb_img = thumb_img.crop((left, top, left + side, top + side))
    thumb_img = thumb_img.resize(THUMB_SIZE, Image.LANCZOS)
    thumb_path = os.path.join(order_dir, 'thumbnail.webp')
    thumb_img.save(thumb_path, 'WEBP', quality=85)

    # 生成预览图 (800px宽, 等比缩放, WebP)
    preview_img = img_rgb.copy()
    pw, ph = preview_img.size
    if pw > PREVIEW_WIDTH:
        ratio = PREVIEW_WIDTH / pw
        new_h = int(ph * ratio)
        preview_img = preview_img.resize((PREVIEW_WIDTH, new_h), Image.LANCZOS)
    preview_path = os.path.join(order_dir, 'preview.webp')
    preview_img.save(preview_path, 'WEBP', quality=88)

    # 构建 URL 路径
    image_url = f'/uploads/orders/{order_id}/preview.webp'
    thumb_url = f'/uploads/orders/{order_id}/thumbnail.webp'
    image_path = f'orders/{order_id}/original{ext}'
    original_url = f'/uploads/{image_path}'

    logger.info("订单 #%d 图片上传成功: %s", order_id, image_path)

    return {
        'image_url': image_url,
        'image_path': image_path,
        'thumb_url': thumb_url,
        'original_url': original_url,
    }


def process_uploaded_file_multi(file_storage, order_id: int, upload_orders_dir: str, img_key: str) -> dict:
    """处理 Flask FileStorage 上传的图片（P15d 多图版）

    与 process_uploaded_file 相同，但用 img_key 生成唯一文件名，
    使同一订单可保存多张图片而不互相覆盖。

    Args:
        file_storage: Flask request.files['image'] 对象
        order_id: 订单ID
        upload_orders_dir: uploads/orders/ 目录路径
        img_key: 单图唯一标识（如 uuid 短码），用于文件命名

    Returns:
        {'image_url': str, 'image_path': str, 'thumb_url': str, 'original_url': str}
    """
    from PIL import Image

    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"不支持的图片格式: {ext}")

    # 检查文件大小
    file_storage.seek(0, 2)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > MAX_UPLOAD_SIZE:
        raise ValueError(f"文件过大 ({size // 1024 // 1024}MB)，最大 10MB")

    # 创建订单专属目录
    order_dir = os.path.join(upload_orders_dir, str(order_id))
    os.makedirs(order_dir, exist_ok=True)

    # 读取图片数据
    img_data = file_storage.read()
    img = Image.open(io.BytesIO(img_data))

    # RGBA/调色板 → RGB
    if img.mode in ('RGBA', 'P'):
        img_rgb = img.convert('RGBA')
    else:
        img_rgb = img.convert('RGB')

    # 保存原图（保留原始格式，文件名带 img_key 避免多图覆盖）
    original_name = f'original_{img_key}{ext}'
    original_path = os.path.join(order_dir, original_name)
    with open(original_path, 'wb') as f:
        f.write(img_data)

    # 生成缩略图 (200x200, center crop, WebP)
    thumb_img = img_rgb.copy()
    w, h = thumb_img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    thumb_img = thumb_img.crop((left, top, left + side, top + side))
    thumb_img = thumb_img.resize(THUMB_SIZE, Image.LANCZOS)
    thumb_name = f'thumb_{img_key}.webp'
    thumb_path = os.path.join(order_dir, thumb_name)
    thumb_img.save(thumb_path, 'WEBP', quality=85)

    # 生成预览图 (800px宽, 等比缩放, WebP)
    preview_img = img_rgb.copy()
    pw, ph = preview_img.size
    if pw > PREVIEW_WIDTH:
        ratio = PREVIEW_WIDTH / pw
        new_h = int(ph * ratio)
        preview_img = preview_img.resize((PREVIEW_WIDTH, new_h), Image.LANCZOS)
    preview_name = f'preview_{img_key}.webp'
    preview_path = os.path.join(order_dir, preview_name)
    preview_img.save(preview_path, 'WEBP', quality=88)

    # 构建 URL 路径
    image_url = f'/uploads/orders/{order_id}/{preview_name}'
    thumb_url = f'/uploads/orders/{order_id}/{thumb_name}'
    image_path = f'orders/{order_id}/{original_name}'
    original_url = f'/uploads/{image_path}'

    logger.info("订单 #%d 多图上传成功: %s", order_id, image_path)

    return {
        'image_url': image_url,
        'image_path': image_path,
        'thumb_url': thumb_url,
        'original_url': original_url,
    }


def save_without_pillow(file_storage, order_id: int, upload_orders_dir: str) -> dict:
    """Pillow 未安装时的回退方案：直接保存文件，不生成缩略图/预览图

    Args:
        file_storage: Flask FileStorage 对象
        order_id: 订单ID
        upload_orders_dir: uploads/orders/ 目录路径

    Returns:
        {'image_url': str, 'image_path': str}
    """
    import time

    ext = os.path.splitext(file_storage.filename)[1].lower()
    order_dir = os.path.join(upload_orders_dir, str(order_id))
    os.makedirs(order_dir, exist_ok=True)

    safe_name = f'order_{order_id}_{int(time.time())}{ext}'
    filepath = os.path.join(order_dir, safe_name)
    file_storage.save(filepath)

    image_url = f'/uploads/orders/{order_id}/{safe_name}'
    logger.warning("Pillow 未安装，使用直接保存模式")

    return {
        'image_url': image_url,
        'image_path': f'orders/{order_id}',
    }
