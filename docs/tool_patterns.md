# Tool Call Pattern Matching System

The Serverless Proxy includes a powerful pattern matching system that converts non-standard tool calls from various AI models into a compatible format for tools that expect standard OpenAI-style function calling.

## Problem Statement

Different AI models output tool calls in different formats:

| Model | Example Tool Call |
|-------|-------------------|
| Standard OpenAI | `{"name": "read", "arguments": {"filePath": "/path"}}` |
| Qwen 3.5 | `<read_file>file_path="/path">` |
| Some models | `[searching for files]` (action style) |
| Custom XML | `<tool_call><function=read>...</tool_call>` |

The proxy sits between misbehaving models and standard tools, translating these varied formats into a consistent format.

## How It Works

1. Model sends a response with tool calls in any format
2. The `extract_tool_calls()` function iterates through enabled patterns
3. Patterns are checked in priority order (highest first)
4. First matching pattern extracts the tool name and arguments
5. Tool is remapped and parameters are normalized
6. Standard tool call format is returned

## Database Schema

```sql
CREATE TABLE tool_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT NOT NULL UNIQUE,
    pattern_type TEXT NOT NULL,
    regex_pattern TEXT NOT NULL,
    tool_name TEXT,              -- Static tool name if always same
    tool_name_group INTEGER,     -- Capture group for dynamic tool name
    tool_name_json_path TEXT,    -- JSON path for tool name extraction
    tool_name_mapping TEXT,      -- JSON: map tool names (e.g., {"read_file": "read"})
    parameter_mapping TEXT,       -- JSON: map parameter names
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 0   -- Higher = checked first
);
```

## Pattern Types

| Type | Description | Example |
|------|-------------|---------|
| `fence` | Code block format | ` ```json {...} ``` ` |
| `inline` | Inline JSON | `{"name": "read", ...}` |
| `xml` | XML tags | `<tool_call>...</tool_call>` |
| `bracket` | Bracket enclosed | `[tool]read[/tool] {...}` |
| `action` | Action description | `[searching for files]` |

## Tool Name Extraction Methods

There are three ways to extract the tool name from a matched pattern:

### 1. Static Tool Name
Set `tool_name` to a fixed value. Use when all matches use the same tool.

```json
{
  "tool_name": "bash"
}
```

### 2. Capture Group
Set `tool_name_group` to the group number (1-based) that contains the tool name.

```json
{
  "regex_pattern": "<tool_use name=\"(\\w+)\">(.*?)</tool_use>",
  "tool_name_group": 1
}
```

### 3. JSON Path
Set `tool_name_json_path` to extract from JSON content. Use `name` for `{"name": "..."}` or `arguments.name` for nested.

```json
{
  "regex_pattern": "<tool_call>(.*?)</tool_call>",
  "tool_name_json_path": "name"
}
```

## Default Patterns

The system auto-seeds these patterns on first run:

| Name | Type | Regex | Tool Source |
|------|------|-------|--------------|
| fence_json | fence | `` ```\s*(\w*)\s*\n?(.*?)``` `` | JSON path: name |
| inline_json | inline | `\{.*?"name"\s*:\s*"(\w+)".*?"arguments"\s*:\s*(\{[^}]*\})` | Group 1 |
| tool_use | xml | `<tool_use\s+code\s+name="(\w+)"\s*>(.*?)</tool_use>` | Group 1 |
| tool_code | xml | `<tool_code>(.*?)</tool_code>` | JSON path: name |
| tool_call | xml | `<tool_call>(.*?)</tool_call>` | JSON path: name |
| tool_call_nested_xml | xml | `<tool_call><function=tool><parameter=key>value</parameter></function></tool_call>` | Group 1 + param mapping |
| tool_call_bare_param | xml | `<tool_call> tool <parameter=key>value</parameter> </function>` (closing tags optional) | Group 1 + param mapping |
| tool_call_alt | bracket | `\[TOOL_CALL\]\s*(\{.*?\})\s*\[/TOOL_CALL\]` | JSON path: name |
| bracket_tool | bracket | `\[tool\](\w+)\[/tool\]\s*(\{.*?\})` | Group 1 |
| action | action | `\[([a-zA-Z][^\]]+)\]` | Keyword mapping |

