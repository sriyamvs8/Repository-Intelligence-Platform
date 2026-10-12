package com.shop.web;

import com.shop.model.Order;
import com.shop.service.OrderService;
import org.springframework.web.bind.annotation.*;
import java.util.List;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @GetMapping
    public List<Order> list() {
        return orderService.findAll();
    }

    @GetMapping("/{id}")
    public Order get(@PathVariable long id) {
        return orderService.findById(id);
    }

    @PostMapping
    public Order create(@RequestBody Order order) {
        return orderService.place(order);
    }

    @RequestMapping(value = "/{id}", method = RequestMethod.DELETE)
    public void cancel(@PathVariable long id) {
        orderService.cancel(id);
    }
}
