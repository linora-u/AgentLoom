"""Tests for LLM semantic type coercion of tool parameters.

Covers boolean, integer, number, and string type coercion, as well as
edge cases like empty schema, missing keys, and already-correct types.
"""

import pytest

from src.lib.smolagents.hooks.type_coercion import coerce_tool_parameters


# ---------------------------------------------------------------------------
# Boolean coercion
# ---------------------------------------------------------------------------

class TestBooleanCoercion:
    """Schema type 'boolean' should convert string booleans to Python bool."""

    @pytest.mark.parametrize("input_val, expected", [
        ("true", True),
        ("false", False),
        ("True", True),
        ("False", False),
        ("TRUE", True),
        ("FALSE", False),
        ("  true  ", True),   # leading/trailing whitespace
        ("  FALSE ", False),
    ])
    def test_string_boolean_variants(self, input_val, expected):
        schema = {"flag": {"type": "boolean"}}
        result = coerce_tool_parameters({"flag": input_val}, schema)
        assert result["flag"] is expected

    def test_non_boolean_string_unchanged(self):
        """A string that is not 'true'/'false' should remain a string."""
        schema = {"flag": {"type": "boolean"}}
        result = coerce_tool_parameters({"flag": "yes"}, schema)
        assert result["flag"] == "yes"

    def test_already_bool_unchanged(self):
        """A Python bool should pass through untouched."""
        schema = {"flag": {"type": "boolean"}}
        result = coerce_tool_parameters({"flag": True}, schema)
        assert result["flag"] is True

        result = coerce_tool_parameters({"flag": False}, schema)
        assert result["flag"] is False


# ---------------------------------------------------------------------------
# Integer coercion
# ---------------------------------------------------------------------------

class TestIntegerCoercion:
    """Schema type 'integer' should convert numeric strings to Python int."""

    @pytest.mark.parametrize("input_val, expected", [
        ("42", 42),
        ("-7", -7),
        ("0", 0),
        ("  100  ", 100),   # whitespace tolerance
    ])
    def test_valid_integer_strings(self, input_val, expected):
        schema = {"count": {"type": "integer"}}
        result = coerce_tool_parameters({"count": input_val}, schema)
        assert result["count"] == expected
        assert isinstance(result["count"], int)

    @pytest.mark.parametrize("input_val", [
        "abc",
        "3.14",
        "12e3",
        "",
        "  ",
        "Infinity",
    ])
    def test_non_integer_strings_unchanged(self, input_val):
        """Non-integer strings must not be coerced."""
        schema = {"count": {"type": "integer"}}
        result = coerce_tool_parameters({"count": input_val}, schema)
        assert result["count"] == input_val

    def test_already_int_unchanged(self):
        """A Python int should pass through untouched."""
        schema = {"count": {"type": "integer"}}
        result = coerce_tool_parameters({"count": 42}, schema)
        assert result["count"] == 42
        assert isinstance(result["count"], int)


# ---------------------------------------------------------------------------
# Number (float) coercion
# ---------------------------------------------------------------------------

class TestNumberCoercion:
    """Schema type 'number' should convert numeric strings to Python float."""

    @pytest.mark.parametrize("input_val, expected", [
        ("3.14", 3.14),
        ("-0.5", -0.5),
        ("0.0", 0.0),
        ("100", 100.0),        # integer string is also valid for number
        ("  2.718  ", 2.718),  # whitespace
    ])
    def test_valid_number_strings(self, input_val, expected):
        schema = {"ratio": {"type": "number"}}
        result = coerce_tool_parameters({"ratio": input_val}, schema)
        assert result["ratio"] == pytest.approx(expected)
        assert isinstance(result["ratio"], float)

    @pytest.mark.parametrize("input_val", [
        "Infinity",
        "-Infinity",
        "NaN",
        "inf",
        "abc",
        "",
        "1e10",    # scientific notation not matched by the regex
    ])
    def test_infinity_nan_no_conversion(self, input_val):
        """Infinity, NaN, and non-numeric strings must stay as strings."""
        schema = {"ratio": {"type": "number"}}
        result = coerce_tool_parameters({"ratio": input_val}, schema)
        assert result["ratio"] == input_val

    def test_already_float_unchanged(self):
        """A Python float should pass through untouched."""
        schema = {"ratio": {"type": "number"}}
        result = coerce_tool_parameters({"ratio": 3.14}, schema)
        assert result["ratio"] == pytest.approx(3.14)


# ---------------------------------------------------------------------------
# String schema — no coercion
# ---------------------------------------------------------------------------

class TestStringSchemaNoCoercion:
    """When schema type is 'string', no coercion should happen."""

    def test_true_string_stays_string(self):
        schema = {"text": {"type": "string"}}
        result = coerce_tool_parameters({"text": "true"}, schema)
        assert result["text"] == "true"
        assert isinstance(result["text"], str)

    def test_numeric_string_stays_string(self):
        schema = {"text": {"type": "string"}}
        result = coerce_tool_parameters({"text": "42"}, schema)
        assert result["text"] == "42"
        assert isinstance(result["text"], str)