## Mapping Examples

### Tool Name Mapping
Transform non-standard tool names to standard ones:

```json
{
  "tool_name_mapping": "{\"read_file\": \"read\", \"write_file\": \"write\", \"run_command\": \"bash\"}"
}
```

Maps: `read_file` → `read`, `write_file` → `write`, `run_command` → `bash`

### Parameter Mapping
Transform parameter names:

```json
{
  "parameter_mapping": "{\"file_path\": \"filePath\", \"cmd\": \"command\"}"
}
```

Maps: `file_path` → `filePath`, `cmd` → `command`

## Admin UI

Navigate to **Patterns** tab in the admin dashboard to:
- View all patterns with their regex and configuration
- Add new patterns
- Edit existing patterns
- Delete patterns
- Enable/disable patterns
- Test patterns before saving

## API Endpoints

```bash
# List all patterns
GET /api/admin/tool-patterns

# Create pattern
POST /api/admin/tool-patterns
{
  "pattern_name": "my_pattern",
  "pattern_type": "fence",
  "regex_pattern": "...",
  "priority": 50,
  "tool_name_group": 1,
  "tool_name_json_path": "name",
  "tool_name_mapping": "{}",
  "parameter_mapping": "{}",
  "enabled": true
}

# Update pattern
PUT /api/admin/tool-patterns/<id>

# Delete pattern
DELETE /api/admin/tool-patterns/<id>

# Compatibility aliases used by some reverse-proxy setups
GET/POST /tool-patterns
PUT/DELETE /tool-patterns/<id>
GET/POST /endpoints/patterns
PUT/DELETE /endpoints/patterns/<id>
```

## Tool-Specific Normalization

Some tool schemas require fields that malformed model outputs may omit. The proxy applies tool-specific normalization after extraction.

### Bash normalization

For `bash` tool calls, the proxy ensures arguments always include:

- `command` (required)
- `description` (required)

If a model places command text in `filePath`, `file_path`, `value`, `query`, or `path`, it is remapped to `command` automatically.

## Example: Adding a Custom Pattern

Suppose a model outputs tool calls like:
```
[EXECUTE:bash:ls -la /tmp]
```

To handle this:

1. Go to Admin UI → Patterns → Add Pattern
2. Fill in:
   - **Pattern Name**: `execute_bracket`
   - **Pattern Type**: `bracket`
   - **Priority**: `95` (high to check first)
   - **Regex**: `\[EXECUTE:(\w+):([^\]]+)\]`
   - **Tool Name Group**: `1`
   - **Tool Mapping**: `{"EXECUTE": "bash"}`
   - **Parameter Mapping**: `{"2": "command"}` (or handle differently)

## Testing Your Pattern

```python
from simple_bridge import extract_tool_calls

# Test content
content = "[EXECUTE:bash:ls -la /tmp]"
result = extract_tool_calls(content)

if result[0]:
    print(f"Tool: {result[0][0]['function']['name']}")
    print(f"Args: {result[0][0]['function']['arguments']}")
else:
    print("No tool call extracted")
```

## Common Issues

1. **Pattern not matching**: Check regex with `re.search()` in Python
2. **Wrong tool name**: Verify `tool_name_group` or `tool_name_json_path`
3. **Arguments not parsed**: Ensure JSON is valid in captured content
4. **Priority too low**: Increase priority to check before other patterns

## Use Cases

1. **Model compatibility**: Handle models with non-standard tool call formats
2. **Legacy model support**: Add patterns for older models
3. **Custom model support**: Add patterns for fine-tuned models
4. **Testing**: Quickly add/disable patterns without code changes
