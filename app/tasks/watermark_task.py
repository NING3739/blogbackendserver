import os
import subprocess
import tempfile
from pathlib import Path
from typing import Union, List
from app.core.celery import celery_app, with_db_init
from app.core.logger import logger_manager
from app.utils.s3_bucket import create_s3_bucket
from app.utils.io_utils import download_inputs_if_needed


logger = logger_manager.get_logger(__name__)


def generate_image_watermark(
    input_paths: Union[str, Path, List[Union[str, Path]]],
    output_dir: Union[str, Path],
    text: str,
    font_size: int = 36,
    font_color: str = "white",
    opacity: float = 0.6,
):
    """
    为图片添加右上角文字水印并转换为 WebP，支持单个文件或文件列表。

    Args:
        input_paths: 单个文件路径或文件列表
        output_dir: 输出文件夹
        text: 水印文字内容
        font_size: 字体大小
        font_color: 字体颜色
        opacity: 透明度 (0.0-1.0)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 统一 input_paths 为列表
    if isinstance(input_paths, (str, Path)):
        input_paths = [Path(input_paths)]
    else:
        input_paths = [Path(p) for p in input_paths]

    # 预处理文本
    input_text = "" if text is None else str(text)

    for file_path in input_paths:
        file_path = Path(file_path)
        if not file_path.is_file():
            logger.warning(f"跳过非文件: {file_path}")
            continue

        # 转换为 WebP 格式，添加水印
        output_file = output_dir / f"{file_path.stem}_watermark.webp"

        # 获取图片尺寸，动态计算字体大小
        fsize = font_size
        w, h = 1920, 1080  # 默认值
        try:
            cmd = [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(file_path),
            ]
            res = subprocess.run(
                cmd, check=True, capture_output=True, timeout=10)
            out = res.stdout.decode().strip()
            if out:
                w_h = out.split(",")
                if len(w_h) == 2:
                    w = int(w_h[0])
                    h = int(w_h[1])
                    if w > 0 and h > 0:
                        # 根据图片尺寸动态调整字体大小（约为短边的3%）
                        dyn = int(min(w, h) * 0.03)
                        fsize = max(dyn, font_size)
        except Exception:
            pass

        # 采样右上角区域颜色，计算对比色
        fcolor = font_color
        falpha = opacity
        try:
            crop = (
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=rgb24,"
                "crop=iw*0.25:ih*0.18:10:10,scale=1:1"
            )
            cmd = [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(file_path),
                "-frames:v",
                "1",
                "-vf",
                crop,
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ]
            res = subprocess.run(
                cmd, check=True, capture_output=True, timeout=10)
            data = res.stdout
            if len(data) >= 3:
                sr, sg, sb = data[0], data[1], data[2]
                inv_r, inv_g, inv_b = 255 - sr, 255 - sg, 255 - sb
                fcolor = f"0x{inv_r:02x}{inv_g:02x}{inv_b:02x}"
                brightness = (0.299 * sr + 0.587 * sg + 0.114 * sb) / 255.0
                if brightness >= 0.7:
                    falpha = 0.50  # 亮背景
                elif brightness >= 0.4:
                    falpha = 0.60  # 中等背景
                else:
                    falpha = 0.70  # 暗背景
        except Exception:
            pass

        # 转义文本中的特殊字符
        escaped_text = input_text.replace("\\", "\\\\").replace(
            ":", "\\:").replace("'", "\\'")

        # 简化 filter：直接在右上角添加水印文字
        filter_complex = (
            f"drawtext=text='{escaped_text}':"
            f"fontsize={fsize}:"
            f"fontcolor={fcolor}@{falpha}:"
            f"x=w-tw-10:y=10"  # 右上角：距离右边10px，距离顶部10px
        )

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(file_path),
            "-vf",
            filter_complex,
            "-c:v",
            "libwebp",
            "-q:v",
            "85",  # 高质量 WebP
            "-frames:v",
            "1",
            "-y",
            str(output_file),
        ]

        try:
            subprocess.run(command, check=True, timeout=60)
            logger.info(f"添加图片水印成功: {output_file}")
        except subprocess.TimeoutExpired:
            logger.error(f"添加图片水印超时: {file_path}")
        except subprocess.CalledProcessError as e:
            logger.error(f"添加图片水印失败: {file_path}, 错误: {e}")


def generate_video_watermark(
    input_paths: Union[str, Path, List[Union[str, Path]]],
    output_dir: Union[str, Path],
    text: str,
    font_size: int = 60,
    font_color: str = "white",
    opacity: float = 0.15,
    start_time: float = 0.0,
    duration: float = None,
):
    """
    为视频添加文字水印并转换为 MP4，支持单个视频或视频列表。
    优化版本：水印位于右上角，处理速度更快。

    Args:
        input_paths: 单个视频路径或视频列表
        output_dir: 输出目录
        text: 水印文字内容
        font_size: 字体大小（默认更大）
        font_color: 字体颜色
        opacity: 透明度 (0.0-1.0)
        start_time: 水印开始时间（秒）
        duration: 水印持续时间（秒），None 表示持续到视频结束
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 统一为列表
    if isinstance(input_paths, (str, Path)):
        input_paths = [Path(input_paths)]
    else:
        input_paths = [Path(p) for p in input_paths]

    # 预处理文本
    input_text = "" if text is None else str(text)

    for file_path in input_paths:
        file_path = Path(file_path)
        if not file_path.is_file():
            logger.warning(f"跳过非文件: {file_path}")
            continue

        # 转换为 MP4 格式，添加水印（H.264 编码速度更快）
        output_file = output_dir / f"{file_path.stem}_watermark.mp4"

        # 动态计算字号与颜色（取反色）、不透明度
        # 采样时间靠近 start_time，默认 0.5s
        sample_t = start_time if start_time and start_time > 0 else 0.5

        # 1) 获取视频尺寸并计算字体大小
        fsize = font_size
        w, h = 1920, 1080  # 默认值
        try:
            cmd = [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(file_path),
            ]
            res = subprocess.run(
                cmd, check=True, capture_output=True, timeout=10)
            out = res.stdout.decode().strip()
            if out:
                w_h = out.split(",")
                if len(w_h) == 2:
                    w = int(w_h[0])
                    h = int(w_h[1])
                    if w > 0 and h > 0:
                        # 根据视频尺寸动态调整字体大小（约为高度的3-4%）
                        dyn = int(min(w, h) * 0.04)
                        fsize = max(dyn, font_size)
                    else:
                        w, h = 1920, 1080
                else:
                    w, h = 1920, 1080
            else:
                w, h = 1920, 1080
        except Exception:
            w, h = 1920, 1080
            dyn = int(min(w, h) * 0.04)
            fsize = max(dyn, font_size)

        # 2) 右上角区域平均色采样并取反（用于确定水印颜色）
        fcolor = font_color
        falpha = opacity
        try:
            # 采样右上角区域（宽度25%，高度18%）
            crop = (
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=rgb24,"
                "crop=iw*0.25:ih*0.18:10:10,scale=1:1"
            )
            cmd = [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(sample_t),
                "-i",
                str(file_path),
                "-frames:v",
                "1",
                "-vf",
                crop,
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ]
            res = subprocess.run(
                cmd, check=True, capture_output=True, timeout=10)
            data = res.stdout
            if len(data) >= 3:
                sr, sg, sb = data[0], data[1], data[2]
                inv_r, inv_g, inv_b = 255 - sr, 255 - sg, 255 - sb
                fcolor = f"0x{inv_r:02x}{inv_g:02x}{inv_b:02x}"
                brightness = (0.299 * sr + 0.587 * sg + 0.114 * sb) / 255.0
                if brightness >= 0.7:
                    falpha = 0.50  # 亮背景，水印更明显
                elif brightness >= 0.4:
                    falpha = 0.60  # 中等背景，水印清晰可见
                else:
                    falpha = 0.70  # 暗背景，水印非常明显
        except Exception:
            # 如果采样失败，使用默认值
            pass

        # 覆盖区间控制
        enable_expr = (
            f"between(t,{start_time},{start_time + (duration if duration else 999999)})"
        )

        # 保持原始分辨率或限制为1080p（高质量）
        # 如果原始高度大于1080，则缩放到1080p
        target_h = min(h, 1080)
        # 确保宽度为偶数（H.264要求）
        scale_expr = f"scale=-2:{target_h}:flags=fast_bilinear"

        # 调整字体大小适配720p
        fsize_scaled = max(int(fsize * target_h / h), 30) if h > 0 else fsize

        # 计算右上角位置（距离右边和顶部各10像素）
        # 使用 drawtext 直接在右上角绘制，无需复杂的 overlay
        # 简化 filter：直接缩放并添加右上角水印，无需旋转和平铺
        # 转义文本中的特殊字符，避免 ffmpeg 命令解析错误
        escaped_text = input_text.replace("\\", "\\\\").replace(
            ":", "\\:").replace("'", "\\'")
        filter_complex = (
            f"[0:v]{scale_expr},"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=fast_bilinear,"
            f"drawtext=text='{escaped_text}':"
            f"fontsize={fsize_scaled}:"
            f"fontcolor={fcolor}@{falpha}:"
            f"x=w-tw-10:y=10:"  # 右上角位置：距离右边10px，距离顶部10px
            f"enable='{enable_expr}'[vout]"
        )

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-threads",
            "2",  # 2线程，适合2 vCPU服务器
            "-i",
            str(file_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "0:a?",  # 如果有音频流则映射
            "-c:v",
            "libx264",
            "-preset",
            "superfast",  # superfast 比 ultrafast 质量好，速度仍然很快
            "-crf",
            "23",  # CRF 23 平衡质量和文件大小
            "-profile:v",
            "main",  # main profile 质量更好，兼容性仍好
            "-level",
            "4.0",  # 支持1080p
            "-movflags",
            "+faststart",  # 优化网页播放
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",  # 高质量音频
            "-ac",
            "2",  # 立体声
            "-ar",
            "44100",  # 标准采样率
            "-y",
            str(output_file),
        ]

        try:
            subprocess.run(command, check=True, timeout=3600)  # 1小时超时
            logger.info(f"添加视频水印成功: {output_file}")
        except subprocess.TimeoutExpired:
            logger.error(f"添加视频水印超时: {file_path}")
        except subprocess.CalledProcessError as e:
            logger.error(f"添加视频水印失败: {file_path}, 错误: {e}")


