package com.shop.repo;

import com.shop.model.Order;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import java.util.List;

@Repository
public class OrderDao {
    private static final String FIND_ALL = "SELECT o.id, o.sku, o.quantity, c.name FROM orders o JOIN customers c ON c.id = o.customer_id";
    private final JdbcTemplate jdbc;

    public OrderDao(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public List<Order> findAll() {
        return jdbc.query(FIND_ALL, (rs, i) -> new Order());
    }

    public Order insert(Order order) {
        jdbc.update("INSERT INTO orders (sku, quantity, customer_id, status) VALUES (?, ?, ?, 'NEW')",
                order.getSku(), order.getQuantity(), order.getCustomerId());
        jdbc.update("INSERT INTO audit_log (entity, action) VALUES ('order', 'create')");
        return order;
    }

    public void updateStatus(long id, String status) {
        String sql = """
            UPDATE orders
               SET status = ?
             WHERE id = ?
            """;
        jdbc.update(sql, status, id);
    }
}
