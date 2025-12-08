import json
import string
import random
from datetime import datetime

from typing import Optional, Dict, Any, Tuple, List
from fastapi import HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from sqlmodel import select, insert, func
from app.models.user_model import RoleType
from app.models.payment_model import Payment_Record, PaymentStatus, PaymentType
from app.models.project_model import Project
from app.utils.offset_pagination import offset_paginator
from app.core.database.mysql import mysql_manager
from app.core.database.redis import redis_manager
from app.core.i18n.i18n import get_message, Language
from app.core.logger import logger_manager
from app.crud.auth_crud import get_auth_crud


class PaymentCrud:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.auth_crud = get_auth_crud(db)
        self.logger = logger_manager.get_logger(__name__)

    def _generate_order_number(
        self, user_id: int, section_id: Optional[int], length: int
    ) -> str:
        data_stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        random_str = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=length)
        )
        # 处理 section_id 为 None 的情况
        section_id_str = str(section_id) if section_id is not None else "0"
        order_number = f"{data_stamp}{user_id}{section_id_str}{random_str}"
        return order_number

    async def _validate_project_for_payment(
        self, project_id: int, language: Language
    ) -> Project:
        """验证项目是否存在且可以进行支付，并返回项目实例

        Args:
            project_id: 项目ID

        Returns:
            Project: 包含状态和变现信息的项目实例

        Raises:
            HTTPException: 当项目不存在、已被删除、未发布或不活跃时
        """
        # 使用 joinedload 预加载相关数据，避免 N+1 查询
        result = await self.db.execute(
            select(Project)
            .options(
                joinedload(Project.project_monetization), joinedload(
                    Project.section)
            )
            .where(Project.id == project_id, Project.is_published == True)
        )
        project = result.unique().scalar_one_or_none()

        if not project:
            raise HTTPException(
                status_code=404,
                detail=get_message(
                    key="project.common.projectNotFound", lang=language),
            )

        # 验证变现设置
        if project.project_monetization.price <= 0:
            raise HTTPException(
                status_code=400,
                detail=get_message("payment.common.paymentFree", language),
            )

        return project

    async def _check_user_already_paid(self, user_id: int, project_id: int) -> bool:
        """检查用户是否已经成功支付过该项目

        Args:
            user_id: 用户ID
            project_id: 项目ID

        Returns:
            bool: 如果用户已支付返回True，否则返回False
        """
        # 使用 exists() 是检查记录存在性的最优方法
        exists_query = (
            select(Payment_Record.id)
            .where(
                Payment_Record.user_id == user_id,
                Payment_Record.project_id == project_id,
                Payment_Record.payment_status == PaymentStatus.success,
            )
            .exists()
        )

        result = await self.db.execute(select(exists_query))
        return result.scalar()

    async def _get_user_payment_record(
        self,
        user_id: int,
        project_id: int,
    ) -> Optional[Payment_Record]:
        """获取用户对特定项目的成功支付记录

        Args:
            user_id: 用户ID
            project_id: 项目ID

        Returns:
            Optional[Payment_Record]: 成功的支付记录，如果不存在返回None
        """
        result = await self.db.execute(
            select(Payment_Record)
            .options(
                joinedload(Payment_Record.user), joinedload(
                    Payment_Record.project)
            )
            .where(
                Payment_Record.user_id == user_id,
                Payment_Record.project_id == project_id,
                Payment_Record.payment_status == PaymentStatus.success,
            )
            .order_by(Payment_Record.created_at.desc())  # 获取最新的支付记录
        )
        return result.unique().scalar_one_or_none()

    async def create_payment_intent(
        self,
        user_id: int,
        project_id: int,
        cover_url: str,
        project_name: str,
        project_description: str,
        project_price: float,
        tax_name: str,
        tax_rate: float,
        tax_amount: float,
        final_amount: float,
        language: Language,
    ) -> Dict[str, Any]:
        # 检查project是否是可支付的project
        project = await self._validate_project_for_payment(project_id, language)

        # 检查用户是否已经购买过该项目
        have_paid = await self._check_user_already_paid(user_id, project_id)
        if have_paid:
            raise HTTPException(
                status_code=400,
                detail=get_message(
                    key="project.common.projectAlreadyExists", lang=language
                ),
            )

        # 生成订单号，使用项目的 section_id
        order_number = self._generate_order_number(
            user_id=user_id, section_id=project.section_id, length=6
        )

        # 获取用户信息
        user = await self.auth_crud.get_user_by_id(user_id)

        # 获取 section 名称
        section_name = None
        if project.section:
            section_name = (
                project.section.chinese_title
                if language == Language.ZH_CN
                else (project.section.english_title or project.section.chinese_title)
            )

        response = {
            "user": {
                "user_id": user.id,
                "user_name": user.username,
                "email": user.email,
            },
            "project": {
                "project_id": project.id,
                "cover_url": cover_url,
                "project_name": project_name,
                "project_description": project_description,
                "project_price": project_price,
                "project_section_name": section_name,
                "project_slug": project.slug,
            },
            "tax": {
                "tax_name": tax_name,
                "tax_rate": tax_rate,
                "tax_amount": tax_amount,
            },
            "final_amount": final_amount,  # 最终金额
            "order_number": order_number,
        }

        # 清理缓存
        await redis_manager.delete_pattern_async(
            f"project_details:lang={language}:project_slug={project.slug}:*"
        )

        return response

    async def create_payment_record(
        self,
        user_id: int,
        project_id: int,
        order_number: str,
        payment_type: PaymentType,
        final_amount: float,
        tax_name: str,
        tax_rate: float,
        tax_amount: float,
        payment_status: PaymentStatus,
    ) -> Payment_Record:
        # 创建支付记录
        result = await self.db.execute(
            insert(Payment_Record).values(
                user_id=user_id,
                project_id=project_id,
                tax_name=tax_name,
                tax_rate=tax_rate,
                tax_amount=tax_amount,
                order_number=order_number,
                payment_type=payment_type,
                amount=final_amount,
                payment_status=payment_status,
            )
        )
        await self.db.commit()

        # 获取创建的记录
        payment_record_id = result.inserted_primary_key[0]
        payment_record_result = await self.db.execute(
            select(Payment_Record).where(
                Payment_Record.id == payment_record_id)
        )
        payment_record = payment_record_result.scalar_one()

        # 清理缓存
        await redis_manager.delete_pattern_async(
            "payment_record:page=*:size=*:user_id=*"
        )

        return payment_record

    async def get_payment_record(
        self,
        language: Language,
        role: RoleType,
        page: int = 1,
        size: int = 20,
        user_id: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            page, size = offset_paginator.validate_pagination_params(
                page, size)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=get_message("common.invalidRequest", language),
            )

        cache_key = f"payment_record:page={page}:size={size}:user_id={user_id}"
        cache_data = await redis_manager.get_async(cache_key)

        if cache_data:
            payload = json.loads(cache_data)
            cached_items = payload.get("items", [])
            pagination_metadata = payload.get("pagination", {})
            return cached_items, pagination_metadata

        # 设置过滤器
        filters = {}
        if user_id:
            filters["user_id"] = user_id

        # 使用 offset_paginator 进行分页查询
        rows, pagination_metadata = await offset_paginator.get_paginated_result(
            db=self.db,
            model_class=Payment_Record,
            page=page,
            size=size,
            order_by=[Payment_Record.created_at.desc(),
                      Payment_Record.id.desc()],
            join_options=[
                joinedload(Payment_Record.user),
                joinedload(Payment_Record.project).joinedload(
                    Project.project_attachments
                ),
            ],
            filters=filters,
            language=language,
        )

        # 本月新增支付记录数量
        now = datetime.now()
        month_start = datetime(now.year, now.month, 1)
        if now.month == 12:
            next_month_start = datetime(now.year + 1, 1, 1)
        else:
            next_month_start = datetime(now.year, now.month + 1, 1)

        # 构建本月统计的查询条件
        monthly_conditions = [
            Payment_Record.created_at.between(month_start, next_month_start)
        ]

        # 如果有用户ID，则只统计该用户的记录
        if user_id:
            monthly_conditions.append(Payment_Record.user_id == user_id)

        count_this_month = await self.db.execute(
            select(func.count(Payment_Record.id)).where(*monthly_conditions)
        )
        count_this_month = count_this_month.scalar_one_or_none()

        # 获取本月总支付金额
        total_amount = await self.db.execute(
            select(func.sum(Payment_Record.amount)).where(*monthly_conditions)
        )
        total_amount = total_amount.scalar_one_or_none()

        pagination_metadata["new_items_this_month"] = count_this_month
        pagination_metadata["total_amount_this_month"] = total_amount

        # rows 为 Payment_Record 实例列表
        payment_records: List[Payment_Record] = rows

        items: List[Dict[str, Any]] = [
            {
                "payment_id": payment_record.id,
                "order_number": payment_record.order_number,
                "payment_type": payment_record.payment_type.name.capitalize(),
                "amount": payment_record.amount,
                "payment_status": payment_record.payment_status.name,
                "created_at": payment_record.created_at.isoformat()
                if payment_record.created_at
                else None,
                "project": {
                    "project_id": payment_record.project.id
                    if payment_record.project
                    else None,
                    "project_title": payment_record.project.chinese_title if role == RoleType.admin else (payment_record.project.english_title if language == Language.EN_US else payment_record.project.chinese_title),
                    "project_slug": payment_record.project.slug,
                }
                if payment_record.project
                else None,
                "attachment_id": payment_record.project.project_attachments.attachment_id
                if payment_record.project and payment_record.project.project_attachments
                else None,
            }
            for payment_record in payment_records
        ]

        # Add user information to each item if user_id filter is provided
        if role == RoleType.admin:
            for i, payment_record in enumerate(payment_records):
                items[i]["user"] = {
                    "user_id": payment_record.user.id if payment_record.user else None,
                    "username": payment_record.user.username if payment_record.user else None,
                    "email": payment_record.user.email if payment_record.user else None,
                } if payment_record.user else None

        # 缓存结果
        cache_payload = {"items": items, "pagination": pagination_metadata}
        await redis_manager.set_async(cache_key, json.dumps(cache_payload))

        return items, pagination_metadata


def get_payment_crud(db: AsyncSession = Depends(mysql_manager.get_db)) -> PaymentCrud:
    return PaymentCrud(db)
