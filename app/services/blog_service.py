from typing import Optional, List, Dict, Any
from fastapi import Depends, Request, HTTPException
from app.core.logger import logger_manager
from app.models.user_model import RoleType
from app.crud.blog_crud import BlogCrud, get_blog_crud
from app.core.i18n.i18n import Language, get_message
from app.utils.client_info import client_info_utils


class BlogService:
    def __init__(self, blog_crud: BlogCrud):
        self.blog_crud = blog_crud
        self.logger = logger_manager.get_logger(__name__)

    async def get_blog_lists(
        self,
        language: Language,
        section_id: int,
        page: int = 1,
        size: int = 20,
        published_only: bool = True,
    ) -> tuple[list[dict], dict]:
        return await self.blog_crud.get_blog_lists(
            language=language,
            section_id=section_id,
            page=page,
            size=size,
            published_only=published_only,
        )

    async def create_blog(
        self,
        user_id: int,
        section_id: int,
        seo_id: int,
        chinese_title: str,
        chinese_description: str,
        chinese_content: dict,
        cover_id: int,
        blog_tags: List[int],
    ) -> int:
        return await self.blog_crud.create_blog(
            user_id=user_id,
            section_id=section_id,
            seo_id=seo_id,
            chinese_title=chinese_title,
            chinese_description=chinese_description,
            chinese_content=chinese_content,
            cover_id=cover_id,
            blog_tags=blog_tags,
        )

    async def update_blog(
        self,
        language: Language,
        user_id: int,
        blog_slug: str,
        seo_id: int,
        cover_id: int,
        chinese_title: str,
        chinese_description: str,
        chinese_content: dict,
        blog_tags: List[int] = [],

    ) -> str:
        return await self.blog_crud.update_blog(
            language=language,
            user_id=user_id,
            seo_id=seo_id,
            cover_id=cover_id,
            blog_slug=blog_slug,
            chinese_title=chinese_title,
            chinese_description=chinese_description,
            chinese_content=chinese_content,
            blog_tags=blog_tags,
        )

    async def get_blog_details_seo(
        self,
        blog_slug: str,
        language: Language,
    ) -> Optional[Dict]:
        return await self.blog_crud.get_blog_details_seo(
            blog_slug=blog_slug,
            language=language,
        )

    async def get_blog_details(
        self,
        request: Request,
        blog_slug: str,
        language: Language,
        is_editor: bool = False,
        user_id: Optional[int] = None,
    ) -> Optional[Dict]:
        return await self.blog_crud.get_blog_details(
            request=request,
            blog_slug=blog_slug,
            language=language,
            is_editor=is_editor,
            user_id=user_id,
        )

    async def get_blog_tts(
        self,
        blog_id: int,
        language: Language,
    ) -> Optional[Dict]:
        return await self.blog_crud.get_blog_tts(
            blog_id=blog_id,
            language=language,
        )

    async def get_blog_summary(
        self,
        blog_id: int,
        language: Language,
    ) -> Optional[Dict]:
        return await self.blog_crud.get_blog_summary(
            blog_id=blog_id,
            language=language,
        )

    async def get_blog_comment_lists(
        self,
        blog_id: int,
        language: Language,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.blog_crud.get_blog_comment_lists(
            blog_id=blog_id,
            limit=limit,
            cursor=cursor,
            language=language,
        )

    async def create_blog_comment(
        self,
        user_id: int,
        blog_id: int,
        comment: str,
        language: Language,
        parent_id: Optional[int] = None,
    ) -> bool:
        return await self.blog_crud.create_blog_comment(
            user_id=user_id,
            blog_id=blog_id,
            comment=comment,
            parent_id=parent_id,
            language=language,
        )

    async def update_blog_comment(
        self,
        user_id: int,
        comment_id: int,
        comment: str,
        language: Language,
    ) -> bool:
        return await self.blog_crud.update_blog_comment(
            user_id=user_id,
            comment_id=comment_id,
            comment=comment,
            language=language,
        )

    async def delete_blog_comment(
        self,
        user_id: int,
        role: RoleType,
        comment_id: int,
        language: Language,
    ) -> bool:
        return await self.blog_crud.delete_blog_comment(
            user_id=user_id,
            role=role,
            comment_id=comment_id,
            language=language,
        )

    async def save_blog_button(
        self,
        user_id: int,
        blog_id: int,
        language: Language,
    ) -> bool:
        return await self.blog_crud.save_blog_button(
            user_id=user_id,
            blog_id=blog_id,
            language=language,
        )

    async def update_blog_status(
        self,
        blog_id: int,
        language: Language,
        role: RoleType,
        is_published: Optional[bool] = None,
        is_archived: Optional[bool] = None,
        is_featured: Optional[bool] = None,
    ) -> bool:
        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message("common.insufficientPermissions", language),
            )
        return await self.blog_crud.update_blog_status(
            blog_id=blog_id,
            language=language,
            is_published=is_published,
            is_archived=is_archived,
            is_featured=is_featured,
        )

    async def get_blog_navigation(self, blog_id: int, language: Language) -> Optional[Dict]:
        return await self.blog_crud.get_blog_navigation(
            blog_id=blog_id,
            language=language,
        )

    async def get_blog_stats(self, blog_id: int, language: Language) -> Optional[Dict]:
        return await self.blog_crud.get_blog_stats(
            blog_id=blog_id,
            language=language,
        )

    async def like_blog_button(self, request: Request, blog_id: int, language: Language) -> bool:
        ip_address = client_info_utils.get_client_ip(request)
        return await self.blog_crud.like_blog_button(
            blog_id=blog_id,
            language=language, ip_address=ip_address,
        )

    async def delete_blog(self, blog_id: int, language: Language, role: RoleType) -> bool:

        if role != RoleType.admin:
            raise HTTPException(
                status_code=403,
                detail=get_message("common.insufficientPermissions", language),
            )
        return await self.blog_crud.delete_blog(
            blog_id=blog_id,
            language=language,
        )

    async def get_saved_blog_lists(
        self,
        user_id: int,
        language: Language,
        page: int = 1,
        size: int = 20
    ) -> List[Dict[str, Any]]:
        return await self.blog_crud.get_saved_blog_lists(
            user_id=user_id,
            page=page,
            size=size,
            language=language
        )

    async def get_recent_populor_blog(self, language: Language) -> List[Dict[str, Any]]:
        return await self.blog_crud.get_recent_populor_blog(
            language=language
        )

    async def get_blog_lists_by_tag_slug(
        self,
        tag_slug: str,
        language: Language,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict], dict]:
        """根据标签slug获取博客列表

        Args:
            tag_slug: 标签的slug
            language: 语言设置
            page: 页码
            size: 每页数量

        Returns:
            (items, pagination_metadata) - 博客列表和分页元数据
        """
        return await self.blog_crud.get_blog_lists_by_tag_slug(
            tag_slug=tag_slug,
            language=language,
            page=page,
            size=size,
        )

    async def get_archived_blog_lists(
        self,
        language: Language,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """获取归档的博客列表，使用 cursor pagination

        Args:
            language: 语言设置
            cursor: 可选的分页游标
            limit: 每页数量

        Returns:
            包含归档博客列表和分页信息的字典
        """
        return await self.blog_crud.get_archived_blog_lists(
            language=language,
            cursor=cursor,
            limit=limit,
        )


def get_blog_service(blog_crud: BlogCrud = Depends(get_blog_crud)) -> BlogService:
    return BlogService(blog_crud)
