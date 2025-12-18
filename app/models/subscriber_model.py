from sqlmodel import SQLModel, Field, Column
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.dialects.mysql import TIMESTAMP


class Subscriber(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    is_active: bool = True  # 是否订阅
    subscribed_at: datetime = Field(
        nullable=False,
        sa_type=TIMESTAMP,
        sa_column_kwargs={"server_default": text("CURRENT_TIMESTAMP")},
    )
    unsubscribed_at: Optional[datetime] = Field(
        default=None,
        nullable=True,
        sa_type=TIMESTAMP,
    )  # 用户取消订阅时间
    send_count: int = Field(default=0, nullable=False)  # 已发送邮件数量
    send_at: Optional[datetime] = Field(
        default=None,
        nullable=True,
        sa_type=TIMESTAMP,
    )  # 上次发送邮件时间

    def __str__(self):
        return f"Subscriber(id={self.id}, email={self.email}, is_active={self.is_active})"
