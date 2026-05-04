"""Tests for search/ast_grep_tool — migrated from tests/tools_test/grep/test_ast_grep_search.py"""

import json
from pathlib import Path
import pytest
from src.tools.search.ast_grep_tool import ast_grep_search_file
from src.tools.search.ast_grep_tool.ast_grep_tool import infer_language_from_file

TESTDATA_DIR = Path(__file__).parent / "testdata"

# --- Basic Validation Tests ---
def test_ast_grep_search_file_requires_file_path():
    with pytest.raises(ValueError, match="file_path is required"):
        ast_grep_search_file("", "needle")

def test_ast_grep_search_file_requires_keyword():
    sample_py = TESTDATA_DIR / "python" / "sample.py"
    with pytest.raises(ValueError, match="keyword is required"):
        ast_grep_search_file(str(sample_py), "")

def test_ast_grep_search_file_requires_file_path_to_be_file():
    with pytest.raises(ValueError, match="file_path must be a file"):
        ast_grep_search_file(str(TESTDATA_DIR), "needle")

# --- Complex Syntax Targeting Tests ---
def test_ast_grep_search_python_complex_structures():
    sample_py = TESTDATA_DIR / "python" / "sample.py"
    
    # 1. Test Class
    res_class = json.loads(ast_grep_search_file(str(sample_py), "AdvancedProcessor"))
    assert len(res_class) > 0, "Failed to find Python class"
    assert "class AdvancedProcessor:" in res_class[0]["content"]

    # 2. Test Instance Method
    res_method = json.loads(ast_grep_search_file(str(sample_py), "process_data"))
    assert len(res_method) > 0, "Failed to find Python instance method"
    assert "def process_data" in res_method[0]["content"]

    # 3. Test Static Method
    res_static = json.loads(ast_grep_search_file(str(sample_py), "validate_config"))
    assert len(res_static) > 0, "Failed to find Python static method"
    assert "@staticmethod" in res_static[0]["content"]
    assert "def validate_config" in res_static[0]["content"]

def test_ast_grep_search_go_complex_structures():
    sample_go = TESTDATA_DIR / "go" / "sample.go"
    
    # 1. Test Interface
    res_interface = json.loads(ast_grep_search_file(str(sample_go), "Processor"))
    assert len(res_interface) > 0, "Failed to find Go interface"
    assert "type Processor interface" in res_interface[0]["content"]

    # 2. Test Struct
    res_struct = json.loads(ast_grep_search_file(str(sample_go), "GoTargetStruct"))
    assert len(res_struct) > 0, "Failed to find Go struct"
    assert "type GoTargetStruct struct" in res_struct[0]["content"]

    # 3. Test Method with receiver
    res_method = json.loads(ast_grep_search_file(str(sample_go), "Process"))
    assert len(res_method) > 0, "Failed to find Go method with receiver"
    assert "func (p *DefaultProcessor) Process" in res_method[0]["content"]

def test_ast_grep_search_typescript_complex_structures():
    sample_ts = TESTDATA_DIR / "typescript" / "sample.ts"
    
    # 1. Test Interface
    res_interface = json.loads(ast_grep_search_file(str(sample_ts), "DemoPayload"))
    assert len(res_interface) > 0, "Failed to find TS interface"
    assert "export interface DemoPayload" in res_interface[0]["content"]

    # 2. Test Class
    res_class = json.loads(ast_grep_search_file(str(sample_ts), "PayloadProcessor"))
    assert len(res_class) > 0, "Failed to find TS class"
    assert "export class PayloadProcessor" in res_class[0]["content"]

    # 3. Test Arrow Function (Variable Declaration)
    res_arrow = json.loads(ast_grep_search_file(str(sample_ts), "tsTargetArrow"))
    assert len(res_arrow) > 0, "Failed to find TS arrow function"
    assert "const tsTargetArrow =" in res_arrow[0]["content"]

    # 4. Test Type Alias
    res_type = json.loads(ast_grep_search_file(str(sample_ts), "ComplexType"))
    assert len(res_type) > 0, "Failed to find TS type alias"
    assert "export type ComplexType =" in res_type[0]["content"]

def test_ast_grep_search_python_async_and_decorated():
    sample_py = TESTDATA_DIR / "python" / "sample_async.py"

    res_async = json.loads(ast_grep_search_file(str(sample_py), "fetch_data"))
    assert len(res_async) > 0, "Failed to find Python async function"
    assert "async def fetch_data" in res_async[0]["content"]

    res_decorated_async = json.loads(
        ast_grep_search_file(str(sample_py), "cached_fetch")
    )
    assert len(res_decorated_async) > 0, "Failed to find decorated async function"
    assert "@functools.lru_cache()" in res_decorated_async[0]["content"]
    assert "async def cached_fetch" in res_decorated_async[0]["content"]