# ---------------------------------------------------------------------------
# Empty / None schema — no crash
# ---------------------------------------------------------------------------

class TestEmptySchema:
    """Passing empty or None schema should not crash."""

    def test_none_schema(self):
        result = coerce_tool_parameters({"x": "true"}, None)
        assert result == {"x": "true"}

    def test_empty_dict_schema(self):
        result = coerce_tool_parameters({"x": "true"}, {})
        assert result == {"x": "true"}

    def test_non_dict_schema_entry(self):
        """Schema entries that are not dicts should be skipped."""
        schema = {"x": "boolean"}  # wrong format: should be {"type": "boolean"}
        result = coerce_tool_parameters({"x": "true"}, schema)
        assert result["x"] == "true"

    def test_schema_entry_missing_type(self):
        """Schema entry dict without 'type' key should be skipped."""
        schema = {"x": {"description": "some flag"}}
        result = coerce_tool_parameters({"x": "true"}, schema)
        assert result["x"] == "true"


# ---------------------------------------------------------------------------
# Key not in schema → no conversion
# ---------------------------------------------------------------------------

class TestKeyNotInSchema:
    """Parameters whose keys don't appear in the schema stay untouched."""

    def test_extra_key_not_coerced(self):
        schema = {"flag": {"type": "boolean"}}
        result = coerce_tool_parameters(
            {"flag": "true", "extra": "42"},
            schema,
        )
        assert result["flag"] is True
        assert result["extra"] == "42"  # not converted to int

    def test_all_keys_unknown(self):
        schema = {"known": {"type": "integer"}}
        result = coerce_tool_parameters(
            {"unknown1": "true", "unknown2": "42"},
            schema,
        )
        assert result["unknown1"] == "true"
        assert result["unknown2"] == "42"


# ---------------------------------------------------------------------------
# Multiple parameters in one call
# ---------------------------------------------------------------------------

