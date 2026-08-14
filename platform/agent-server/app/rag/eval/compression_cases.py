"""Representative code and non-code fixtures for compression benchmarks."""


# ── Test Samples ──────────────────────────────────────────────
# Real code samples that simulate tool output from file_read

PYTHON_SAMPLE = '''File: src/auth/jwt_handler.py (85 lines total, showing 1-85)
   1 | """JWT token handling for API authentication."""
   2 | 
   3 | import jwt
   4 | import time
   5 | from dataclasses import dataclass
   6 | from typing import Optional
   7 | 
   8 | SECRET_KEY = "your-secret-key"
   9 | ALGORITHM = "HS256"
  10 | TOKEN_EXPIRY = 3600
  11 | 
  12 | @dataclass
  13 | class TokenPayload:
  14 |     user_id: str
  15 |     role: str
  16 |     exp: float
  17 | 
  18 | def create_access_token(user_id: str, role: str = "user") -> str:
  19 |     """Create a JWT access token with expiration.
  20 |     
  21 |     Args:
  22 |         user_id: The unique user identifier
  23 |         role: User role (admin, user, readonly)
  24 |     
  25 |     Returns:
  26 |         Encoded JWT string
  27 |     """
  28 |     payload = {
  29 |         "user_id": user_id,
  30 |         "role": role,
  31 |         "exp": time.time() + TOKEN_EXPIRY,
  32 |         "iat": time.time(),
  33 |     }
  34 |     return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
  35 | 
  36 | def verify_token(token: str) -> Optional[TokenPayload]:
  37 |     """Verify and decode a JWT token.
  38 |     
  39 |     Returns:
  40 |         TokenPayload if valid, None if expired or invalid
  41 |     """
  42 |     try:
  43 |         payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
  44 |         return TokenPayload(
  45 |             user_id=payload["user_id"],
  46 |             role=payload["role"],
  47 |             exp=payload["exp"],
  48 |         )
  49 |     except jwt.ExpiredSignatureError:
  50 |         return None
  51 |     except jwt.InvalidTokenError:
  52 |         return None
  53 | 
  54 | def refresh_token(old_token: str) -> Optional[str]:
  55 |     """Refresh an expired token if it was valid within grace period.
  56 |     
  57 |     Grace period: 24 hours after expiration.
  58 |     Returns new token or None if too old.
  59 |     """
  60 |     try:
  61 |         payload = jwt.decode(old_token, SECRET_KEY, algorithms=[ALGORITHM],
  62 |                             options={"verify_exp": False})
  63 |         if time.time() - payload["exp"] > 86400:
  64 |             return None
  65 |         return create_access_token(payload["user_id"], payload["role"])
  66 |     except jwt.InvalidTokenError:
  67 |         return None
  68 | 
  69 | def extract_user_id(token: str) -> Optional[str]:
  70 |     """Quick extraction of user_id without full verification."""
  71 |     payload = verify_token(token)
  72 |     return payload.user_id if payload else None
  73 | 
  74 | def is_admin(token: str) -> bool:
  75 |     """Check if the token belongs to an admin user."""
  76 |     payload = verify_token(token)
  77 |     return payload is not None and payload.role == "admin"
  78 | 
  79 | def get_token_expiry(token: str) -> Optional[float]:
  80 |     """Get the expiration timestamp of a token."""
  81 |     payload = verify_token(token)
  82 |     return payload.exp if payload else None
  83 | 
  84 | # Aliases for backward compatibility
  85 | decode_token = verify_token
'''

