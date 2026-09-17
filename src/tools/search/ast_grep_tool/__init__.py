"""AstGrepTool — syntax-aware code search using ast-grep."""

from .ast_grep_tool import ast_grep_search_file, infer_language_from_file

__all__ = ["ast_grep_search_file", "infer_language_from_file"]
