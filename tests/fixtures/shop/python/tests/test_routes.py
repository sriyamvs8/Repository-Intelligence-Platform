from fastapi.testclient import TestClient

from inventory.app import app
from inventory.services import reserve_stock

client = TestClient(app)


def test_low_stock_endpoint():
    resp = client.get("/inventory/low-stock")
    assert resp.status_code == 200


def test_reserve_stock_rejects_missing():
    assert reserve_stock(None, "x", 1) is False
