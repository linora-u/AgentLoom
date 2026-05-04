import ast
import json
import inspect
import textwrap
import warnings
from collections.abc import Callable
from functools import wraps

from smolagents.tools import Tool, TypeHintParsingException, get_json_schema

def tool(tool_function: Callable) -> Tool:
    """
    Convert a function into an instance of a dynamically created Tool subclass.

    Args:
        tool_function (`Callable`): Function to convert into a Tool subclass.
            Should have type hints for each input and a type hint for the output.
            Should also have a docstring including the description of the function
            and an 'Args:' part where each argument is described.
    """
    tool_json_schema = get_json_schema(tool_function)["function"]
    if "return" not in tool_json_schema:
        if len(tool_json_schema["parameters"]["properties"]) == 0:
            tool_json_schema["return"] = {"type": "null"}
        else:
            raise TypeHintParsingException(
                "Tool return type not found: make sure your function has a return type hint!"
            )

    class SimpleTool(Tool):
        def __init__(self):
            self.is_initialized = True

    # Set the class attributes
    SimpleTool.name = tool_json_schema["name"]
    SimpleTool.description = tool_json_schema["description"]
    SimpleTool.inputs = tool_json_schema["parameters"]["properties"]
    SimpleTool.output_type = tool_json_schema["return"]["type"]

    @wraps(tool_function)
    def wrapped_function(*args, **kwargs):
        return tool_function(*args, **kwargs)

    # Bind the copied function to the forward method
    SimpleTool.forward = staticmethod(wrapped_function)

    # Get the signature parameters of the tool function
    sig = inspect.signature(tool_function)
    # - Add "self" as first parameter to tool_function signature
    new_sig = sig.replace(
        parameters=[inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)] + list(sig.parameters.values())
    )
    # - Set the signature of the forward method
    SimpleTool.forward.__signature__ = new_sig

    # Create and attach the source code of the dynamically created tool class and forward method
    # - Get the source code of tool_function
    tool_source = textwrap.dedent(inspect.getsource(tool_function))
    # - Remove the tool decorator and function definition line
    lines = tool_source.splitlines()
    tree = ast.parse(tool_source)
    #   - Find function definition
    func_node = next((node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)), None)
    if not func_node:
        raise ValueError(
            "No function definition found in the provided source of {tool_function.__name__}. "
            "Ensure the input is a standard function."
        )
    #   - Extract decorator lines
    decorator_lines = ""
    if func_node.decorator_list:
        tool_decorators = [d for d in func_node.decorator_list if isinstance(d, ast.Name) and d.id == "tool"]
        if len(tool_decorators) > 1:
            raise ValueError(
                f"Multiple @tool decorators found on function '{func_node.name}'. Only one @tool decorator is allowed."
            )
        if len(tool_decorators) < len(func_node.decorator_list):
            warnings.warn(
                f"Function '{func_node.name}' has decorators other than @tool. "
                "This may cause issues with serialization in the remote executor. See issue #1626."
            )
        decorator_start = tool_decorators[0].end_lineno if tool_decorators else 0
        decorator_end = func_node.decorator_list[-1].end_lineno
        decorator_lines = "\n".join(lines[decorator_start:decorator_end])
    #   - Extract tool source body
    body_start = func_node.body[0].lineno - 1  # AST lineno starts at 1
    tool_source_body = "\n".join(lines[body_start:])
    # - Create the forward method source, including def line and indentation
    forward_method_source = f"def forward{new_sig}:\n{tool_source_body}"
    # - Create the class source
    indent = " " * 4  # for class method
    class_source = (
        textwrap.dedent(f"""
        class SimpleTool(Tool):
            name: str = "{tool_json_schema["name"]}"
            description: str = {json.dumps(textwrap.dedent(tool_json_schema["description"]).strip())}
            inputs: dict[str, dict[str, str]] = {tool_json_schema["parameters"]["properties"]}
            output_type: str = "{tool_json_schema["return"]["type"]}"

            def __init__(self):
                self.is_initialized = True

        """)
        + textwrap.indent(decorator_lines, indent)
        + textwrap.indent(forward_method_source, indent)
    )
    # - Store the source code on both class and method for inspection
    SimpleTool.__source__ = class_source
    SimpleTool.forward.__source__ = forward_method_source

    simple_tool = SimpleTool()
    return simple_tool


def is_tool_decorated(obj) -> bool:
    """
    Check whether a given object has been decorated by `@tool`.

    The `@tool` decorator converts a normal function into an instance of `Tool`,
    so this can be determined by checking whether the object is a `Tool` instance.

    Args:
        obj: Object to check.

    Returns:
        bool: Returns True if the object is decorated by `@tool`, otherwise False.
    """
    # Check whether object is an instance of Tool.
    if not isinstance(obj, Tool):
        return False

    # Further check whether it has key Tool attributes.
    # These are required attributes for SimpleTool created by `@tool`.
    required_attrs = ['name', 'description', 'output_type', 'forward', 'is_initialized']

    for attr in required_attrs:
        if not hasattr(obj, attr):
            return False
    # Check forward method exists and is callable.
    if not callable(getattr(obj, 'forward', None)):
        return False
    # Check name and description are strings.
    if not isinstance(getattr(obj, 'name', None), str):
        return False
    if not isinstance(getattr(obj, 'description', None), str):
        return False
    return True


def ensure_tool_wrapped(tools_list: list) -> list:
    """
    Ensure all objects in the given tool list are wrapped as `Tool` instances.

    If an object is already a `@tool`-decorated tool, keep it as is.
    If it is a normal function, wrap it with the `@tool` decorator.

    Args:
        tools_list: Tool list containing plain functions or decorated Tool objects.

    Returns:
        list[Tool]: List where all tools are decorated by `@tool`.
    """
    result = []
    for item in tools_list:
        if is_tool_decorated(item):
            result.append(item)
        else:
            result.append(tool(item))
    return result
