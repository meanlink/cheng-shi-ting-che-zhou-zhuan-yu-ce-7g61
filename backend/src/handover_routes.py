from fastapi import APIRouter, Depends

from . import handover
from .auth import current_user, require
from .contracts import Role
from .handover_input import DiversionClaimIn, HandoverReceiptIn, HandoverTotalIn

router = APIRouter(prefix='/handover', tags=['handover'])
analyst = require(Role.ANALYST)


@router.get('/receipts')
def receipts(user=Depends(current_user)):
    return handover.list_receipts()


@router.post('/receipts', status_code=201)
def register(item: HandoverReceiptIn, user=Depends(analyst)):
    return handover.register_receipt(item, user['email'])


@router.post('/receipts/{receipt_id}/correct')
def correct(receipt_id: int, item: HandoverTotalIn, user=Depends(analyst)):
    return handover.correct_total(receipt_id, item, user['email'])


@router.get('/claims')
def claims(user=Depends(current_user)):
    return handover.list_claims()


@router.post('/claims', status_code=201)
def claim(item: DiversionClaimIn, user=Depends(analyst)):
    return handover.claim_vehicles(item, user['email'])
