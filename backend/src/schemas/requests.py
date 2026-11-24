from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


class Import(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    csv: str = Field(min_length=1, max_length=1_000_000)


class Run(BaseModel):
    dataset_id: int = Field(gt=0)
    options: dict = Field(default_factory=dict)


class Review(BaseModel):
    decision: Literal['approved', 'returned']
    note: str = Field(min_length=2, max_length=1000)


class Delivery(BaseModel):
    recipient: str = Field(min_length=2, max_length=100)
    cadence: Literal['once', 'daily']
    scheduled_at: datetime | None = None


class OutageItem(BaseModel):
    elevator_id: str = Field(min_length=1, max_length=40)
    start_at: str = Field(min_length=5, max_length=5)
    end_at: str = Field(min_length=5, max_length=5)


class OutageUpdate(BaseModel):
    dataset_id: int = Field(gt=0)
    plan_date: str = Field(min_length=10, max_length=10)
    outages: list[OutageItem] = Field(default_factory=list, max_length=20)


class RescheduleRequest(BaseModel):
    dataset_id: int = Field(gt=0)
    plan_date: str = Field(min_length=10, max_length=10)
    threshold_minutes: float = Field(default=30, gt=0, le=480)
