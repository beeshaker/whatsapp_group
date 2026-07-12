from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("message_id", "issue_index", name="uq_incidents_message_id_issue_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    property_name: Mapped[str] = mapped_column(Text, nullable=False)
    reporter_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reporter_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    vehicle_plate: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="review", server_default="review")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    reminder_offset_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    issue_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class IncidentUpdate(Base):
    __tablename__ = "incident_updates"
    __table_args__ = (
        UniqueConstraint("message_id", "issue_index", name="uq_incident_updates_message_id_issue_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reporter_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reporter_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ai_linked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    relinked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    issue_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class IncidentMedia(Base):
    __tablename__ = "incident_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    update_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("incident_updates.id"), nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mimetype: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IncidentStatusHistory(Base):
    __tablename__ = "incident_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(15), nullable=False, default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserGroup(Base):
    __tablename__ = "user_groups"
    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_user_groups"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    group_id: Mapped[str] = mapped_column(Text, nullable=False)


class AdminProfile(Base):
    __tablename__ = "admin_profiles"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    whatsapp_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AdminGroupSubscription(Base):
    __tablename__ = "admin_group_subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_admin_subscriptions"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    group_id: Mapped[str] = mapped_column(Text, nullable=False)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    messages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IncidentCategory(Base):
    __tablename__ = "incident_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