def test_ast_grep_search_go_generic_and_type_alias():
    sample_go = TESTDATA_DIR / "go" / "sample_generic.go"

    res_struct = json.loads(ast_grep_search_file(str(sample_go), "Box"))
    assert len(res_struct) > 0, "Failed to find Go generic struct"
    assert "type Box[T any] struct" in res_struct[0]["content"]

    res_interface = json.loads(ast_grep_search_file(str(sample_go), "Handler"))
    assert len(res_interface) > 0, "Failed to find Go generic interface"
    assert "type Handler[T any] interface" in res_interface[0]["content"]

    res_alias = json.loads(ast_grep_search_file(str(sample_go), "Count"))
    assert len(res_alias) > 0, "Failed to find Go type alias"
    assert "type Count = int" in res_alias[0]["content"]

def test_ast_grep_search_typescript_export_default_and_function_expression():
    sample_ts = TESTDATA_DIR / "typescript" / "sample_extra.ts"

    res_class = json.loads(ast_grep_search_file(str(sample_ts), "MyService"))
    assert len(res_class) > 0, "Failed to find TS export default class"
    assert "export default class MyService" in res_class[0]["content"]

    res_func_expr = json.loads(ast_grep_search_file(str(sample_ts), "localFn"))
    assert len(res_func_expr) > 0, "Failed to find TS function expression"
    assert "const localFn = function" in res_func_expr[0]["content"]

    res_arrow = json.loads(ast_grep_search_file(str(sample_ts), "arrowFn"))
    assert len(res_arrow) > 0, "Failed to find TS export arrow function"
    assert "export const arrowFn =" in res_arrow[0]["content"]

def test_ast_grep_search_python_advanced_structures():
    sample_py = TESTDATA_DIR / "python" / "sample_advanced.py"

    res_decorated_class = json.loads(
        ast_grep_search_file(str(sample_py), "TaskPayload")
    )
    assert len(res_decorated_class) > 0, "Failed to find Python decorated class"
    assert "@dataclass" in res_decorated_class[0]["content"]
    assert "class TaskPayload" in res_decorated_class[0]["content"]

    res_decorated_fn = json.loads(ast_grep_search_file(str(sample_py), "build_key"))
    assert len(res_decorated_fn) > 0, "Failed to find Python decorated function"
    assert "@functools.lru_cache" in res_decorated_fn[0]["content"]
    assert "def build_key" in res_decorated_fn[0]["content"]

    res_async_fn = json.loads(ast_grep_search_file(str(sample_py), "normalize"))
    assert len(res_async_fn) > 0, "Failed to find Python async function"
    assert "async def normalize" in res_async_fn[0]["content"]

    res_method = json.loads(ast_grep_search_file(str(sample_py), "run"))
    assert len(res_method) > 0, "Failed to find Python class method"
    assert "def run" in res_method[0]["content"]

    res_static_async = json.loads(ast_grep_search_file(str(sample_py), "check"))
    assert len(res_static_async) > 0, "Failed to find Python decorated async method"
    assert "@staticmethod" in res_static_async[0]["content"]
    assert "async def check" in res_static_async[0]["content"]

    res_decorated_async = json.loads(
        ast_grep_search_file(str(sample_py), "decorated_async")
    )
    assert len(res_decorated_async) > 0, "Failed to find Python decorated async function"
    assert "@functools.cache" in res_decorated_async[0]["content"]
    assert "async def decorated_async" in res_decorated_async[0]["content"]

def test_ast_grep_search_go_advanced_structures():
    sample_go = TESTDATA_DIR / "go" / "sample_advanced.go"

    res_interface = json.loads(ast_grep_search_file(str(sample_go), "Reader"))
    assert len(res_interface) > 0, "Failed to find Go interface in advanced sample"
    assert "type Reader interface" in res_interface[0]["content"]

    res_struct = json.loads(ast_grep_search_file(str(sample_go), "Pair"))
    assert len(res_struct) > 0, "Failed to find Go generic struct in advanced sample"
    assert "type Pair[T any] struct" in res_struct[0]["content"]

    res_alias = json.loads(ast_grep_search_file(str(sample_go), "ID"))
    assert len(res_alias) > 0, "Failed to find Go type alias in advanced sample"
    assert "type ID = int64" in res_alias[0]["content"]

    res_func = json.loads(ast_grep_search_file(str(sample_go), "ComputeSum"))
    assert len(res_func) > 0, "Failed to find Go function in advanced sample"
    assert "func ComputeSum" in res_func[0]["content"]

    res_generic_func = json.loads(ast_grep_search_file(str(sample_go), "Convert"))
    assert len(res_generic_func) > 0, "Failed to find Go generic function in advanced sample"
    assert "func Convert[T any]" in res_generic_func[0]["content"]

    res_method = json.loads(ast_grep_search_file(str(sample_go), "Name"))
    assert len(res_method) > 0, "Failed to find Go value receiver method"
    assert "func (w Worker) Name" in res_method[0]["content"]

    res_ptr_method = json.loads(ast_grep_search_file(str(sample_go), "SetName"))
    assert len(res_ptr_method) > 0, "Failed to find Go pointer receiver method"
    assert "func (w *Worker) SetName" in res_ptr_method[0]["content"]