JAVA_SAMPLE = '''File: src/main/java/com/example/UserService.java (70 lines total, showing 1-70)
   1 | package com.example;
   2 | 
   3 | import org.springframework.stereotype.Service;
   4 | import org.springframework.transaction.annotation.Transactional;
   5 | import java.util.Optional;
   6 | import java.util.List;
   7 | 
   8 | /**
   9 |  * Service for managing user accounts.
  10 |  * Handles CRUD operations with caching and audit logging.
  11 |  */
  12 | @Service
  13 | public class UserService {
  14 | 
  15 |     private final UserRepository repository;
  16 |     private final CacheService cache;
  17 | 
  18 |     public UserService(UserRepository repository, CacheService cache) {
  19 |         this.repository = repository;
  20 |         this.cache = cache;
  21 |     }
  22 | 
  23 |     @Transactional(readOnly = true)
  24 |     public Optional<User> findById(Long id) {
  25 |         return cache.get("user:" + id)
  26 |             .or(() -> {
  27 |                 Optional<User> user = repository.findById(id);
  28 |                 user.ifPresent(u -> cache.put("user:" + id, u));
  29 |                 return user;
  30 |             });
  31 |     }
  32 | 
  33 |     @Transactional
  34 |     public User createUser(String name, String email, String role) {
  35 |         if (repository.existsByEmail(email)) {
  36 |             throw new DuplicateEmailException("Email already registered: " + email);
  37 |         }
  38 |         User user = new User();
  39 |         user.setName(name);
  40 |         user.setEmail(email);
  41 |         user.setRole(role);
  42 |         user.setCreatedAt(Instant.now());
  43 |         User saved = repository.save(user);
  44 |         cache.put("user:" + saved.getId(), saved);
  45 |         return saved;
  46 |     }
  47 | 
  48 |     @Transactional
  49 |     public void deleteUser(Long id) {
  50 |         repository.deleteById(id);
  51 |         cache.evict("user:" + id);
  52 |     }
  53 | 
  54 |     @Transactional(readOnly = true)
  55 |     public List<User> findByRole(String role) {
  56 |         return repository.findByRole(role);
  57 |     }
  58 | 
  59 |     @Transactional
  60 |     public User updateEmail(Long id, String newEmail) {
  61 |         User user = repository.findById(id)
  62 |             .orElseThrow(() -> new UserNotFoundException("User not found: " + id));
  63 |         user.setEmail(newEmail);
  64 |         User saved = repository.save(user);
  65 |         cache.put("user:" + id, saved);
  66 |         return saved;
  67 |     }
  68 | }
'''

NON_CODE_SAMPLE = '''📂 call-center-compliance-manager/
├── 📁 docs/
│   ├── 📄 CN-Nonprod-Compliance-Design.md  (15234B)
│   └── 📄 Position-Whitelisting-Config-Design.md  (8923B)
├── 📁 src/
│   ├── 📁 ccs_geo_desensitization/
│   │   ├── 📄 ccs_geo_desensitization_adapter.py  (4521B)
│   │   └── 📁 auth/
│   └── 📁 ccs_position_whitelisting/
│       └── 📄 position_whitelisting_adapter.py  (3891B)
├── 📄 pyproject.toml  (1234B)
└── 📄 README.md  (567B)
''' * 5  # Repeat to make it longer

