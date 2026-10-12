from fastapi import APIRouter

from ..db.models import Item
from ..db.repository import StockRepository
from ..services import reserve_stock

router = APIRouter()
repo = StockRepository()


@router.post("/reserve")
def reserve(sku: str, quantity: int) -> bool:
    return reserve_stock(repo, sku, quantity)


@router.get("/items/{sku}")
def get_item(sku: str):
    return repo.get_item(sku)


@router.get("/low-stock")
def low_stock():
    return repo.low_stock(threshold=5)
