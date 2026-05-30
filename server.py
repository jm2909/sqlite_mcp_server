"""
SQLite MCP Server for News Details

Provides tools to interact with a SQLite database containing news articles.
Tools:
  - get_schema: Retrieve the database schema
  - get_data_from_table: Query data from the new_details table
  - prepare_query: Use LLM to convert natural language to SQL
  - post_into_table_details: Insert a new row into the new_details table

The new_details table schema:
  - id (INTEGER PRIMARY KEY AUTOINCREMENT)
  - timestamp (TEXT)
  - source (TEXT)
  - news (TEXT)
  - header (TEXT)
  - keywords (TEXT)
"""

import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

# Determine the database path – default to a local file inside the mcp_server dir
DB_DIR = Path(__file__).parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DB_DIR / "news_details.db")

# LLM configuration (mirrors the pattern used in bengal_politics/orchestrator.py)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
USE_OPENAI = os.getenv("USE_OPENAI", "true").lower() == "true"
USE_DEEPSEEK = os.getenv("USE_DEEPSEEK", "false").lower() == "true"
USE_GROQ = os.getenv("USE_GROQ", "false").lower() == "true"
USE_OLLAMA = os.getenv("LLM_PROVIDER", "openai").lower() == "ollama"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

NEW_DETAILS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS new_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    news TEXT NOT NULL,
    header TEXT NOT NULL,
    keywords TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    """Return a connection to the SQLite database (with row factory)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_table():
    """Create the new_details table if it does not exist."""
    conn = get_connection()
    try:
        conn.execute(NEW_DETAILS_TABLE_DDL)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LLM helper (mirrors orchestrator.py patterns)
# ---------------------------------------------------------------------------

def _llm_generate(prompt: str, system_prompt: str | None = None) -> str:
    """
    Send a prompt to the configured LLM provider and return the text response.

    Supports: ollama, openai, groq, deepseek.
    Falls back to a simple message if no provider is available.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    try:
        # --- Ollama (local LLM) ---
        if USE_OLLAMA:
            import httpx
            # Build the full prompt with system prompt if provided
            full_prompt = prompt
            if system_prompt:
                full_prompt = f"{system_prompt}\n\n{prompt}"
            resp = httpx.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": full_prompt,
                    "stream": False,
                    "options": {"num_predict": 500, "temperature": 0.1},
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("response", "")
            if raw:
                return raw.strip()
            return "-- Ollama returned empty response --"

        if USE_GROQ and GROQ_API_KEY:
            client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
            model = os.getenv("GROQ_MODEL", "llama3-8b-8192")
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=500,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()

        if USE_OPENAI and OPENAI_API_KEY:
            client = OpenAI(api_key=OPENAI_API_KEY)
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=500,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()

        if USE_DEEPSEEK and DEEPSEEK_API_KEY:
            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                max_tokens=500,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()

    except Exception as e:
        return f"-- LLM call failed: {e} --"

    return "-- No LLM provider configured --"


# ---------------------------------------------------------------------------
# FastMCP Server
# ---------------------------------------------------------------------------

# Create the server instance
mcp = FastMCP(
    "news-sqlite-server",
    instructions="SQLite MCP server for managing news details. Provides schema inspection, "
                 "data querying, natural-language-to-SQL conversion, and row insertion.",
    version="1.0.0",
)

# Ensure the table exists on import
ensure_table()


# ---------------------------------------------------------------------------
# Tool 1: get_schema
# ---------------------------------------------------------------------------

@mcp.tool(description="Retrieve the full schema of the SQLite database, including all table names, column names, types, and constraints.")
def get_schema() -> str:
    """
    Query the sqlite_master table and PRAGMA table_info to build a human-readable
    description of every table in the database.
    """
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = cursor.fetchall()

        if not tables:
            return "The database contains no tables."

        lines: list[str] = []
        for row in tables:
            table_name = row["name"]
            lines.append(f"Table: {table_name}")
            lines.append(f"  DDL: {row['sql']}")
            # Get column details
            col_cursor = conn.execute(f"PRAGMA table_info(\"{table_name}\");")
            columns = col_cursor.fetchall()
            lines.append("  Columns:")
            for col in columns:
                not_null = "NOT NULL" if col["notnull"] else "NULLABLE"
                default = f" DEFAULT {col['dflt_value']}" if col["dflt_value"] else ""
                pk = " PRIMARY KEY" if col["pk"] else ""
                lines.append(f"    - {col['name']} ({col['type']}) {not_null}{default}{pk}")
            lines.append("")

        return "\n".join(lines)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 2: get_data_from_table
