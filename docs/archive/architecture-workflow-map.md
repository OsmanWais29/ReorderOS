# ReorderOS Architecture Workflow Map

This map shows the v1 workflows that must work before real restaurant launch.

## Customer Activation

```mermaid
flowchart TD
    A["Install app"] --> B["Choose EN/FR"]
    B --> C["Create account via Clerk"]
    C --> D["Create restaurant tenant"]
    D --> E["Pick POS"]
    E -->|Clover| F["Connect Clover OAuth"]
    E -->|Unsupported| W["Waitlist capture"]
    F --> G["Initial Clover sync"]
    G --> H["Recipe + item setup"]
    H --> I["Supplier/item setup"]
    I --> J["Invite team"]
    J --> K["Pilot mode dashboard"]
```

Hard gates:

- Unsupported POS cannot proceed to live dashboard.
- Billing hidden in pilot.
- App language can switch EN/FR.
- Staff/Manager accounts are individual accounts.

## Daily Sales To Inventory Loop

```mermaid
flowchart TD
    A["Clover sale"] --> B["Webhook received"]
    B --> C["Verify signature"]
    C --> D["Store raw event in inbox"]
    D --> E["Normalize sale"]
    E --> F["Insert sales row"]
    F --> G["Recipe walk"]
    G --> H["Write inventory_movements"]
    H --> I["Update current_quantity"]
    I --> J["Dashboard/Sales/Stock reads update"]
```

Hard gates:

- Duplicate webhook does not duplicate inventory movement.
- Failed worker can retry safely.
- No LLM in sale depletion.

## Receiving And Price Capture

```mermaid
flowchart TD
    A["Receipt photo"] --> B["Upload to Spaces"]
    B --> C["Anthropic extraction"]
    C --> D["Human review/correction"]
    D --> E["Commit receipt"]
    E --> F["Receipt lines"]
    E --> G["Inventory movements"]
    E --> H["Ingredient prices"]
    F --> I["Variance visibility"]
    H --> J["Tenant-only price alerts"]
```

Hard gates:

- Anthropic result is never auto-committed.
- Failed extraction allows manual receipt entry.
- Price rows are tenant-only in v1.

## Purchase Order Flow

```mermaid
flowchart TD
    A["At-risk item or owner need"] --> B["Agent draft PO or owner manual PO"]
    B --> C["Owner review"]
    C --> D["Owner approve"]
    D --> E["Owner send email"]
    E --> F["Supplier receives PO"]
    F --> G["Await receipt"]
    G --> H["Receipt commit"]
    H --> I["Inventory and variance update"]
```

Hard gates:

- Owner-only manual PO.
- Owner-only approval.
- Owner-only send.
- Email-only in v1.

## Nightly Agent

```mermaid
flowchart TD
    A["Tenant-local 2am guard"] --> B["Integrity checks"]
    B --> C["Clover reconciliation"]
    C --> D["Forecast run"]
    D --> E["At-risk timeline"]
    D --> F["Draft POs"]
    D --> G["Dashboard payload"]
    D --> H["Stock/variance caches"]
    B -->|mismatch| P1["P1 admin alert"]
```

Hard gates:

- Runs once per tenant per local date.
- Forecast math deterministic.
- Draft PO generation is not synchronous.
- Integrity mismatch freezes automation, not owner operations.

## Pilot Launch Workflow

```mermaid
flowchart TD
    A["Clover sandbox pass"] --> B["Internal QA"]
    B --> C["TestFlight + Android internal test"]
    C --> D["Restore drill"]
    D --> E["Legal EN/FR review"]
    E --> F["App Store / Play submission"]
    F --> G["Pilot restaurant 1"]
    G --> H["Observe real day"]
    H --> I["Fix critical issues"]
    I --> J["Pilot restaurants 2-10"]
```

Hard gates:

- No pilot before restore drill.
- No cross-tenant price comparison in pilot.
- Pilot pricing cap: first 10 restaurants.

