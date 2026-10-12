package com.shop.web;

import com.shop.service.OrderService;
import org.junit.jupiter.api.Test;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;

class OrderControllerTest {
    private OrderService service;
    private org.springframework.test.web.servlet.MockMvc mvc;

    @Test
    void listsOrders() throws Exception {
        mvc.perform(get("/api/orders"));
    }

    @Test
    void serviceFindsAll() {
        service.findAll();
    }
}
