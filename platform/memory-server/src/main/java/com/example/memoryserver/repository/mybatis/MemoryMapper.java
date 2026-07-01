package com.example.memoryserver.repository.mybatis;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.example.memoryserver.model.MemoryEntity;
import org.apache.ibatis.annotations.Mapper;

/**
 * MyBatis-Plus mapper for MemoryEntity.
 * Auto-generates basic CRUD; complex queries use LambdaQueryWrapper in the repository impl.
 */
@Mapper
public interface MemoryMapper extends BaseMapper<MemoryEntity> {
}
