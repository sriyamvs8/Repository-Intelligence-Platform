package com.shop.service;

import com.shop.model.Order;
import java.util.List;

public interface OrderService {
    List<Order> findAll();
    Order findById(long id);
    Order place(Order order);
    void cancel(long id);
}
