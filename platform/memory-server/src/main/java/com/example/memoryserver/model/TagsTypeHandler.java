package com.example.memoryserver.model;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.ibatis.type.BaseTypeHandler;
import org.apache.ibatis.type.JdbcType;
import org.apache.ibatis.type.MappedJdbcTypes;
import org.apache.ibatis.type.MappedTypes;

import java.sql.*;
import java.util.HashSet;
import java.util.Set;

/**
 * MyBatis TypeHandler for Set<String> ↔ JSON string.
 * Equivalent of StringSetConverter for JPA.
 */
@MappedTypes(Set.class)
@MappedJdbcTypes(JdbcType.VARCHAR)
public class TagsTypeHandler extends BaseTypeHandler<Set<String>> {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Set<String>> TYPE_REF = new TypeReference<>() {};

    @Override
    public void setNonNullParameter(PreparedStatement ps, int i, Set<String> parameter, JdbcType jdbcType)
            throws SQLException {
        try {
            ps.setString(i, MAPPER.writeValueAsString(parameter));
        } catch (Exception e) {
            ps.setString(i, "[]");
        }
    }

    @Override
    public Set<String> getNullableResult(ResultSet rs, String columnName) throws SQLException {
        return parse(rs.getString(columnName));
    }

    @Override
    public Set<String> getNullableResult(ResultSet rs, int columnIndex) throws SQLException {
        return parse(rs.getString(columnIndex));
    }

    @Override
    public Set<String> getNullableResult(CallableStatement cs, int columnIndex) throws SQLException {
        return parse(cs.getString(columnIndex));
    }

    private Set<String> parse(String json) {
        if (json == null || json.isBlank()) return new HashSet<>();
        try {
            return MAPPER.readValue(json, TYPE_REF);
        } catch (Exception e) {
            return new HashSet<>();
        }
    }
}
