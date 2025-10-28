# RedBank Demo -

# Instructions for the PostgreSQL database for the RedBank demo.

## Deploy PostgreSQL

```bash
make deploy-postgres
```

## Delete PostgreSQL

```bash
make delete-postgres
```

## Sample Data

- 4 customers (Alice Johnson, Bob Smith, Carol Williams, David Brown)
- 11 statements (Jan - Apr 2025)
- 20+ transactions

## Configuration

Edit `postgres-db/postgres.yaml` to customize credentials.
