-- shop schema
CREATE TABLE customers (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL
);

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    sku VARCHAR(32) NOT NULL,
    quantity INT NOT NULL,
    status VARCHAR(16),
    customer_id BIGINT REFERENCES customers(id)
);

CREATE TABLE audit_log (
    id BIGINT PRIMARY KEY,
    entity VARCHAR(32),
    action VARCHAR(32)
);

CREATE VIEW open_orders AS SELECT * FROM orders WHERE status = 'NEW';
