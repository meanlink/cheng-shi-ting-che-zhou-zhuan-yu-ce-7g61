from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=500)]
Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=80)]
ReceiptNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=48)]
PositiveId = Annotated[int, Field(gt=0)]


class HandoverReceiptIn(BaseModel):
    lot_code: str
    service_day: date
    reference: Reference
    total_vehicles: int = Field(ge=0, le=1000000)
    reason: Note


class HandoverTotalIn(BaseModel):
    total_vehicles: int = Field(ge=0, le=1000000)


class DiversionClaimIn(BaseModel):
    receipt_id: PositiveId
    diversion_id: PositiveId
    receipt_no: ReceiptNo
    claimed_vehicles: int = Field(gt=0, le=1000000)