# ---------------------------------------------------------------------------

@mcp.tool(description="Query rows from the new_details table. Supports optional WHERE clause, LIMIT, and ORDER BY. "
                       "Returns results as a JSON array of objects.")
def get_data_from_table(
    where_clause: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str | None = "id DESC",
) -> str:
    """
    Retrieve data from the new_details table.

    Args:
        where_clause: Optional SQL WHERE clause (without the WHERE keyword).
                      Example: "source = 'Times of India'"
        limit: Maximum number of rows to return (default 50, max 500).
        offset: Number of rows to skip (for pagination).
        order_by: ORDER BY clause (without the ORDER BY keyword). Default "id DESC".

    Returns:
        JSON string containing the matching rows.
    """
    if limit > 500:
        limit = 500
    if limit < 1:
        limit = 1

    query = "SELECT id, timestamp, source, news, header, keywords FROM new_details"
    params: list[Any] = []

    if where_clause:
        query += f" WHERE {where_clause}"

    if order_by:
        query += f" ORDER BY {order_by}"

    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_connection()
    try:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        results = [dict(row) for row in rows]
        return json.dumps(results, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 3: prepare_query
# ---------------------------------------------------------------------------

@mcp.tool(description="Convert a natural-language question into a SQLite SQL query using an LLM. "
                       "The LLM is given the database schema so it can generate accurate queries. "
                       "Returns the generated SQL query string.")
def prepare_query(user_question: str) -> str:
    """
    Use an LLM to translate a user's natural-language question into a SQL query
    suitable for the SQLite database.

    The LLM receives the current schema as context so it can reference the correct
    tables and columns.

    Args:
        user_question: The question the user wants to ask in plain English.
                       Example: "Show me all news articles from the last 7 days"

    Returns:
        The generated SQL query string.
    """
    # Fetch the schema to provide as context
    schema_text = get_schema()

    system_prompt = (
        "You are a SQL expert. Given a SQLite database schema and a user's question, "
        "generate a valid SQLite SQL query that answers the question. "
        "Return ONLY the SQL query, without any markdown formatting, explanations, or backticks. "
        "The query must be a single SELECT statement (read-only). "
        "Do not use any features not supported by SQLite."
    )

    prompt = (
        f"Database schema:\n{schema_text}\n\n"
        f"User question: {user_question}\n\n"
        f"Generate a SQLite SQL query for this question."
    )

    sql = _llm_generate(prompt, system_prompt=system_prompt)

    # Clean up common LLM artifacts
    sql = sql.strip()
    if sql.startswith("```sql"):
        sql = sql[6:]
    elif sql.startswith("```"):
        sql = sql[3:]
    if sql.endswith("```"):
        sql = sql[:-3]
    sql = sql.strip().rstrip(";") + ";"

    return sql


# ---------------------------------------------------------------------------
# Tool 4: post_into_table_details
# ---------------------------------------------------------------------------

@mcp.tool(description="Insert a new row into the new_details table. "
                       "All fields except 'source' are required. "
                       "Returns a success message with the new row ID.")
def post_into_table_details(
    source: str,
    news: str,
    header: str,
    keywords: str,
    timestamp: str | None = None,
) -> str:
    """
    Insert a new news article record into the new_details table.

    Args:
        source: The news source name (e.g., "Times of India", "BBC").
        news: The full news article content or summary text.
        header: The headline / title of the news article.
        keywords: Comma-separated keywords related to the article.
        timestamp: ISO-format timestamp. If not provided, the current UTC time is used.

    Returns:
        A JSON string with the result status and the new row ID.
    """
    if not source or not news or not header or not keywords:
        return json.dumps({"error": "All fields (source, news, header, keywords) are required."})

    ts = timestamp or datetime.utcnow().isoformat()

    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO new_details (timestamp, source, news, header, keywords) VALUES (?, ?, ?, ?, ?)",
            (ts, source, news, header, keywords),
        )
        conn.commit()
        new_id = cursor.lastrowid
        return json.dumps({
            "status": "success",
            "message": f"Row inserted successfully with id={new_id}",
            "id": new_id,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"🚀 News SQLite MCP Server starting...")
    print(f"   Database: {DB_PATH}")
    print(f"   LLM Provider: {LLM_PROVIDER}")
    mcp.run(transport="stdio")
