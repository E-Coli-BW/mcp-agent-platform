package com.example.auth.repository;

import com.example.auth.model.AuthPolicy;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface AuthPolicyRepository extends JpaRepository<AuthPolicy, Long> {

    /**
     * Find matching policies for an actor+audience+tenant.
     * Matches exact values OR wildcards (*).
     * Returns all matching policies (most specific wins at caller level).
     */
    @Query("SELECT p FROM AuthPolicy p WHERE p.enabled = true " +
           "AND p.actor = :actor " +
           "AND (p.audience = :audience OR p.audience = '*') " +
           "AND (p.tenantId = :tenant OR p.tenantId = '*')")
    List<AuthPolicy> findMatchingPolicies(@Param("actor") String actor,
                                          @Param("audience") String audience,
                                          @Param("tenant") String tenant);

    List<AuthPolicy> findByActor(String actor);
}
