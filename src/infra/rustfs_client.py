"""RustFS 对象存储客户端（S3 兼容）。

RustFS 不提供第一方 Python SDK，官方推荐使用 AWS SDK for Python（boto3），
通过自定义 endpoint 指向 RustFS 的 S3 API（默认 9000 端口，控制台为 9001）。
RustFS 默认使用 path-style 寻址，签名版本 s3v4。
"""

import io

import boto3
from botocore.client import Config

from src.core.concurrency import to_thread
from src.core.config import get_settings
from loguru import logger


# 获取配置
settings = get_settings()


def _build_endpoint_url() -> str:
    """把配置中的 endpoint 归一化成带 scheme 的完整 URL。

    boto3 要求 endpoint_url 形如 http://host:port，
    而配置里可能沿用旧习惯写成 "localhost:9000"，这里统一补齐 scheme。
    """
    endpoint = settings.RUSTFS_ENDPOINT.strip().rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        scheme = "https" if settings.RUSTFS_SECURE else "http"
        endpoint = f"{scheme}://{endpoint}"
    return endpoint


# 创建 RustFS 客户端（boto3 的 S3 客户端，指向 RustFS 服务）
_rustfs_client = boto3.client(
    "s3",
    endpoint_url=_build_endpoint_url(),
    aws_access_key_id=settings.RUSTFS_ACCESS_KEY,
    aws_secret_access_key=settings.RUSTFS_SECRET_KEY,
    region_name=settings.RUSTFS_REGION,
    config=Config(
        signature_version="s3v4",            # RustFS 支持 v4 签名
        s3={"addressing_style": "path"},     # RustFS 默认 path-style 寻址
    ),
)


# 暴露 RustFS 客户端实例
def get_rustfs_client():
    """
    FastAPI Depends 注入用。
    直接返回模块级别的 RustFS（boto3 S3）客户端实例，不需要每次创建新实例。
    """
    return _rustfs_client


async def aensure_bucket_exists() -> None:
    """异步版本：boto3 是同步 IO，启动阶段同样堵住 event loop。"""
    await to_thread(ensure_bucket_exists)


# 保证桶的存在，如果不存在则创建； 在项目的启动周期中调用
def ensure_bucket_exists() -> None:
    """
    确保桶的存在，如果不存在则创建。
    """
    bucket = settings.RUSTFS_BUCKET
    existing = {item["Name"] for item in _rustfs_client.list_buckets().get("Buckets", [])}
    logger.info(f"检查桶 {bucket} 是否存在：{bucket in existing}")
    if bucket not in existing:
        _rustfs_client.create_bucket(Bucket=bucket)
        logger.info(f"桶 {bucket} 不存在，已自动创建")


# 上传文件到 RustFS 桶； 接受文件流和文件名作为参数
def upload_file(object_name: str, data: bytes,
                content_type: str = "application/octet-stream") -> str:
    """
    上传文件到 RustFS 桶。

    :param object_name: 文件名，用于在 RustFS 桶中唯一标识文件。
    :param data: 文件流数据。
    :param content_type: 文件内容类型，默认值为 "application/octet-stream"。
    :return: 文件名
    """
    # ensure_bucket_exists()   项目启动时调用，确保桶的存在

    _rustfs_client.put_object(Bucket=settings.RUSTFS_BUCKET,
                              Key=object_name,
                              Body=io.BytesIO(data),
                              ContentType=content_type,
                              ContentLength=len(data))

    return object_name


async def aupload_file(object_name: str, data: bytes,
                       content_type: str = "application/octet-stream") -> str:
    """异步版本：put_object 是同步 IO，直接调用会阻塞整个 worker 进程。"""
    return await to_thread(upload_file, object_name, data, content_type)


def download_file(object_name: str) -> bytes:
    """
    从 RustFS 桶下载文件。

    :param object_name: 文件名，用于在 RustFS 桶中唯一标识文件。
    :return: 文件字节内容
    """

    resp = _rustfs_client.get_object(Bucket=settings.RUSTFS_BUCKET,
                                     Key=object_name)
    body = resp["Body"]

    try:
        return body.read()
    except Exception as e:
        logger.error(f"下载文件 {object_name} 失败：{e}")
        raise e
    finally:
        body.close()  # 关闭响应流（同时释放底层连接）


def delete_object(object_name: str) -> None:
    """
    删除 RustFS 桶中的文件。
    :param object_name: 文件名，用于在 RustFS 桶中唯一标识文件。
    :return: None
    """

    _rustfs_client.delete_object(Bucket=settings.RUSTFS_BUCKET,
                                 Key=object_name)
    logger.info(f"删除文件 {object_name} 成功")