def test_ast_grep_search_typescript_advanced_structures():
    sample_ts = TESTDATA_DIR / "typescript" / "sample_advanced.ts"

    res_interface = json.loads(ast_grep_search_file(str(sample_ts), "LocalPayload"))
    assert len(res_interface) > 0, "Failed to find TS non-export interface"
    assert "interface LocalPayload" in res_interface[0]["content"]

    res_type = json.loads(ast_grep_search_file(str(sample_ts), "LocalResult"))
    assert len(res_type) > 0, "Failed to find TS non-export type alias"
    assert "type LocalResult =" in res_type[0]["content"]

    res_class = json.loads(ast_grep_search_file(str(sample_ts), "LocalProcessor"))
    assert len(res_class) > 0, "Failed to find TS non-export class"
    assert "class LocalProcessor" in res_class[0]["content"]

    res_enum = json.loads(ast_grep_search_file(str(sample_ts), "LocalStatus"))
    assert len(res_enum) > 0, "Failed to find TS non-export enum"
    assert "enum LocalStatus" in res_enum[0]["content"]

    res_fn = json.loads(ast_grep_search_file(str(sample_ts), "localHelper"))
    assert len(res_fn) > 0, "Failed to find TS function declaration"
    assert "function localHelper" in res_fn[0]["content"]

    res_arrow = json.loads(ast_grep_search_file(str(sample_ts), "mapPayload"))
    assert len(res_arrow) > 0, "Failed to find TS let arrow function"
    assert "let mapPayload =" in res_arrow[0]["content"]

    res_fn_expr = json.loads(ast_grep_search_file(str(sample_ts), "transformPayload"))
    assert len(res_fn_expr) > 0, "Failed to find TS function expression"
    assert "const transformPayload = function" in res_fn_expr[0]["content"]

    res_export_async_fn = json.loads(
        ast_grep_search_file(str(sample_ts), "loadPayload")
    )
    assert len(res_export_async_fn) > 0, "Failed to find TS export async function"
    assert "export async function loadPayload" in res_export_async_fn[0]["content"]

# --- Edge Cases and Error Handling ---
def test_ast_grep_search_not_found():
    sample_py = TESTDATA_DIR / "python" / "sample.py"
    result_json = ast_grep_search_file(str(sample_py), "NonExistentFunction123")
    assert result_json == "[]", "Should return empty JSON array for non-existent keyword"

def test_ast_grep_search_regex_escaping():
    sample_py = TESTDATA_DIR / "python" / "sample.py"
    result_json = ast_grep_search_file(str(sample_py), "Demo.*")
    assert result_json == "[]", "Regex characters should be escaped and not match"

def test_ast_grep_search_explicit_language():
    sample_py = TESTDATA_DIR / "python" / "sample.py"
    result_json = ast_grep_search_file(str(sample_py), "DemoGreeter", language="python")
    results = json.loads(result_json)
    assert len(results) > 0
    assert "class DemoGreeter:" in results[0]["content"]

def test_ast_grep_search_invalid_language():
    sample_py = TESTDATA_DIR / "python" / "sample.py"
    with pytest.raises(ValueError, match="did you mean|Unsupported|language"):
        ast_grep_search_file(str(sample_py), "DemoGreeter", language="invalid_lang_xyz")

# --- Advanced Language Inference ---
def test_infer_language_from_file_detects_python():
    sample_py = TESTDATA_DIR / "python" / "sample.py"
    assert infer_language_from_file(str(sample_py)) == "py"

def test_infer_language_from_file_detects_go():
    sample_go = TESTDATA_DIR / "go" / "sample.go"
    assert infer_language_from_file(str(sample_go)) == "go"

def test_infer_language_from_file_detects_typescript():
    sample_ts = TESTDATA_DIR / "typescript" / "sample.ts"
    assert infer_language_from_file(str(sample_ts)) == "ts"

def test_infer_language_from_file_no_extension():
    script_file = TESTDATA_DIR / "misc" / "my_script"
    assert infer_language_from_file(str(script_file)) == "py"

def test_infer_language_from_file_unknown_extension():
    unknown_file = TESTDATA_DIR / "misc" / "data.xyz123"
    with pytest.raises(ValueError):
        infer_language_from_file(str(unknown_file))
