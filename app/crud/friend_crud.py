import json
from datetime import datetime, timezone
from fastapi import Depends, HTTPException
from sqlmodel import select, update, insert, delete, func
from sqlalchemy import exists
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Dict, Any, Tuple, List
from app.models.friend_model import Friend, Friend_List, FriendType
from app.models.user_model import RoleType, User
from sqlalchemy.orm import lazyload, load_only
from app.core.database.mysql import mysql_manager
from app.core.database.redis import redis_manager
from app.core.logger import logger_manager
from app.core.i18n.i18n import get_message, Language
from app.utils.agent import agent_utils
from app.utils.keyset_pagination import paginator_desc
from app.utils.offset_pagination import offset_paginator
from app.tasks import notification_task
from app.schemas.common import NotificationType
from app.crud.auth_crud import get_auth_crud


class FriendCrud:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.auth_crud = get_auth_crud(db)
        self.logger = logger_manager.get_logger(__name__)

    async def _get_friend_by_id(self, friend_id: int) -> Optional[Friend]:
        statement = (
            select(Friend)
            .options(
                load_only(
                    Friend.id,
                    Friend.chinese_title,
                    Friend.english_title,
                    Friend.chinese_description,
                    Friend.english_description,
                ),
            )
            .where(Friend.id == friend_id)
        )
        result = await self.db.execute(statement)
        return result.scalar_one_or_none()

    async def _get_friend_list_by_id(self, friend_list_id: int) -> bool:
        statement = select(
            exists(select(Friend_List.id)).where(
                Friend_List.id == friend_list_id)
        )
        result = await self.db.execute(statement)
        return bool(result.scalar_one())

    async def get_friend_details(
        self,
        language: Language,
    ) -> Dict[str, any]:
        # 获取缓存
        cache_key = f"friend_details:lang={language}"
        cache_result = await redis_manager.get_async(cache_key)
        if cache_result:
            return json.loads(cache_result)

        # 获取友链列表
        statement = select(Friend)
        result = await self.db.execute(statement)
        friend = result.scalars().first()

        if not friend:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="friend.common.friendNotFound", lang=language),
            )

        response = {
            "friend_id": friend.id,
            "friend_title": friend.chinese_title
            if language == Language.ZH_CN
            else friend.english_title,
            "friend_description": friend.chinese_description
            if language == Language.ZH_CN
            else friend.english_description,
            "created_at": friend.created_at.isoformat() if friend.created_at else None,
            "updated_at": friend.updated_at.isoformat() if friend.updated_at else None,
        }

        # cache the result
        await redis_manager.set_async(cache_key, json.dumps(response))

        return response

    async def update_friend(
        self,
        role: RoleType,
        friend_id: int,
        chinese_title: str,
        chinese_description: str,
        language: Language,
    ) -> bool:
        # 检查友链是否存在
        friend = await self._get_friend_by_id(friend_id)
        if not friend:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="friend.common.friendNotFound", lang=language),
            )

        # 检查创建用户的身份
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 更新友链
        if chinese_title != friend.chinese_title:
            # 翻译
            english_title = await agent_utils.translate(text=chinese_title)
        else:
            english_title = friend.english_title

        if chinese_description != friend.chinese_description:
            english_description = await agent_utils.translate(text=chinese_description)
        else:
            english_description = friend.english_description

        # 更新友链
        await self.db.execute(
            update(Friend)
            .where(Friend.id == friend_id)
            .values(
                chinese_title=chinese_title,
                chinese_description=chinese_description,
                english_title=english_title,
                english_description=english_description,
            )
        )

        await self.db.commit()

        # 更新cache
        await redis_manager.delete_pattern_async("friend_details:*")

        return True

    # 获取友链列表
    async def get_friend_list(
        self,
        friend_id: int,
        language: Language,
        limit: int = 10,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取友链列表，使用 keyset pagination 进行分页

        Args:
            friend_id: 友链分类ID
            limit: 每页数量限制
            cursor: 分页游标

        Returns:
            包含分页信息和友链列表的字典
        """
        # 构建缓存键
        cache_key = f"friend_list:{friend_id}:{limit}:{cursor}:{language}"
        cache_data = await redis_manager.get_async(cache_key)

        if cache_data:
            return json.loads(cache_data)

        # 构建基础查询语句
        statement = (
            select(Friend_List)
            .options(lazyload("*"))
            .where(Friend_List.friend_id == friend_id)
        )

        # 应用 keyset pagination 过滤
        if cursor:
            statement = paginator_desc.apply_filters(
                statement, Friend_List.created_at, Friend_List.id, cursor
            )

        # 应用排序
        statement = statement.order_by(
            *paginator_desc.order_by(Friend_List.created_at, Friend_List.id)
        )

        # 限制数量（多查询一条用于判断是否有下一页）
        statement = statement.limit(limit + 1)

        # 执行查询
        result = await self.db.execute(statement)
        friend_lists = result.scalars().all()

        # 检查是否有更多数据
        has_next = len(friend_lists) > limit
        if has_next:
            friend_lists = friend_lists[:-1]  # 移除最后一条数据

        if not friend_lists:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="friend.common.friendNotFound", lang=language),
            )

        # 构建响应数据
        response_items = [
            {
                "id": friend_list.id,
                "type_name": friend_list.type.name,
                "logo_url": friend_list.logo_url,
                "site_url": friend_list.site_url,
                "title": friend_list.chinese_title
                if language == Language.ZH_CN
                else friend_list.english_title,
                "description": friend_list.chinese_description
                if language == Language.ZH_CN
                else friend_list.english_description,
                "created_at": friend_list.created_at.isoformat(),
                "updated_at": friend_list.updated_at.isoformat()
                if friend_list.updated_at
                else None,
            }
            for friend_list in friend_lists
            if friend_list.type == FriendType.featured
            or friend_list.type == FriendType.normal
        ]

        # 生成下一页的 cursor
        next_cursor = None
        if has_next and friend_lists:
            last_friend_list = friend_lists[-1]
            next_cursor = paginator_desc.encode_cursor(
                last_friend_list.created_at, last_friend_list.id
            )

        # 使用 keyset paginator 的 create_response_data 方法
        response = paginator_desc.create_response_data(
            items=response_items,
            limit=limit,
            has_next=has_next,
            next_cursor=next_cursor,
            items_key="friend_lists",
        )

        # 缓存结果
        await redis_manager.set_async(cache_key, json.dumps(response))

        return response

    async def get_friend_lists_by_offset_pagination(
        self,
        page: int = 1,
        size: int = 10,
    ) -> Tuple[List[Friend_List], Dict[str, Any]]:

        cach_key = f"friend_lists:offset_pagination:page={page}:size={size}"
        cached_data = await redis_manager.get_async(cach_key)
        if cached_data:
            cached_result = json.loads(cached_data)
            items = [Friend_List(**item) for item in cached_result['items']]
            pagination_metadata = cached_result['pagination_metadata']
            return items, pagination_metadata

        items, pagination_metadata = await offset_paginator.get_paginated_result(
            self.db,
            Friend_List,
            page=page,
            size=size
        )

        # 计算本月新增的友链数量
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            next_month_start = datetime(
                now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month_start = datetime(
                now.year, now.month + 1, 1, tzinfo=timezone.utc)

        count_this_month = await self.db.execute(
            select(func.count(Friend_List.id)).where(
                Friend_List.created_at.between(month_start, next_month_start))
        )
        count_this_month = count_this_month.scalar_one_or_none()
        pagination_metadata["new_items_this_month"] = count_this_month

        return items, pagination_metadata

    async def create_single_friend(
        self,
        friend_id: int,
        user_id: int,
        logo_url: str,
        site_url: str,
        chinese_title: str,
        chinese_description: str,
        language: Language,
    ) -> bool:
        # 检查是否有friend
        friend = await self._get_friend_by_id(friend_id)
        self.logger.info(
            f"friend found: {friend.chinese_title if friend else None}")
        if not friend:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="friend.common.friendNotFound", lang=language),
            )
        # 翻译
        if chinese_title:
            english_title = await agent_utils.translate(text=chinese_title)

        if chinese_description:
            english_description = await agent_utils.translate(text=chinese_description)

        # 创建友链
        await self.db.execute(
            insert(Friend_List).values(
                friend_id=friend_id,
                user_id=user_id,
                logo_url=logo_url,
                site_url=site_url,
                chinese_title=chinese_title,
                english_title=english_title,
                chinese_description=chinese_description,
                english_description=english_description,
            )
        )
        await self.db.commit()

        # 发送通知
        try:
            # 使用 auth_crud 获取用户信息
            user = await self.auth_crud.get_user_by_id(user_id)

            if not user:
                self.logger.warning(
                    f"发送友链请求通知失败：用户不存在，user_id: {user_id}"
                )
            else:
                message = (
                    f"用户 - {user.username}, {user.email}\n"
                    f"请求成为您的好友\n"
                    f"请登录后台管理页面查看详情。\n"
                )
                notification_task.delay(
                    notification_type=NotificationType.FRIEND_REQUEST.value,
                    message=message
                )
                self.logger.info(
                    f"友链请求通知已发送，用户: {user.username} ({user.email}), 友链分类: {friend.chinese_title}"
                )
        except Exception as e:
            self.logger.error(
                f"发送友链请求通知失败，user_id: {user_id}, friend_id: {friend_id}, 错误: {str(e)}"
            )

    async def delete_single_friend(
        self,
        role: RoleType,
        friend_list_id: int,
        language: Language,
    ) -> bool:
        # 检查创建用户的身份
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 检查友链是否存在
        friend = await self._get_friend_list_by_id(friend_list_id)
        if not friend:
            self.logger.error(
                f"Single friend list not found: {friend_list_id}")
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="friend.common.friendNotFound", lang=language),
            )

        # 删除友链
        await self.db.execute(
            delete(Friend_List).where(Friend_List.id == friend_list_id)
        )
        await self.db.commit()

        # 更新cache
        cache_key = "friend_list:*"
        await redis_manager.delete_pattern_async(cache_key)

        return True

    async def update_friend_list_type(
        self,
        friend_list_id: int,
        type: FriendType,
        role: RoleType,
        language: Language,
    ) -> bool:
        # 检查创建用户的身份
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message(
                    key="common.insufficientPermissions", lang=language),
            )

        # 检查友链是否存在
        friend = await self._get_friend_list_by_id(friend_list_id)
        if not friend:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="friend.common.friendNotFound", lang=language),
            )

        # 更新友链类型
        await self.db.execute(
            update(Friend_List)
            .where(Friend_List.id == friend_list_id)
            .values(type=type)
        )
        await self.db.commit()

        # 更新cache
        await redis_manager.delete_pattern_async("friend_list:*")
        await redis_manager.delete_pattern_async("friend_lists:offset_pagination:*")

        return True


def get_friend_crud(db: AsyncSession = Depends(mysql_manager.get_db)) -> FriendCrud:
    return FriendCrud(db)
