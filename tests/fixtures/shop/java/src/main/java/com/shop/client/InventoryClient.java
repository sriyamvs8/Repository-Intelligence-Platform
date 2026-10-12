package com.shop.client;

import org.springframework.web.client.RestTemplate;

public class InventoryClient {
    private final RestTemplate rest = new RestTemplate();

    public boolean reserve(String sku, int quantity) {
        Boolean ok = rest.postForObject("http://inventory:8000/inventory/reserve", null, Boolean.class);
        return Boolean.TRUE.equals(ok);
    }
}
