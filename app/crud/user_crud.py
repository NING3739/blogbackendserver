import json
from datetime import datetime, timezone

from typing import Optional, List, Dict, Any, Tuple
from fastapi import HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select, update, func
from app.utils.offset_pagination import offset_paginator
from app.models.user_model import User, RoleType
from app.models.media_model import Media
from app.crud.auth_crud import get_auth_crud
from app.core.database.mysql import mysql_manager
from app.core.database.redis import redis_manager
from app.core.logger import logger_manager
from app.core.i18n.i18n import get_message, Language
from app.tasks.delete_user_media_task import delete_user_media_task


class UserCrud:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.auth_crud = get_auth_crud(db)
        self.logger = logger_manager.get_logger(__name__)

    async def get_profile(self, user_id: int, language: Language) -> Dict[str, Any]:
        """Get current user's profile by id."""
        key = f"user_profile_{user_id}"
        cached_data = await redis_manager.get_async(key)
        if cached_data:
            return json.loads(cached_data)

        statement = (
            select(User).options(selectinload(
                User.avatar)).where(User.id == user_id)
        )
        result = await self.db.execute(statement)

        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="auth.common.userNotFound", lang=language),
            )
        response = {
            "user_id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role.name,
            "avatar_url": user.avatar.thumbnail_filepath_url if user.avatar else None,
            "bio": user.bio,
            "city": user.city,
            "ip_address": user.ip_address,
            "longitude": user.longitude,
            "latitude": user.latitude,
            "account_status": {
                "is_active": user.is_active,
                "is_deleted": user.is_deleted,
                "is_verified": user.is_verified,
            },
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        }
        await redis_manager.set_async(key, json.dumps(response))

        return response

    async def update_my_bio(self, user_id: int, bio: str) -> bool:
        """Update current user's bio by id."""
        statement = update(User).where(User.id == user_id).values(bio=bio)
        await self.db.execute(statement)
        await self.db.commit()

        # 删除缓存
        await redis_manager.delete_async(f"user_profile_{user_id}")
        return True

    async def get_my_avatar(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get current user's avatar by id."""
        statement = select(Media).where(
            Media.user_id == user_id, Media.is_avatar == True
        )
        result = await self.db.execute(statement)
        media = result.scalar_one_or_none()

        if media is None:
            return None

        return {
            "media_id": media.id,
            "original_filepath_url": media.original_filepath_url,
        }

    async def get_user_lists(
        self,
        language: Language,
        page: int = 1,
        size: int = 20,
        role: Optional[RoleType] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        获取用户列表 - 使用传统分页方式

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
        cache_key = f"admin_all_users:page={page}:size={size}"
        cache_data = await redis_manager.get_async(cache_key)
        if cache_data:
            payload = json.loads(cache_data)
            return payload.get("items", []), payload.get("pagination", {})

        # 使用分页工具获取结果
        items, pagination_metadata = await offset_paginator.get_paginated_result(
            db=self.db,
            model_class=User,
            page=page,
            size=size,
            order_by=[User.created_at.desc(), User.id.desc()],
            join_options=[selectinload(User.avatar)],
            language=language,
            filters=[User.is_verified == True],
        )

        # 计算本月的用户数量
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        # 处理跨年的情况
        if now.month == 12:
            next_month_start = datetime(
                now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month_start = datetime(
                now.year, now.month + 1, 1, tzinfo=timezone.utc)
        count_this_month = await self.db.execute(
            select(func.count(User.id)).where(
                User.created_at.between(month_start, next_month_start))
        )
        count_this_month = count_this_month.scalar_one_or_none()

        # 计算活跃用户数量
        count_active_users = await self.db.execute(
            select(func.count(User.id)).where(User.is_active == True)
        )
        count_active_users = count_active_users.scalar_one_or_none()

        pagination_metadata["new_items_this_month"] = count_this_month
        pagination_metadata["active_users"] = count_active_users

        # 格式化响应数据
        response_items = [
            {
                "user_id": user.id,
                "username": user.username.lower(),
                "email": user.email,
                "role": user.role.name.capitalize(),
                "avatar_url": user.avatar.original_filepath_url
                if user.avatar
                else None,
                "bio": user.bio,
                "city": user.city,
                "ip_address": user.ip_address,
                "longitude": user.longitude,
                "latitude": user.latitude,
                "is_active": user.is_active,
                "is_verified": user.is_verified,
                "is_deleted": user.is_deleted,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            }
            for user in items
        ]

        # 缓存结果
        cache_data = offset_paginator.create_response_data(
            response_items, pagination_metadata
        )
        await redis_manager.set_async(cache_key, json.dumps(cache_data))

        return response_items, pagination_metadata

    async def delete_user(
        self, user_id: int, role: RoleType, current_user_id: int, language: Language
    ) -> bool:
        """Delete user by id using DB-level cascades/SET NULL to handle dependents."""
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        if user_id == current_user_id:
            self.logger.error(
                f"Admin {current_user_id} attempted to delete themselves."
            )
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # Ensure user exists
        result = await self.db.execute(select(User.id).where(User.id == user_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="auth.common.userNotFound", lang=language),
            )

        # 删除用户所有媒体文件
        delete_user_media_task.delay(user_id)

        # Invalidate caches (best-effort)
        await redis_manager.delete_async(f"user_profile_{user_id}")
        await redis_manager.delete_async(f"other_user_profile:{user_id}")
        await redis_manager.delete_pattern_async("admin_all_users:*")

        return True

    async def enable_or_disable_user(
        self,
        user_id: int,
        is_active: bool,
        current_user_id: int,
        role: int,
        language: Language,
    ) -> bool:
        """Enable or disable user by id"""
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        if current_user_id == user_id:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        user = await self.auth_crud.get_user_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="auth.common.userNotFound", lang=language),
            )

        user.is_active = is_active
        await self.db.commit()
        await self.db.refresh(user)

        # 删除缓存
        await redis_manager.delete_pattern_async("admin_all_users:*")
        await redis_manager.delete_async(f"user_profile_{user_id}")

        return True


def get_user_crud(db: AsyncSession = Depends(mysql_manager.get_db)) -> UserCrud:
    return UserCrud(db)
