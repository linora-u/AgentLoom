"""Shell command analysis built on top of tree-sitter-bash."""

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import List, Optional


from tree_sitter import Language, Parser
import tree_sitter_bash


_OPERATOR_TOKEN_PATTERN = re.compile(r"^[|&;<>]+$")


@dataclass(frozen=True)
class ShellCommandInvocation:
    """A single command invocation extracted from shell AST."""

    name: str
    args: List[str]
    source: str


@dataclass(frozen=True)
class ShellCommandRedirection:
    """A shell redirection extracted from shell AST."""

    operator: str
    target: str


@dataclass(frozen=True)
class ShellCommandAnalysis:
    """Structured shell analysis extracted from tree-sitter AST."""

    commands: List[ShellCommandInvocation]
    operators: List[str]
    redirections: List[ShellCommandRedirection]
    pipelines: List[List[str]]


@lru_cache(maxsize=1)
def _get_bash_language():
    if Language is None or tree_sitter_bash is None:
        raise ImportError(
            "tree-sitter-bash is required for shell parsing. Install it with `uv add tree-sitter-bash`."
        )
    return Language(tree_sitter_bash.language())


def _build_parser():
    if Parser is None:
        raise ImportError(
            "tree-sitter is required for shell parsing. Install it with `uv add tree-sitter`."
        )
    parser = Parser()
    parser.language = _get_bash_language()
    return parser


def _node_text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


class _TreeSitterBashCollector:
    def __init__(self, source_bytes: bytes):
        self.source_bytes = source_bytes
        self.commands: List[ShellCommandInvocation] = []
        self.operators: List[str] = []
        self.redirections: List[ShellCommandRedirection] = []
        self.pipelines: List[List[str]] = []

    def collect(self, root_node) -> ShellCommandAnalysis:
        self._walk(root_node)
        return ShellCommandAnalysis(
            commands=self.commands,
            operators=self.operators,
            redirections=self.redirections,
            pipelines=self.pipelines,
        )

    def _walk(self, node) -> None:
        node_type = getattr(node, "type", "")
        if node_type in ("command", "declaration_command", "unset_command"):
            invocation = self._extract_command_invocation(node)
            if invocation is not None:
                self.commands.append(invocation)
        elif node_type == "pipeline":
            pipeline_commands = self._extract_pipeline_commands(node)
            if pipeline_commands:
                self.pipelines.append(pipeline_commands)
        elif "redirect" in node_type:
            redirection = self._extract_redirection(node)
            if redirection is not None:
                self.redirections.append(redirection)

        if not node.is_named:
            token = _node_text(node, self.source_bytes).strip()
            if token and _OPERATOR_TOKEN_PATTERN.fullmatch(token):
                self.operators.append(token)

        for child in node.children:
            self._walk(child)

    def _extract_command_invocation(self, command_node) -> Optional[ShellCommandInvocation]:
        name = ""
        args: List[str] = []
        node_type = getattr(command_node, "type", "")
        
        if node_type in ("declaration_command", "unset_command"):
            if command_node.children:
                name = _node_text(command_node.children[0], self.source_bytes).strip()
                for child in command_node.children[1:]:
                    args.append(_node_text(child, self.source_bytes).strip())
        else:
            for idx, child in enumerate(command_node.children):
                field_name = command_node.field_name_for_child(idx)
                if field_name not in {"name", "argument"}:
                    continue
                token = _node_text(child, self.source_bytes).strip()
                if not token:
                    continue
                if field_name == "name":
                    name = token
                else:
                    args.append(token)

        if not name:
            return None

        return ShellCommandInvocation(
            name=name,
            args=args,
            source=_node_text(command_node, self.source_bytes).strip(),
        )

    def _extract_pipeline_commands(self, pipeline_node) -> List[str]:
        names: List[str] = []
        for child in pipeline_node.children:
            if getattr(child, "type", "") != "command":
                continue
            invocation = self._extract_command_invocation(child)
            if invocation is None:
                continue
            name = invocation.name.strip()
            if name:
                names.append(name)
        return names

    def _extract_redirection(self, redirection_node) -> Optional[ShellCommandRedirection]:
        operator = ""
        target = ""
        for child in redirection_node.children:
            text = _node_text(child, self.source_bytes).strip()
            if not text:
                continue
            if not child.is_named and _OPERATOR_TOKEN_PATTERN.fullmatch(text):
                if not operator:
                    operator = text
                continue
            if child.is_named:
                target = text

        if not operator or not target:
            return None

        return ShellCommandRedirection(operator=operator, target=target)


def analyze_shell_command(command: str) -> ShellCommandAnalysis:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")

    parser = _build_parser()
    source_bytes = command.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node
    if root.has_error:
        raise ValueError(f"Invalid shell command: {command}")

    collector = _TreeSitterBashCollector(source_bytes=source_bytes)
    return collector.collect(root)
