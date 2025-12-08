import json
from datetime import datetime, timezone
from sqlalchemy import func
from fastapi import Depends, HTTPException
from sqlmodel import select, update, insert, delete
from sqlalchemy.orm import lazyload
from sqlalchemy import exists
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List, Dict, Any, Tuple
from app.models.seo_model import Seo
from app.models.user_model import RoleType
from app.core.database.mysql import mysql_manager
from app.core.database.redis import redis_manager
from app.core.logger import logger_manager
from app.core.i18n.i18n import get_message, Language
from app.utils.agent import agent_utils
from app.utils.offset_pagination import offset_paginator


class SeoCrud:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.logger = logger_manager.get_logger(__name__)

    async def _get_seo_by_chinese_title(self, chinese_title: str) -> bool:
        statement = select(
            exists(select(Seo.id)).where(Seo.chinese_title == chinese_title)
        )
        result = await self.db.execute(statement)
        return bool(result.scalar_one())

    async def _get_seo_by_id(self, seo_id: int) -> bool:
        statement = select(exists(select(Seo.id)).where(Seo.id == seo_id))
        result = await self.db.execute(statement)
        return bool(result.scalar_one())

    async def get_seo_lists(
        self,
        language: Language,
        page: int = 1,
        size: int = 20,
        role: Optional[RoleType] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        获取SEO列表 - 使用传统分页方式

        Args:
            page: 页码，从1开始
            size: 每页数量
            role: 用户角色

        Returns:
            Tuple of (items, pagination_metadata)
        """
        # 验证分页参数
        try:
            page, size = offset_paginator.validate_pagination_params(
                page, size)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=get_message("common.invalidRequest", language),
            )

        # 检查权限
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 缓存键
        cache_key = f"seo_lists:lang={language}:page={page}:size={size}"
        cache_data = await redis_manager.get_async(cache_key)
        if cache_data:
            payload = json.loads(cache_data)
            return payload.get("items", []), payload.get("pagination", {})

        # 使用分页工具获取结果
        items, pagination_metadata = await offset_paginator.get_paginated_result(
            db=self.db,
            model_class=Seo,
            page=page,
            size=size,
            order_by=[Seo.created_at.desc(), Seo.id.desc()],
            join_options=[lazyload("*")],
        )

        # 计算本月新增的SEO数量
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            next_month_start = datetime(
                now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month_start = datetime(
                now.year, now.month + 1, 1, tzinfo=timezone.utc)

        count_this_month = await self.db.execute(
            select(func.count(Seo.id)).where(
                Seo.created_at.between(month_start, next_month_start))
        )
        count_this_month = count_this_month.scalar_one_or_none()

        # 计算本月更新的SEO数量
        count_updated = await self.db.execute(
            select(func.count(Seo.id)).where(
                Seo.updated_at.between(month_start, next_month_start))
        )
        count_updated = count_updated.scalar_one_or_none()

        pagination_metadata["new_items_this_month"] = count_this_month
        pagination_metadata["updated_items_this_month"] = count_updated

        # 格式化响应数据
        response_items = [
            {
                "seo_id": seo.id,
                "title": seo.chinese_title,
                "description": seo.chinese_description,
                "keywords": seo.chinese_keywords,
                "created_at": seo.created_at.isoformat(),
                "updated_at": seo.updated_at.isoformat() if seo.updated_at else None,
            }
            for seo in items
        ]

        # 缓存结果
        cache_data = offset_paginator.create_response_data(
            response_items, pagination_metadata
        )
        await redis_manager.set_async(cache_key, json.dumps(cache_data), ex=300)

        return response_items, pagination_metadata

    async def create_seo(
        self,
        role: RoleType,
        chinese_title: str,
        chinese_description: str,
        chinese_keywords: str,
        language: Language,
    ) -> bool:
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        seo = await self._get_seo_by_chinese_title(chinese_title)
        if seo:
            raise HTTPException(
                status_code=400,
                detail=get_message(
                    key="seo.createSeo.seoAlreadyExists", lang=language),
            )

        english_title = await agent_utils.translate(text=chinese_title)
        english_description = await agent_utils.translate(text=chinese_description)
        english_keywords = await agent_utils.translate(text=chinese_keywords)

        # 插入seo
        await self.db.execute(
            insert(Seo).values(
                chinese_title=chinese_title,
                english_title=english_title,
                chinese_description=chinese_description,
                english_description=english_description,
                chinese_keywords=chinese_keywords,
                english_keywords=english_keywords,
            )
        )
        await self.db.commit()

        # 更新缓存
        await redis_manager.delete_pattern_async("seo_lists:*")

        return True

    async def update_seo(
        self,
        seo_id: int,
        role: RoleType,
        chinese_title: str,
        chinese_description: str,
        chinese_keywords: str,
        language: Language,
    ) -> bool:
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 获取要更新的SEO记录
        seo_statement = select(Seo).options(
            lazyload("*")).where(Seo.id == seo_id)
        seo_result = await self.db.execute(seo_statement)
        seo = seo_result.scalar_one_or_none()

        if not seo:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="seo.common.seoNotFound", lang=language),
            )

        # 检查是否存在相同的chinese_title（排除当前记录）
        duplicate_statement = (
            select(Seo)
            .options(lazyload("*"))
            .where(Seo.chinese_title == chinese_title, Seo.id != seo_id)
        )
        duplicate_result = await self.db.execute(duplicate_statement)
        existing_seo = duplicate_result.scalar_one_or_none()

        if existing_seo:
            raise HTTPException(
                status_code=400,
                detail=get_message(
                    key="seo.updateSeo.seoAlreadyExists", lang=language),
            )

        # 更新SEO数据
        if seo.chinese_title != chinese_title:
            english_title = await agent_utils.translate(text=chinese_title)
        else:
            english_title = seo.english_title

        if seo.chinese_description != chinese_description:
            english_description = await agent_utils.translate(text=chinese_description)
        else:
            english_description = seo.english_description

        if seo.chinese_keywords != chinese_keywords:
            english_keywords = await agent_utils.translate(text=chinese_keywords)
        else:
            english_keywords = seo.english_keywords

        await self.db.execute(
            update(Seo)
            .where(Seo.id == seo_id)
            .values(
                chinese_title=chinese_title,
                english_title=english_title,
                chinese_description=chinese_description,
                english_description=english_description,
                chinese_keywords=chinese_keywords,
                english_keywords=english_keywords,
            )
        )
        await self.db.commit()

        # 更新缓存
        await redis_manager.delete_pattern_async("seo_lists:*")

        return True

    async def delete_seo(
        self,
        seo_id: int,
        role: RoleType,
        language: Language,
    ) -> bool:
        if role == RoleType.user:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        seo = await self._get_seo_by_id(seo_id)

        if not seo:
            raise HTTPException(
                status_code=400,
                detail=get_message(
                    key="seo.common.seoNotFound", lang=language),
            )

        await self.db.execute(delete(Seo).where(Seo.id == seo_id))
        await self.db.commit()

        # 更新缓存
        await redis_manager.delete_pattern_async("seo_lists:*")

        return True


def get_seo_crud(db: AsyncSession = Depends(mysql_manager.get_db)) -> SeoCrud:
    return SeoCrud(db)
