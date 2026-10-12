"""Hand-labelled ground truth for tests/fixtures/shop (written from reading the code, not from tool output).
Data access is expressed as (callable, READS|WRITES, table) regardless of whether it goes through a
SqlQuery node (EXECUTES -> READS/WRITES) or an ORM operation (direct READS/WRITES)."""

J = "method:com.shop."
P = "function:python.inventory."

HANDLED_BY = {
    ("api:GET /api/orders", J + "web.OrderController.list()"),
    ("api:GET /api/orders/{}", J + "web.OrderController.get(long)"),
    ("api:POST /api/orders", J + "web.OrderController.create(Order)"),
    ("api:DELETE /api/orders/{}", J + "web.OrderController.cancel(long)"),
    ("api:POST /inventory/reserve", P + "api.routes.reserve"),
    ("api:GET /inventory/items/{}", P + "api.routes.get_item"),
    ("api:GET /inventory/low-stock", P + "api.routes.low_stock"),
}

CALLS = {
    (J + "web.OrderController.list()", J + "service.OrderService.findAll()"),
    (J + "web.OrderController.get(long)", J + "service.OrderService.findById(long)"),
    (J + "web.OrderController.create(Order)", J + "service.OrderService.place(Order)"),
    (J + "web.OrderController.cancel(long)", J + "service.OrderService.cancel(long)"),
    (J + "service.OrderServiceImpl.findAll()", J + "repo.OrderDao.findAll()"),
    (J + "service.OrderServiceImpl.findById(long)", J + "repo.OrderMapper.selectById(long)"),
    (J + "service.OrderServiceImpl.place(Order)", J + "client.InventoryClient.reserve(String,int)"),
    (J + "service.OrderServiceImpl.place(Order)", J + "model.Order.getSku()"),
    (J + "service.OrderServiceImpl.place(Order)", J + "model.Order.getQuantity()"),
    (J + "service.OrderServiceImpl.place(Order)", J + "service.OrderServiceImpl.validate(Order)"),
    (J + "service.OrderServiceImpl.place(Order)", J + "repo.OrderDao.insert(Order)"),
    (J + "service.OrderServiceImpl.cancel(long)", J + "repo.OrderDao.updateStatus(long,String)"),
    (J + "service.OrderServiceImpl.validate(Order)", J + "model.Order.getQuantity()"),
    (J + "repo.OrderDao.insert(Order)", J + "model.Order.getSku()"),
    (J + "repo.OrderDao.insert(Order)", J + "model.Order.getQuantity()"),
    (J + "repo.OrderDao.insert(Order)", J + "model.Order.getCustomerId()"),
    (P + "api.routes.reserve", P + "services.reserve_stock"),
    (P + "api.routes.get_item", P + "db.repository.StockRepository.get_item"),
    (P + "api.routes.low_stock", P + "db.repository.StockRepository.low_stock"),
    (P + "services.reserve_stock", P + "db.repository.StockRepository.get_item"),
    (P + "services.reserve_stock", P + "db.repository.StockRepository.decrement"),
    (P + "services.reserve_stock", P + "services.notify_orders"),
    (P + "db.repository.StockRepository.decrement", P + "db.repository.StockRepository._log"),
}

OVERRIDES = {
    (J + "service.OrderServiceImpl.findAll()", J + "service.OrderService.findAll()"),
    (J + "service.OrderServiceImpl.findById(long)", J + "service.OrderService.findById(long)"),
    (J + "service.OrderServiceImpl.place(Order)", J + "service.OrderService.place(Order)"),
    (J + "service.OrderServiceImpl.cancel(long)", J + "service.OrderService.cancel(long)"),
}

IMPLEMENTS = {("class:com.shop.service.OrderServiceImpl", "class:com.shop.service.OrderService")}

DATA_ACCESS = {
    (J + "repo.OrderDao.findAll()", "READS", "table:orders"),
    (J + "repo.OrderDao.findAll()", "READS", "table:customers"),
    (J + "repo.OrderDao.insert(Order)", "WRITES", "table:orders"),
    (J + "repo.OrderDao.insert(Order)", "WRITES", "table:audit_log"),
    (J + "repo.OrderDao.updateStatus(long,String)", "WRITES", "table:orders"),
    (J + "repo.OrderMapper.selectById(long)", "READS", "table:orders"),
    (P + "db.repository.StockRepository.get_item", "READS", "table:items"),
    (P + "db.repository.StockRepository.decrement", "WRITES", "table:items"),
    (P + "db.repository.StockRepository.low_stock", "READS", "table:items"),
    (P + "db.repository.StockRepository._log", "WRITES", "table:stock_events"),
}

HAS_COLUMN = {("table:" + t, f"column:{t}.{c}") for t, cs in {
    "customers": ["id", "name"], "orders": ["id", "sku", "quantity", "status", "customer_id"],
    "audit_log": ["id", "entity", "action"], "warehouses": ["id", "name"],
    "items": ["sku", "quantity", "warehouse_id"]}.items() for c in cs}

FOREIGN_KEY = {("table:orders", "table:customers"), ("table:items", "table:warehouses")}

MAPS_TO = {("class:python.inventory.db.models.Warehouse", "table:warehouses"),
           ("class:python.inventory.db.models.Item", "table:items")}

CROSS_LANGUAGE_MAP = {
    (J + "client.InventoryClient.reserve(String,int)", "api:POST /inventory/reserve"),
    (P + "services.notify_orders", "api:GET /api/orders"),
}

TESTS = {
    ("test:com.shop.web.OrderControllerTest.listsOrders()", "api:GET /api/orders"),
    ("test:com.shop.web.OrderControllerTest.serviceFindsAll()", J + "service.OrderService.findAll()"),
    ("test:python.tests.test_routes.test_low_stock_endpoint", "api:GET /inventory/low-stock"),
    ("test:python.tests.test_routes.test_reserve_stock_rejects_missing", P + "services.reserve_stock"),
}

APIS = {"api:GET /api/orders", "api:GET /api/orders/{}", "api:POST /api/orders", "api:DELETE /api/orders/{}",
        "api:POST /inventory/reserve", "api:GET /inventory/items/{}", "api:GET /inventory/low-stock",
        "api:POST /api/refunds"}

# Must NOT exist (negative tests)
FORBIDDEN = [
    ("HANDLED_BY", "api:POST /api/refunds", None),          # documented in OpenAPI only
    ("TESTS", "test:com.shop.web.OrderControllerTest.stubsMockedService()", None),  # only stubs a @Mock
]
