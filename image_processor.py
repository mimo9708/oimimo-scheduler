"""图片处理工具 — 生成三级图片（原图/缩略图/预览）

从旧版 app/utils/image_processor.py 移植（纯函数，无架构依赖）。
"""

import os
import io
import logging

logger = logging.getLogger(__name__)

# 图片处理配置
THUMB_SIZE = (200, 200)
THUMB_BG = (255, 255, 255)  # Spec 28 D2: contain letterbox 白色底
PREVIEW_WIDTH = 800
ALLOWED_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB


def _make_contain_thumb(img_rgb, size=THUMB_SIZE, bg_color=THUMB_BG):
    """contain + letterbox 缩略图：完整保留图片内容，居中放置在指定背景色上。

    替代原 center crop 模式（Spec 28 D2/D3）。
    """
    from PIL import Image
    w, h = img_rgb.size
    ratio = min(size[0] / w, size[1] / h)
    new_w, new_h = int(w * ratio), int(h * ratio)
    thumb = img_rgb.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new('RGB', size, bg_color)
    offset_x = (size[0] - new_w) // 2
    offset_y = (size[1] - new_h) // 2
    canvas.paste(thumb, (offset_x, offset_y))
    return canvas


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

    # 生成缩略图 (200x200, contain + letterbox, WebP) — Spec 28 D2/D3
    thumb_img = _make_contain_thumb(img_rgb)
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
    preview_img.save(preview_path, 'WEBP', quality=72)  # Spec 30 D5: 88→72, 体积降30-40%, 视觉差异极小

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

    # 生成缩略图 (200x200, contain + letterbox, WebP) — Spec 28 D2/D3
    thumb_img = _make_contain_thumb(img_rgb)
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
    preview_img.save(preview_path, 'WEBP', quality=72)  # Spec 30 D5: 88→72, 体积降30-40%, 视觉差异极小

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

    # 生成缩略图 (200x200, contain + letterbox, WebP) — Spec 28 D2/D3
    thumb_img = _make_contain_thumb(img_rgb)
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
    preview_img.save(preview_path, 'WEBP', quality=72)  # Spec 30 D5: 88→72, 体积降30-40%, 视觉差异极小

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


def process_tool_image(file_storage, upload_scope_dir: str, scope_name: str,
                       owner_key, img_key: str) -> dict:
    """Spec 22 小工具通用图片处理（价目表例图等）

    与 process_uploaded_file_multi 同链路三级产物（原图/预览 800px/缩略 200×200），
    但 URL 前缀按 scope 参数化，不硬编码 /uploads/orders/。

    Args:
        file_storage: Flask request.files 对象
        upload_scope_dir: uploads/<scope_name>/ 目录绝对路径
        scope_name: 作用域名（如 'pricelist'），用于 URL 前缀
        owner_key: 归属实体标识（如价目表项目 id），作为子目录名
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

    # 创建作用域专属目录
    owner_dir = os.path.join(upload_scope_dir, str(owner_key))
    os.makedirs(owner_dir, exist_ok=True)

    # 读取图片数据
    img_data = file_storage.read()
    img = Image.open(io.BytesIO(img_data))

    # RGBA/调色板 → RGB
    if img.mode in ('RGBA', 'P'):
        img_rgb = img.convert('RGBA')
    else:
        img_rgb = img.convert('RGB')

    # 保存原图（保留原始格式，文件名带 img_key）
    original_name = f'original_{img_key}{ext}'
    original_path = os.path.join(owner_dir, original_name)
    with open(original_path, 'wb') as f:
        f.write(img_data)

    # 生成缩略图 (200x200, contain + letterbox, WebP) — Spec 28 D2/D3
    thumb_img = _make_contain_thumb(img_rgb)
    thumb_name = f'thumb_{img_key}.webp'
    thumb_img.save(os.path.join(owner_dir, thumb_name), 'WEBP', quality=85)

    # 生成预览图 (800px宽, 等比缩放, WebP)
    preview_img = img_rgb.copy()
    pw, ph = preview_img.size
    if pw > PREVIEW_WIDTH:
        ratio = PREVIEW_WIDTH / pw
        new_h = int(ph * ratio)
        preview_img = preview_img.resize((PREVIEW_WIDTH, new_h), Image.LANCZOS)
    preview_name = f'preview_{img_key}.webp'
    preview_img.save(os.path.join(owner_dir, preview_name), 'WEBP', quality=72)  # Spec 30 D5: 88→72, 体积降30-40%, 视觉差异极小

    # 构建 URL 路径（按 scope 前缀）
    image_url = f'/uploads/{scope_name}/{owner_key}/{preview_name}'
    thumb_url = f'/uploads/{scope_name}/{owner_key}/{thumb_name}'
    image_path = f'{scope_name}/{owner_key}/{preview_name}'
    original_url = f'/uploads/{scope_name}/{owner_key}/{original_name}'

    logger.info("小工具图片上传成功 [%s/%s]: %s", scope_name, owner_key, image_path)

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
