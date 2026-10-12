package com.shop.repo;

import com.shop.model.Order;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface OrderMapper {
    Order selectById(long id);
}
