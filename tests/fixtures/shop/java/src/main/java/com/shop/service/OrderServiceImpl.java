package com.shop.service;

import com.shop.client.InventoryClient;
import com.shop.model.Order;
import com.shop.repo.OrderDao;
import com.shop.repo.OrderMapper;
import org.springframework.stereotype.Service;
import java.util.List;

@Service
public class OrderServiceImpl implements OrderService {
    private final OrderDao orderDao;
    private final OrderMapper orderMapper;
    private final InventoryClient inventory;

    public OrderServiceImpl(OrderDao orderDao, OrderMapper orderMapper, InventoryClient inventory) {
        this.orderDao = orderDao;
        this.orderMapper = orderMapper;
        this.inventory = inventory;
    }

    @Override
    public List<Order> findAll() {
        return orderDao.findAll();
    }

    @Override
    public Order findById(long id) {
        return orderMapper.selectById(id);
    }

    @Override
    public Order place(Order order) {
        if (!inventory.reserve(order.getSku(), order.getQuantity())) {
            throw new IllegalStateException("out of stock");
        }
        validate(order);
        return orderDao.insert(order);
    }

    @Override
    public void cancel(long id) {
        orderDao.updateStatus(id, "CANCELLED");
    }

    private void validate(Order order) {
        if (order.getQuantity() <= 0) {
            throw new IllegalArgumentException("quantity");
        }
    }
}
