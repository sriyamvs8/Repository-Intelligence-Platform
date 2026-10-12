from sqlalchemy import select, text

from .models import Item
from .session import SessionLocal

LOW_STOCK_SQL = "SELECT sku, quantity FROM items WHERE quantity < :threshold ORDER BY quantity"


class StockRepository:
    def __init__(self):
        self.session = SessionLocal()

    def get_item(self, sku):
        return self.session.scalars(select(Item).where(Item.sku == sku)).first()

    def decrement(self, sku, quantity):
        self.session.execute(text("UPDATE items SET quantity = quantity - :q WHERE sku = :sku"), {"q": quantity, "sku": sku})
        self._log(sku)

    def low_stock(self, threshold):
        return self.session.execute(text(LOW_STOCK_SQL), {"threshold": threshold}).all()

    def _log(self, sku):
        self.session.execute(text("INSERT INTO stock_events (sku, kind) VALUES (:sku, 'decrement')"), {"sku": sku})
