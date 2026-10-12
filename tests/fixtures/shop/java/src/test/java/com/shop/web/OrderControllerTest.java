package com.shop.web;

import com.shop.service.OrderService;
import org.junit.jupiter.api.Test;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;

class OrderControllerTest {
    private OrderService service;
    private org.springframework.test.web.servlet.MockMvc mvc;
    @org.mockito.Mock
    private OrderService mockedService;

    @Test
    void listsOrders() throws Exception {
        mvc.perform(get("/api/orders"));
    }

    @Test
    void serviceFindsAll() {
        service.findAll();
    }

    @Test
    void stubsMockedService() {
        org.mockito.BDDMockito.given(mockedService.findAll()).willReturn(java.util.List.of());
    }
}
