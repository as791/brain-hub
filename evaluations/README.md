# MCP evaluations

`brainhub_mcp.xml` contains ten independent, read-only questions against the immutable Brain Hub demo fixture. Seed a clean local database first:

```bash
BRAINHUB_DB_PATH=/tmp/brainhub-eval.db brainhub demo --reset
```

Start the evaluation MCP process with the same `BRAINHUB_DB_PATH`. An evaluation agent should receive only the Brain Hub MCP server and the questions. Answers are stable strings and require combinations of search, node inspection, expansion, and path tools. Do not run these against a personal graph because the expected answers intentionally target fixed demo IDs and times.

The XML is a capability evaluation, not the marketplace's write/security acceptance suite. The latter lives in backend and adapter tests because MCP evaluation questions must remain non-destructive and independent.