# Large Python sample — functions are spread across 200+ lines
# AST compression should find ALL function signatures regardless of position
# Head+tail will miss functions in the middle
LARGE_PYTHON_SAMPLE = '''File: src/services/order_service.py (210 lines total, showing 1-210)
   1 | """Order processing service with payment integration."""
   2 | 
   3 | import logging
   4 | from datetime import datetime, timedelta
   5 | from decimal import Decimal
   6 | from typing import Optional, List
   7 | from dataclasses import dataclass
   8 | 
   9 | logger = logging.getLogger(__name__)
  10 | 
  11 | TAX_RATE = Decimal("0.08")
  12 | FREE_SHIPPING_THRESHOLD = Decimal("50.00")
  13 | 
  14 | @dataclass
  15 | class OrderItem:
  16 |     product_id: str
  17 |     name: str
  18 |     quantity: int
  19 |     unit_price: Decimal
  20 | 
  21 | @dataclass
  22 | class Order:
  23 |     order_id: str
  24 |     customer_id: str
  25 |     items: List[OrderItem]
  26 |     status: str = "pending"
  27 |     created_at: datetime = None
  28 | 
  29 | def calculate_subtotal(items: List[OrderItem]) -> Decimal:
  30 |     """Calculate the subtotal before tax and shipping."""
  31 |     return sum(item.unit_price * item.quantity for item in items)
  32 | 
  33 | def calculate_tax(subtotal: Decimal) -> Decimal:
  34 |     """Calculate tax based on configurable rate."""
  35 |     return (subtotal * TAX_RATE).quantize(Decimal("0.01"))
  36 | 
  37 | def calculate_shipping(subtotal: Decimal, address: dict) -> Decimal:
  38 |     """Calculate shipping cost. Free above threshold."""
  39 |     if subtotal >= FREE_SHIPPING_THRESHOLD:
  40 |         return Decimal("0.00")
  41 |     zone = address.get("zone", "domestic")
  42 |     rates = {"domestic": Decimal("5.99"), "international": Decimal("15.99")}
  43 |     return rates.get(zone, Decimal("9.99"))
  44 | 
  45 | def validate_inventory(items: List[OrderItem], inventory_service) -> List[str]:
  46 |     """Check all items are in stock. Returns list of out-of-stock product IDs."""
  47 |     out_of_stock = []
  48 |     for item in items:
  49 |         available = inventory_service.check_stock(item.product_id)
  50 |         if available < item.quantity:
  51 |             out_of_stock.append(item.product_id)
  52 |             logger.warning("Product %s: requested %d, available %d",
  53 |                          item.product_id, item.quantity, available)
  54 |     return out_of_stock
  55 | 
  56 | def apply_discount(subtotal: Decimal, coupon_code: Optional[str], 
  57 |                    coupon_service) -> tuple[Decimal, str]:
  58 |     """Apply discount coupon if valid.
  59 |     
  60 |     Returns:
  61 |         Tuple of (discounted_subtotal, discount_description)
  62 |     """
  63 |     if not coupon_code:
  64 |         return subtotal, "No coupon applied"
  65 |     coupon = coupon_service.validate(coupon_code)
  66 |     if not coupon:
  67 |         return subtotal, f"Invalid coupon: {coupon_code}"
  68 |     if coupon.min_purchase and subtotal < coupon.min_purchase:
  69 |         return subtotal, f"Minimum purchase ${coupon.min_purchase} not met"
  70 |     if coupon.type == "percentage":
  71 |         discount = (subtotal * coupon.value / 100).quantize(Decimal("0.01"))
  72 |     else:
  73 |         discount = min(coupon.value, subtotal)
  74 |     return subtotal - discount, f"{coupon.description}: -${discount}"
  75 | 
  76 | def process_payment(order: Order, payment_method: dict, 
  77 |                     payment_service) -> dict:
  78 |     """Process payment for an order.
  79 |     
  80 |     Supports: credit_card, paypal, bank_transfer
  81 |     Returns payment result dict with transaction_id and status.
  82 |     """
  83 |     total = calculate_subtotal(order.items)
  84 |     tax = calculate_tax(total)
  85 |     shipping = calculate_shipping(total, payment_method.get("address", {}))
  86 |     grand_total = total + tax + shipping
  87 |     
  88 |     try:
  89 |         result = payment_service.charge(
  90 |             amount=grand_total,
  91 |             currency="USD",
  92 |             method=payment_method["type"],
  93 |             details=payment_method["details"],
  94 |         )
  95 |         if result["status"] == "success":
  96 |             order.status = "paid"
  97 |             logger.info("Payment successful for order %s: $%s",
  98 |                        order.order_id, grand_total)
  99 |         return result
 100 |     except Exception as e:
 101 |         logger.error("Payment failed for order %s: %s", order.order_id, e)
 102 |         return {"status": "failed", "error": str(e)}
 103 | 
 104 | def create_order(customer_id: str, items: List[dict],
 105 |                  inventory_service, coupon_code: Optional[str] = None,
 106 |                  coupon_service=None) -> Order:
 107 |     """Create a new order after validation.
 108 |     
 109 |     Full flow:
 110 |     1. Convert items to OrderItem objects
 111 |     2. Validate inventory
 112 |     3. Calculate pricing (subtotal, tax, shipping, discount)
 113 |     4. Create Order object
 114 |     
 115 |     Raises:
 116 |         ValueError: if any item is out of stock
 117 |     """
 118 |     order_items = [
 119 |         OrderItem(
 120 |             product_id=item["product_id"],
 121 |             name=item["name"],
 122 |             quantity=item["quantity"],
 123 |             unit_price=Decimal(str(item["price"])),
 124 |         )
 125 |         for item in items
 126 |     ]
 127 |     
 128 |     out_of_stock = validate_inventory(order_items, inventory_service)
 129 |     if out_of_stock:
 130 |         raise ValueError(f"Items out of stock: {out_of_stock}")
 131 |     
 132 |     import uuid
 133 |     order = Order(
 134 |         order_id=str(uuid.uuid4()),
 135 |         customer_id=customer_id,
 136 |         items=order_items,
 137 |         created_at=datetime.now(),
 138 |     )
 139 |     return order
 140 | 
 141 | def cancel_order(order: Order, reason: str, refund_service) -> bool:
 142 |     """Cancel an order and process refund if paid.
 143 |     
 144 |     Returns True if cancellation successful.
 145 |     """
 146 |     if order.status == "shipped":
 147 |         logger.warning("Cannot cancel shipped order: %s", order.order_id)
 148 |         return False
 149 |     if order.status == "paid":
 150 |         refund_result = refund_service.refund(order.order_id)
 151 |         if not refund_result["success"]:
 152 |             return False
 153 |     order.status = "cancelled"
 154 |     logger.info("Order %s cancelled: %s", order.order_id, reason)
 155 |     return True
 156 | 
 157 | def get_order_summary(order: Order) -> dict:
 158 |     """Generate a human-readable order summary."""
 159 |     subtotal = calculate_subtotal(order.items)
 160 |     tax = calculate_tax(subtotal)
 161 |     return {
 162 |         "order_id": order.order_id,
 163 |         "customer": order.customer_id,
 164 |         "items": len(order.items),
 165 |         "subtotal": str(subtotal),
 166 |         "tax": str(tax),
 167 |         "total": str(subtotal + tax),
 168 |         "status": order.status,
 169 |     }
'''