@celery_app.task(
    name="generate_image_watermark", bind=True, max_retries=3, default_retry_delay=30
)
@with_db_init
def generate_image_watermark_task(
    self,
    input_paths: Union[str, Path, List[Union[str, Path]]],
    output_dir: Union[str, Path],
    text: str,
    font_size: int = 60,
    font_color: str = "white",
    opacity: float = 0.08,
) -> None:
    try:
        # 确保输入为本地文件
        local_inputs = download_inputs_if_needed(input_paths)

        if not local_inputs:
            logger.warning("没有可用的本地输入文件，跳过图片水印生成")
            return

        # 使用临时本地目录保存输出，再上传到S3
        local_output_dir = Path(tempfile.mkdtemp(prefix="wm_img_out_"))

        # 调用同步函数添加文字水印
        generate_image_watermark(
            input_paths=local_inputs,
            output_dir=local_output_dir,
            text=text,
            font_size=font_size,
            font_color=font_color,
            opacity=opacity,
        )
        logger.info(f"文字水印任务成功: {input_paths}")

        # 准备上传到S3的文件路径和S3键
        source_inputs = [Path(p) for p in (local_inputs or [])]

        # 生成水印文件路径
        watermark_paths = []
        s3_keys = []

        for file_path in source_inputs:
            watermark_file = local_output_dir / \
                f"{file_path.stem}_watermark.webp"
            if watermark_file.exists():
                watermark_paths.append(str(watermark_file))
                # 生成S3键：使用调用方传入的S3前缀
                s3_key = f"{output_dir}/{file_path.stem}_watermark.webp"
                s3_keys.append(s3_key)

        # 上传水印图片到S3
        if watermark_paths:
            with create_s3_bucket() as s3_bucket:
                # 所有水印图片都是WebP格式
                content_types = ["image/webp"] * len(watermark_paths)

                s3_bucket.upload_files(
                    file_paths=watermark_paths,
                    s3_keys=s3_keys,
                    metadata_list=[],
                    content_types=content_types,
                    acl="public-read",
                )
                logger.info(f"成功上传 {len(watermark_paths)} 个水印图片到S3")

                # 检查是否上传成功，只有成功上传的文件才删除本地副本
                successful_uploads = []
                for file_path, s3_key in zip(watermark_paths, s3_keys):
                    if s3_bucket.file_exists(s3_key):
                        successful_uploads.append(file_path)
                        logger.debug(f"确认文件上传成功: {s3_key}")
                    else:
                        logger.warning(f"文件上传可能失败，保留本地副本: {s3_key}")

                # 删除成功上传的本地文件
                for file_path in successful_uploads:
                    try:
                        os.remove(file_path)
                        logger.debug(f"已删除本地文件: {file_path}")
                    except OSError as e:
                        logger.warning(f"删除本地文件失败: {file_path}, 错误: {e}")

                logger.info(
                    f"成功删除 {len(successful_uploads)} 个本地文件，保留 {len(watermark_paths) - len(successful_uploads)} 个文件用于重试"
                )

    except Exception as e:
        logger.error(f"文字水印任务失败: {e}")
        # 如果任务失败，尝试重试
        if self.request.retries < self.max_retries:
            logger.warning(f"尝试重试任务，第 {self.request.retries + 1} 次重试")
            raise self.retry(exc=e, countdown=self.default_retry_delay)
        else:
            logger.error("任务重试次数已达上限，任务失败")
            raise


