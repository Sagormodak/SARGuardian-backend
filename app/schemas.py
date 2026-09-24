from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=1024)


class UserResponse(BaseModel):
    id: str
    email: str
    created_at: datetime


class MessageResponse(BaseModel):
    message: str


class JobCreate(BaseModel):
    processing_metadata: dict[str, Any] = Field(default_factory=dict)


class JobResponse(BaseModel):
    job_id: str
    user_id: str
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    processing_metadata: dict[str, Any]
    result_folder_id: str | None
    result_file_ids: dict[str, str] | None
    error_code: str | None
    error_message_safe: str | None
