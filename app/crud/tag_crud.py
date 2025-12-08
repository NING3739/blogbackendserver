import json
import slugify
from datetime import datetime, timezone
from fastapi import Depends, HTTPException
from sqlmodel import select, update, insert, delete, func
from sqlalchemy.orm import lazyload
from sqlalchemy import exists
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List, Dict, Any, Tuple
from app.models.tag_model import Tag
from app.models.user_model import RoleType
from app.core.database.mysql import mysql_manager
from app.core.database.redis import redis_manager
from app.core.logger import logger_manager
from app.core.i18n.i18n import get_message, Language
from app.utils.agent import agent_utils
from app.utils.offset_pagination import offset_paginator


class TagCrud:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.logger = logger_manager.get_logger(__name__)

    async def _get_tag_by_chinese_title(self, chinese_title: str) -> bool:
        statement = select(
            exists(select(Tag.id)).where(Tag.chinese_title == chinese_title)
        )
        result = await self.db.execute(statement)
        return bool(result.scalar_one())

    async def _get_tag_by_id(self, tag_id: int) -> Optional[Tag]:
        statement = select(Tag).options(lazyload("*")).where(Tag.id == tag_id)
        result = await self.db.execute(statement)
        return result.scalar_one_or_none()

    async def get_tag_lists(
        self,
        language: Language,
        page: int = 1,
        size: int = 20,
        published_only: Optional[bool] = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Get tag lists with traditional pagination"""
        # 验证分页参数
        try:
            page, size = offset_paginator.validate_pagination_params(
                page, size)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=get_message("common.invalidRequest", language),
            )

        # 缓存键（包含语言以避免不同语言之间的缓存污染）
        cache_key = f"tag_lists:lang={language}:page={page}:size={size}"
        cache_data = await redis_manager.get_async(cache_key)
        if cache_data:
            payload = json.loads(cache_data)
            return payload.get("items", []), payload.get("pagination", {})

        # 使用分页工具获取结果
        items, pagination_metadata = await offset_paginator.get_paginated_result(
            db=self.db,
            model_class=Tag,
            page=page,
            size=size,
            order_by=[Tag.created_at.desc(), Tag.id.desc()],
        )

        # 计算本月新增的tag数量
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            next_month_start = datetime(
                now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month_start = datetime(
                now.year, now.month + 1, 1, tzinfo=timezone.utc)

        count_this_month = await self.db.execute(
            select(func.count(Tag.id)).where(
                Tag.created_at.between(month_start, next_month_start))
        )
        count_this_month = count_this_month.scalar_one_or_none()

        # 计算本月更新的tag数量
        count_updated = await self.db.execute(
            select(func.count(Tag.id)).where(
                Tag.updated_at.between(month_start, next_month_start))
        )
        count_updated = count_updated.scalar_one_or_none()

        pagination_metadata["new_items_this_month"] = count_this_month
        pagination_metadata["updated_items_this_month"] = count_updated

        # 格式化响应数据
        response_items = [
            {
                "tag_id": tag.id,
                "slug": tag.slug,
                "created_at": tag.created_at.isoformat(),
                "updated_at": tag.updated_at.isoformat() if tag.updated_at else None,
            }
            for tag in items
        ]

        if published_only is True:
            for i, tag in enumerate(items):
                response_items[i].update({
                    "title": tag.chinese_title if language == Language.ZH_CN else tag.english_title,
                })
        else:
            for i, tag in enumerate(items):
                response_items[i].update({
                    "title": tag.chinese_title,
                })

        # 缓存结果
        cache_data = offset_paginator.create_response_data(
            response_items, pagination_metadata
        )
        await redis_manager.set_async(cache_key, json.dumps(cache_data))

        return response_items, pagination_metadata

    async def create_tag(
        self,
        role: RoleType,
        chinese_title: str,
        language: Language,
    ) -> bool:
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 检查tag是否已经存在
        existing_tag = await self._get_tag_by_chinese_title(chinese_title)
        if existing_tag:
            raise HTTPException(
                status_code=400,
                detail=get_message(
                    key="tag.createTag.tagAlreadyExists", lang=language),
            )

        # 翻译english name
        english_title = await agent_utils.translate(text=chinese_title)

        # 生成slug
        # 生成slug并限制长度
        slug = slugify.slugify(english_title, max_length=40)  # 限制在40字符以内，留一些缓冲

        # 插入tag
        await self.db.execute(
            insert(Tag).values(
                chinese_title=chinese_title,
                english_title=english_title,
                slug=slug,
            )
        )
        await self.db.commit()

        # 更新缓存
        await redis_manager.delete_pattern_async("tag_lists:*")

        return True

    async def update_tag(
        self,
        tag_id: int,
        role: RoleType,
        chinese_title: str,
        language: Language,
    ) -> bool:
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 检查tag是否已经存在
        tag = await self._get_tag_by_id(tag_id=tag_id)

        # 检查tag是否存在
        if not tag:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="tag.common.tagNotFound", lang=language),
            )

        # 检查是否存在相同的chinese_title（排除当前记录）
        duplicate_statement = (
            select(Tag)
            .options(lazyload("*"))
            .where(Tag.chinese_title == chinese_title, Tag.id != tag_id)
        )
        duplicate_result = await self.db.execute(duplicate_statement)
        existing_tag = duplicate_result.scalar_one_or_none()

        if existing_tag:
            raise HTTPException(
                status_code=400,
                detail=get_message(
                    key="tag.createTag.tagAlreadyExists", lang=language),
            )

        # 翻译english name
        if tag.chinese_title != chinese_title:
            english_title = await agent_utils.translate(text=chinese_title)
            # 生成slug并限制长度
            slug = slugify.slugify(
                english_title, max_length=40)  # 限制在40字符以内，留一些缓冲
        else:
            english_title = tag.english_title
            slug = tag.slug

        # 更新tag
        await self.db.execute(
            update(Tag)
            .where(Tag.id == tag_id)
            .values(
                chinese_title=chinese_title,
                english_title=english_title,
                slug=slug,
            )
        )
        await self.db.commit()

        # 更新缓存
        await redis_manager.delete_pattern_async("tag_lists:*")

        return True

    async def delete_tag(
        self,
        tag_id: int,
        role: RoleType,
        language: Language,
    ) -> bool:
        if role == RoleType.user:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 检查tag是否存在
        tag = await self._get_tag_by_id(tag_id)
        if not tag:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="tag.common.tagNotFound", lang=language),
            )

        # 删除tag
        await self.db.execute(delete(Tag).where(Tag.id == tag_id))
        await self.db.commit()

        # 更新缓存
        await redis_manager.delete_pattern_async("tag_lists:*")

        return True


def get_tag_crud(db: AsyncSession = Depends(mysql_manager.get_db)) -> TagCrud:
    return TagCrud(db)