@celery_app.task(
    name="generate_video_watermark", bind=True, max_retries=3, default_retry_delay=30
)
@with_db_init
def generate_video_watermark_task(
    self,
    input_paths: Union[str, Path, List[Union[str, Path]]],
    output_dir: Union[str, Path],
    text: str,
    font_size: int = 60,
    font_color: str = "white",
    opacity: float = 0.15,
    start_time: float = 0.0,
    duration: float = None,
) -> None:
    try:
        # 确保输入为本地文件
        local_inputs = download_inputs_if_needed(input_paths)

        if not local_inputs:
            logger.warning("没有可用的本地输入文件，跳过视频水印生成")
            return

        # 使用临时本地目录保存输出，再上传到S3
        local_output_dir = Path(tempfile.mkdtemp(prefix="wm_vid_out_"))

        # 调用同步函数添加视频文字水印
        generate_video_watermark(
            input_paths=local_inputs,
            output_dir=local_output_dir,
            text=text,
            font_size=font_size,
            font_color=font_color,
            opacity=opacity,
            start_time=start_time,
            duration=duration,
        )
        logger.info(f"视频文字水印任务成功: {input_paths}")

        # 准备上传到S3的文件路径和S3键
        source_inputs = [Path(p) for p in (local_inputs or [])]

        # 生成水印视频文件路径
        watermark_paths = []
        s3_keys = []

        for file_path in source_inputs:
            watermark_file = local_output_dir / \
                f"{file_path.stem}_watermark.mp4"
            if watermark_file.exists():
                watermark_paths.append(str(watermark_file))
                # 生成S3键：使用调用方传入的S3前缀
                s3_key = f"{output_dir}/{file_path.stem}_watermark.mp4"
                s3_keys.append(s3_key)

        # 上传水印视频到S3
        if watermark_paths:
            with create_s3_bucket() as s3_bucket:
                # 所有水印视频都是MP4格式
                content_types = ["video/mp4"] * len(watermark_paths)

                s3_bucket.upload_files(
                    file_paths=watermark_paths,
                    s3_keys=s3_keys,
                    metadata_list=[],
                    content_types=content_types,
                    acl="public-read",
                )
                logger.info(f"成功上传 {len(watermark_paths)} 个水印视频到S3")

                # 检查是否上传成功，只有成功上传的文件才删除本地副本
                successful_uploads = []
                for file_path, s3_key in zip(watermark_paths, s3_keys):
                    if s3_bucket.file_exists(s3_key):
                        successful_uploads.append(file_path)
                        logger.debug(f"确认文件上传成功: {s3_key}")
                    else:
                        logger.warning(f"文件上传可能失败，保留本地副本: {s3_key}")

                # 删除成功上传的本地文件
                for file_path in successful_uploads:
                    try:
                        os.remove(file_path)
                        logger.debug(f"已删除本地文件: {file_path}")
                    except OSError as e:
                        logger.warning(f"删除本地文件失败: {file_path}, 错误: {e}")

                logger.info(
                    f"成功删除 {len(successful_uploads)} 个本地文件，保留 {len(watermark_paths) - len(successful_uploads)} 个文件用于重试"
                )

    except Exception as e:
        logger.error(f"视频文字水印任务失败: {e}")
        # 如果任务失败，尝试重试
        if self.request.retries < self.max_retries:
            logger.warning(f"尝试重试任务，第 {self.request.retries + 1} 次重试")
            raise self.retry(exc=e, countdown=self.default_retry_delay)
        else:
            logger.error("任务重试次数已达上限，任务失败")
            raise
