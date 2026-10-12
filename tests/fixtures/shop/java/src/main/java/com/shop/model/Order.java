package com.shop.model;

public class Order {
    private long id;
    private String sku;
    private int quantity;
    private long customerId;

    public String getSku() { return sku; }
    public int getQuantity() { return quantity; }
    public long getCustomerId() { return customerId; }
}
