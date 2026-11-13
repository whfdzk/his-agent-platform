from __future__ import annotations
import os
import tempfile
import uuid
from fastapi import APIRouter, UploadFile, File, Form, Depends
from pydantic import BaseModel
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_admin, get_current_user_id
from src.core.concurrency import to_thread
from src.infra.rustfs_client import aupload_file
from src.core.config import get_settings
from src.infra.database import get_db
from src.core.exceptions import BizErrorCode, BizException
from src.infra.embedding import get_embeddings
from src.infra.milvus_client import get_milvus_client
from src.knowledge.doc_ingestion import ingest_file, ensure_knowledge_collection
from src.knowledge.doc_rag import COLLECTION_NAME

settings = get_settings()
router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

# 单份知识文档上限：此前无任何限制，一次全量 read() 就能打爆进程内存
MAX_DOC_BYTES = 50 * 1024 * 1024  # 50MB


class DocumentBrief(BaseModel):
    doc_name: str
    doc_type: str
    category: str


class UploadResponse(BaseModel):
    message: str
    chunks: int


class DeleteResponse(BaseModel):
    message: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentBrief]
    total: int


def _write_temp_file(content: bytes, suffix: str) -> str:
    """落临时文件供 LlamaIndex 解析；同步 IO，由调用方放进线程池。"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        return tmp.name


@router.post("/upload", response_model=UploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form(..., description="文档类型：guideline/drug_instruction/sop/literature"),
    category: str = Form("通用", description="所属分类：内科/外科/药剂科/行政等"),
    # 知识库写入是管理端操作，必须管理员登录
    admin=Depends(get_current_admin),
):
    """
    上传文档并导入知识库。
    支持 PDF、Word、TXT 格式。
    """
    allowed_ext = {".pdf", ".docx", ".doc", ".txt", ".md"}
    filename = os.path.basename(file.filename or "")
    if not filename:
        raise BizException(BizErrorCode.PARAM_ERROR, "文件名不能为空", status_code=400)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_ext:
        raise BizException(
            BizErrorCode.PARAM_ERROR,
            f"不支持的文件格式: {ext}，支持: {allowed_ext}", status_code=400,
        )

    # 有界读取：多读 1 字节用于判断是否超限，避免把超大文件整个读进内存
    content = await file.read(MAX_DOC_BYTES + 1)
    if not content:
        raise BizException(BizErrorCode.PARAM_ERROR, "文件内容为空", status_code=400)
    if len(content) > MAX_DOC_BYTES:
        raise BizException(
            BizErrorCode.PARAM_ERROR,
            f"文件过大（上限 {MAX_DOC_BYTES // 1024 // 1024}MB）", status_code=400,
        )

    # 对象 key 用 uuid：避免原始文件名进入存储路径（可枚举 / 特殊字符风险），
    # 展示用文件名仍保存在 Milvus 的 doc_name 元数据里。
    rustfs_key = f"knowledge/{doc_type}/{uuid.uuid4().hex}{ext}"
    try:
        await aupload_file(rustfs_key, content, file.content_type or "application/octet-stream")
    except Exception as e:
        logger.warning(f"RustFS 上传失败（不影响索引）: {e}")

    # 写临时文件供 LlamaIndex 解析
    tmp_path = await to_thread(_write_temp_file, content, ext)

    try:
        chunk_count = await ingest_file(
            file_path=tmp_path,
            doc_name=filename,
            doc_type=doc_type,
            category=category,
            embedding_model=get_embeddings(),
            milvus_client=get_milvus_client(),
        )
        return {"message": f"文档 '{filename}' 导入成功", "chunks": chunk_count}
    finally:
        await to_thread(os.unlink, tmp_path)


@router.post("/upload/notify")
async def upload_document_with_notify(
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    category: str = Form("通用"),
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """上传文档并导入知识库，同时发送知识更新通知。"""
    result = await upload_document(
        file=file, doc_type=doc_type, category=category, admin=admin,
    )

    from src.knowledge.notification import notify_doc_update
    try:
        await notify_doc_update(db, file.filename, doc_type, category, action="upload")
    except Exception as e:
        logger.warning(f"发送知识更新通知失败: {e}")

    return result


@router.delete("/docs/{doc_name}", response_model=DeleteResponse)
async def delete_document(doc_name: str, admin=Depends(get_current_admin)):
    """按文档名删除知识库中的所有分块。"""
    import hashlib
    doc_id = hashlib.md5(doc_name.encode()).hexdigest()[:16]
    milvus_client = get_milvus_client()
    try:
        await to_thread(
            milvus_client.delete,
            collection_name=COLLECTION_NAME,
            filter=f'doc_id == "{doc_id}"',
        )
        return {"message": f"文档 '{doc_name}' 已删除"}
    except Exception as e:
        logger.exception("知识库文档删除失败")
        raise BizException(BizErrorCode.INTERNAL, "文档删除失败", status_code=500) from None


@router.get("/docs", response_model=DocumentListResponse, dependencies=[Depends(get_current_admin)])
async def list_documents():
    """列出知识库中所有文档（按 doc_id 去重）。"""
    milvus_client = get_milvus_client()
    try:
        await to_thread(ensure_knowledge_collection, milvus_client)
        results = await to_thread(
            milvus_client.query,
            collection_name=COLLECTION_NAME,
            filter="chunk_index == 0",
            output_fields=["doc_id", "doc_name", "doc_type", "category"],
            limit=settings.DOC_LIST_LIMIT,
        )
        docs = []
        seen_doc_ids = set()
        for r in results:
            # 同一文档可能有多个 chunk_index == 0 的分块，按 doc_id 去重
            if r["doc_id"] in seen_doc_ids:
                continue
            seen_doc_ids.add(r["doc_id"])
            docs.append({
                "doc_name": r["doc_name"], "doc_type": r["doc_type"], "category": r["category"]
            })
        return {"documents": docs, "total": len(docs)}
    except Exception as e:
        # 原始异常只进日志，不回传客户端（可能含向量库地址等内部信息）
        logger.exception("知识库文档列表查询失败")
        raise BizException(BizErrorCode.INTERNAL, "文档列表查询失败", status_code=500) from None


# ── 反馈闭环 ─────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    # 兼容旧客户端保留该字段，但服务端一律以 token 里的身份为准
    user_id: str = ""
    question: str
    answer: str
    rating: int
    comment: str = ""
    intent: str = ""
    channels: str = ""


@router.post("/feedback")
async def submit_feedback(
    req: FeedbackRequest,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """提交知识问答反馈（点赞/踩）。rating: 1=有用, -1=无用。

    反馈与账号绑定，身份只取 token：此前信任 body 的 user_id，
    任何人可伪造他人身份刷反馈，污染知识库迭代数据。
    """
    from src.knowledge.feedback import save_feedback
    feedback_id = await save_feedback(
        db=db, user_id=current_user, question=req.question,
        answer=req.answer, rating=req.rating, comment=req.comment,
        intent=req.intent, channels=req.channels,
    )
    return {"feedback_id": feedback_id, "message": "反馈已记录"}


@router.get("/feedback/stats", dependencies=[Depends(get_current_admin)])
async def feedback_stats(db: AsyncSession = Depends(get_db)):
    """获取知识问答反馈统计。"""
    from src.knowledge.feedback import get_feedback_stats
    return await get_feedback_stats(db)


# ── 知识更新通知 ──────────────────────────────────────────────────────────

@router.get("/notifications", dependencies=[Depends(get_current_admin)])
async def list_notifications(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """获取未读的知识更新通知。"""
    from src.knowledge.notification import get_unread_notifications
    notifications = await get_unread_notifications(db, category=category)
    return {"notifications": notifications, "total": len(notifications)}


class MarkReadRequest(BaseModel):
    ids: list[int]


@router.post("/notifications/read", dependencies=[Depends(get_current_admin)])
async def mark_read(
    req: MarkReadRequest,
    db: AsyncSession = Depends(get_db),
):
    """标记通知为已读。"""
    from src.knowledge.notification import mark_notifications_read
    await mark_notifications_read(db, req.ids)
    return {"message": f"已标记 {len(req.ids)} 条通知为已读"}