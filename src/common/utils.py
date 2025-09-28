import json

def convert(obj):
    """Recursively convert Plaid SDK objects, SQLite rows, or other 
    non-serializable objects into JSON-safe values."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (list, tuple)):
        return [convert(x) for x in obj]
    if isinstance(obj, dict):
        return {k: convert(v) for k, v in obj.items()}
    if hasattr(obj, "to_dict"):  # For Plaid SDK model objects
        return convert(obj.to_dict())
    return str(obj)

def to_safe_json(obj, indent=2):
    """Return a pretty-printed JSON string with safe conversion applied."""
    return json.dumps(convert(obj), indent=indent)