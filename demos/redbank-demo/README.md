# RedBank Demo

RedBank demo with PostgreSQL database and FastMCP server for querying customer statements and transactions.

## Components

- **PostgreSQL Database**: Stores customer, statement, and transaction data
- **FastMCP Server**: Provides tools to query the database via MCP protocol

## Quick Start

Deploy both PostgreSQL and the MCP server:

```bash
make deploy
```

### Deploy PostgreSQL

```bash
make deploy-postgres
```

### Deploy MCP Server (OpenShift)

Deploy the MCP server (builds image and deploys):
```bash
make deploy-mcp
```

## Cleanup

Remove both stacks:
```bash
make delete
```

### Delete PostgreSQL
```bash
make delete-postgres
```

### Delete MCP Server
```bash
make delete-mcp
```

## Sample Data

The database includes:
- 4 customers (Alice Johnson, Bob Smith, Carol Williams, David Brown)
- 11 statements (Jan - Apr 2025)
- 20+ transactions

## Configuration

- Database credentials: Edit `postgres-db/postgres.yaml`
- MCP server: See `mcp-server/README.md` for configuration options
