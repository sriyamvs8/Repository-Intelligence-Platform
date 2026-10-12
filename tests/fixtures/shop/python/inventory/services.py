import requests

from .db.repository import StockRepository

ORDERS_URL = "http://orders:8080/api/orders"


def reserve_stock(repo: StockRepository, sku: str, quantity: int) -> bool:
    item = repo.get_item(sku)
    if item is None or item.quantity < quantity:
        return False
    repo.decrement(sku, quantity)
    notify_orders(sku)
    return True


def notify_orders(sku: str) -> None:
    requests.get(f"{ORDERS_URL}")