class TestMultipleParameters:
    """Several parameters coerced in a single call."""

    def test_mixed_types(self):
        schema = {
            "flag": {"type": "boolean"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "name": {"type": "string"},
        }
        result = coerce_tool_parameters(
            {"flag": "True", "count": "10", "ratio": "2.5", "name": "hello"},
            schema,
        )
        assert result["flag"] is True
        assert result["count"] == 10
        assert result["ratio"] == pytest.approx(2.5)
        assert result["name"] == "hello"

    def test_in_place_mutation(self):
        """The function mutates and returns the same dict object."""
        original = {"flag": "true"}
        schema = {"flag": {"type": "boolean"}}
        returned = coerce_tool_parameters(original, schema)
        assert returned is original
        assert original["flag"] is True


# ---------------------------------------------------------------------------
# T6: Error path edge cases — malformed values
# ---------------------------------------------------------------------------

class TestCoercionErrorPaths:
    """Values that look numeric but are malformed stay as strings."""

    def test_multiple_decimals_unchanged(self):
        """'3.14.159' has two dots — not a valid number."""
        schema = {"x": {"type": "number"}}
        result = coerce_tool_parameters({"x": "3.14.159"}, schema)
        assert result["x"] == "3.14.159"

    def test_locale_comma_decimal_unchanged(self):
        """'3,14' uses comma as decimal — not matched by regex."""
        schema = {"x": {"type": "number"}}
        result = coerce_tool_parameters({"x": "3,14"}, schema)
        assert result["x"] == "3,14"

    def test_very_large_integer_coerced(self):
        """Very large integers should still be coerced if they match regex."""
        schema = {"x": {"type": "integer"}}
        big = "9" * 100
        result = coerce_tool_parameters({"x": big}, schema)
        assert result["x"] == int(big)

    def test_leading_zeros_integer(self):
        """'007' matches integer regex and should be coerced to 7."""
        schema = {"x": {"type": "integer"}}
        result = coerce_tool_parameters({"x": "007"}, schema)
        assert result["x"] == 7

    def test_plus_sign_not_matched(self):
        """'+42' does not match the regex (no + sign support)."""
        schema = {"x": {"type": "integer"}}
        result = coerce_tool_parameters({"x": "+42"}, schema)
        assert result["x"] == "+42"

    def test_empty_string_boolean_unchanged(self):
        """Empty string is not 'true' or 'false'."""
        schema = {"x": {"type": "boolean"}}
        result = coerce_tool_parameters({"x": ""}, schema)
        assert result["x"] == ""

    def test_whitespace_only_integer_unchanged(self):
        """Whitespace-only string is not a valid integer."""
        schema = {"x": {"type": "integer"}}
        result = coerce_tool_parameters({"x": "   "}, schema)
        assert result["x"] == "   "


# ---------------------------------------------------------------------------
# Array coercion (JSON string → list)
# ---------------------------------------------------------------------------

class TestArrayCoercion:
    """Schema type 'array' should convert JSON-encoded string arrays to Python lists."""

    def test_json_array_string_coerced(self):
        """A JSON array string should be parsed into a Python list."""
        schema = {"sections": {"type": "array"}}
        result = coerce_tool_parameters(
            {"sections": '[{"heading": "A", "level": 1, "body": "content"}]'},
            schema,
        )
        assert isinstance(result["sections"], list)
        assert len(result["sections"]) == 1
        assert result["sections"][0]["heading"] == "A"

    def test_simple_array_string(self):
        """A simple JSON array of strings."""
        schema = {"tags": {"type": "array"}}
        result = coerce_tool_parameters(
            {"tags": '["foo", "bar", "baz"]'},
            schema,
        )
        assert result["tags"] == ["foo", "bar", "baz"]

    def test_empty_array_string(self):
        """An empty JSON array string."""
        schema = {"items": {"type": "array"}}
        result = coerce_tool_parameters({"items": "[]"}, schema)
        assert result["items"] == []

    def test_array_with_whitespace(self):
        """Leading/trailing whitespace around the JSON array."""
        schema = {"items": {"type": "array"}}
        result = coerce_tool_parameters({"items": '  [1, 2, 3]  '}, schema)
        assert result["items"] == [1, 2, 3]

    def test_already_list_unchanged(self):
        """A Python list should pass through untouched."""
        schema = {"items": {"type": "array"}}
        original = [1, 2, 3]
        result = coerce_tool_parameters({"items": original}, schema)
        assert result["items"] is original

    def test_non_array_string_unchanged(self):
        """A string that is not a JSON array should remain a string."""
        schema = {"items": {"type": "array"}}
        result = coerce_tool_parameters({"items": "not an array"}, schema)
        assert result["items"] == "not an array"

    def test_invalid_json_array_unchanged(self):
        """Invalid JSON that starts with [ should remain a string."""
        schema = {"items": {"type": "array"}}
        result = coerce_tool_parameters({"items": "[invalid json"}, schema)
        assert result["items"] == "[invalid json"

    def test_json_object_string_not_coerced_to_array(self):
        """A JSON object string should not be coerced when schema expects array."""
        schema = {"items": {"type": "array"}}
        result = coerce_tool_parameters({"items": '{"key": "value"}'}, schema)
        assert result["items"] == '{"key": "value"}'

    def test_real_minimax_sections_bug(self):
        """Exact reproduction of the MiniMax sections-as-string bug from ai_check_agent logs."""
        schema = {"sections": {"type": "array"}}
        sections_str = '[{"heading": "A. CAN file list", "level": 1, "body": "## A. File list\\n\\n| File | Module |\\n|------|------|\\n| CanIf.h | CanIf |"}]'
        result = coerce_tool_parameters({"sections": sections_str}, schema)
        assert isinstance(result["sections"], list)
        assert result["sections"][0]["heading"] == "A. CAN file list"


# ---------------------------------------------------------------------------
# Object coercion (JSON string → dict)
# ---------------------------------------------------------------------------

class TestObjectCoercion:
    """Schema type 'object' should convert JSON-encoded string objects to Python dicts."""

    def test_json_object_string_coerced(self):
        """A JSON object string should be parsed into a Python dict."""
        schema = {"metadata": {"type": "object"}}
        result = coerce_tool_parameters(
            {"metadata": '{"key": "value", "count": 42}'},
            schema,
        )
        assert isinstance(result["metadata"], dict)
        assert result["metadata"]["key"] == "value"
        assert result["metadata"]["count"] == 42

    def test_empty_object_string(self):
        """An empty JSON object string."""
        schema = {"config": {"type": "object"}}
        result = coerce_tool_parameters({"config": "{}"}, schema)
        assert result["config"] == {}

    def test_already_dict_unchanged(self):
        """A Python dict should pass through untouched."""
        schema = {"config": {"type": "object"}}
        original = {"a": 1}
        result = coerce_tool_parameters({"config": original}, schema)
        assert result["config"] is original

    def test_non_object_string_unchanged(self):
        """A string that is not a JSON object should remain a string."""
        schema = {"config": {"type": "object"}}
        result = coerce_tool_parameters({"config": "not json"}, schema)
        assert result["config"] == "not json"

    def test_invalid_json_object_unchanged(self):
        """Invalid JSON that starts with { should remain a string."""
        schema = {"config": {"type": "object"}}
        result = coerce_tool_parameters({"config": "{bad json"}, schema)
        assert result["config"] == "{bad json"

    def test_json_array_not_coerced_to_object(self):
        """A JSON array string should not be coerced when schema expects object."""
        schema = {"config": {"type": "object"}}
        result = coerce_tool_parameters({"config": '[1, 2, 3]'}, schema)
        assert result["config"] == '[1, 2, 3]'
