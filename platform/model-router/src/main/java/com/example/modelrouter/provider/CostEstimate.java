package com.example.modelrouter.provider;

import java.math.BigDecimal;

public record CostEstimate(
        BigDecimal inputCostPer1KTokens,
        BigDecimal outputCostPer1KTokens,
        String currency
) {
    public static CostEstimate unknown() {
        return new CostEstimate(BigDecimal.ZERO, BigDecimal.ZERO, "USD");
    }

    public static CostEstimate usd(double inputPer1K, double outputPer1K) {
        return new CostEstimate(BigDecimal.valueOf(inputPer1K), BigDecimal.valueOf(outputPer1K), "USD");
    }
}
