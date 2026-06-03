from pydantic import BaseModel, HttpUrl
from typing import Optional
from datetime import datetime

class TargetCreate(BaseModel):
    url: HttpUrl
    resource: str
    mode: str = "auto_clean"
    keyword_anchor: Optional[str] = None

class UserResponse(BaseModel):
    email: str
    name: Optional[str] = None
    role: str
    class Config: from_attributes = True

class PublishedAlertResponse(BaseModel):
    id: int
    resource: str
    url: str
    topic: str
    summary: str
    actionable_steps: str
    key_deadlines: Optional[str] = None
    published_at: datetime
    class Config: from_attributes = True