# AST-Grep Language Rules Configuration

This directory contains language rule configuration files for AST-Grep code search.

[中文版本](#中文文档)

## File Naming Convention

Rule files must follow this naming format:

```
<language>.yaml
```

Examples:
- `py.yaml` - Python language rules
- `ts.yaml` - TypeScript language rules
- `go.yaml` - Go language rules
- `js.yaml` - JavaScript language rules

**The `<language>` part (filename without extension) is the canonical language identifier**, which will be automatically recognized by the code as the standard name for that language.

## YAML File Structure

Each rule file contains multiple rule definitions separated by `---`:

```yaml
id: rule-name
language: <language-alias>
rule:
  kind: <ast-node-type>
  regex: <pattern>
  not:
    inside:
      kind: <parent-node-type>
---
id: another-rule
language: <language-alias>
rule:
  # ... rule definition
```

## Language Field Configuration Guide

### Why Configure Multiple Language Aliases?

The `language` field is used to map Pygments detection results to standardized language names. When Pygments detects a file's language, it returns various aliases (e.g., `python`, `py`, `py3`, `sage`). To improve matching success rate, you should **declare all common aliases** in the YAML file.

### Recommended Language Alias Configurations

#### Python (`py.yaml`)
```yaml
language: python   # Primary Pygments alias
language: py       # Short name
language: py3      # Python 3 alias
language: sage     # SageMath (Python-based)
language: starlark # Bazel/Starlark (Python-like)
```

#### TypeScript (`ts.yaml`)
```yaml
language: typescript  # Primary Pygments alias
language: ts          # Short name
```

#### JavaScript (`js.yaml`)
```yaml
language: javascript  # Primary Pygments alias
language: js          # Short name
language: jsx         # JSX syntax
```

#### Go (`go.yaml`)
```yaml
language: go       # Primary alias
language: golang   # Full name
```

#### Rust (`rust.yaml`)
```yaml
language: rust  # Primary alias
language: rs    # Short name
```

#### Java (`java.yaml`)
```yaml
language: java
```

#### C (`c.yaml`)
```yaml
language: c
```

#### C++ (`cpp.yaml`)
```yaml
language: cpp      # Primary alias
language: c++      # Standard name
language: cc       # Common extension
language: cxx      # Alternative naming
```

#### Ruby (`ruby.yaml`)
```yaml
language: ruby
language: rb
```

#### PHP (`php.yaml`)
```yaml
language: php
language: php3
language: php4
language: php5
```

#### Shell (`bash.yaml`)
```yaml
language: bash
language: sh
language: shell
```

## How to Find Pygments Aliases for a Language

If you're unsure which aliases to configure for a language, use this Python code:

```python
from pygments.lexers import get_lexer_for_filename

# Test file detection
lexer = get_lexer_for_filename('test.py', '')
print(f"Name: {lexer.name}")
print(f"Aliases: {lexer.aliases}")

# Example output:
# Name: Python
# Aliases: ['python', 'py', 'sage', 'python3', 'py3', 'bazel', 'starlark', 'pyi']
```

**Recommendation: Add the first 3-5 common aliases from `lexer.aliases` to your YAML file.**

## Rule Definition Reference

### Basic Fields

- **id**: Unique identifier for the rule (e.g., `def`, `class`, `function`)
- **language**: Language alias (must match Pygments aliases or AST-Grep supported language names)
- **rule**: AST-Grep search rule

### Rule Fields

- **kind**: AST node type (requires understanding of Tree-sitter syntax tree for that language)
- **regex**: Regular expression pattern; `{WORDS}` is a keyword placeholder that will be replaced with the actual search keyword
- **not**: Exclusion condition
  - **inside**: Exclude matches inside specific parent nodes

### Special Placeholders

- `{WORDS}`: Will be replaced with the user's search keyword (escaped)
- Use `\s+` to match whitespace
- Use `[\s\S]*?` to match any character (non-greedy)

## Complete Example: Python Rules

```yaml
# Regular function definition
id: def
language: python
language: py
language: py3
rule:
  kind: function_definition
  regex: def\s+({WORDS})\s*\(\s*([\s\S]*?)\)\s*
  not:
    inside:
      kind: decorated_definition
---
# Class definition
id: class
language: python
language: py
rule:
  kind: class_definition
  regex: {WORDS}
  not:
    inside:
      kind: decorated_definition
---
# Decorated function
id: decorated def
language: python
rule:
  kind: decorated_definition
  regex: def\s+({WORDS})\s*\(\s*([\s\S]*?)\)\s*
```

## Adding New Language Support

### Step 1: Create YAML File

```bash
touch grep_config/<language>.yaml
```

### Step 2: Find Pygments Aliases

```python
from pygments.lexers import get_lexer_by_name, get_all_lexers

# Method 1: By filename
lexer = get_lexer_for_filename('test.<ext>', '')
print(lexer.aliases)

# Method 2: List all lexers
for name, aliases, patterns, mimetypes in get_all_lexers():
    if '<your-language>' in name.lower():
        print(f"{name}: {aliases}")
```

### Step 3: Write Rules

Refer to the Tree-sitter grammar for that language:
- [Tree-sitter documentation](https://tree-sitter.github.io/tree-sitter/)
- Use `ast-grep scan --help` to see supported languages
- Reference existing YAML files as templates

### Step 4: Test Rules

```python
from ast_grep_search import ast_grep_search_file

# Test search
result = ast_grep_search_file('test.<ext>', 'your_keyword')
print(result)
```

## FAQ

### Q: My language is not recognized?

A: Check the following:
1. Is the file naming correct (`<lang>.yaml`)?
2. Does the `language` field in the YAML file include Pygments aliases?
3. Use the Python code above to query Pygments aliases for that file extension
4. Ensure you've added at least the 2-3 most common aliases

### Q: How do I know what to fill for `kind`?

A: Use Tree-sitter playground or AST-Grep debug features:

```bash
# Install ast-grep
npm install -g @ast-grep/cli

# View AST structure
ast-grep scan <file> --debug-query='$A'
```

### Q: My rule doesn't work?

A: Possible reasons:
1. `regex` pattern is incorrect - verify with an online regex tester
2. `kind` doesn't match - use `--debug-query` to see actual AST node types
3. Missing necessary patterns around `{WORDS}` placeholder (like `\s+`)
4. YAML syntax error - check with a YAML linter

### Q: Why does some language fall back to generic fallback?

A: This means there's no corresponding YAML rule file for that language, so the code uses AST-Grep's generic search pattern. While still effective, it may be less precise than custom rules. Consider creating dedicated YAML rules for frequently used languages.

## Contribution Guidelines

When adding new language rules, ensure:

1. ✅ File naming follows `<language>.yaml` format
2. ✅ Include at least 2-3 common Pygments aliases
3. ✅ Rules match major code structures in that language (functions, classes, interfaces, etc.)
4. ✅ Use `{WORDS}` placeholder for keyword matching position
5. ✅ Add necessary exclusion conditions (`not`) to avoid duplicate matches
6. ✅ Test rules work correctly on actual files

## Reference Resources

- [AST-Grep Documentation](https://ast-grep.github.io/)
- [Tree-sitter Documentation](https://tree-sitter.github.io/tree-sitter/)
- [Pygments Supported Languages](https://pygments.org/languages/)
- [Tree-sitter Playground](https://tree-sitter.github.io/tree-sitter/playground)

---

# 中文文档

## AST-Grep 语言规则配置

这个目录包含了用于 AST-Grep 代码搜索的语言规则配置文件。

## 文件命名规范

规则文件必须遵循以下命名格式：

```
<language>.yaml
```

例如：
- `py.yaml` - Python 语言规则
- `ts.yaml` - TypeScript 语言规则
- `go.yaml` - Go 语言规则
- `js.yaml` - JavaScript 语言规则

**文件名（不含扩展名）即为规范语言标识符**，会被代码自动识别为该语言的标准名称。

## YAML 文件结构

每个规则文件包含多个规则定义，用 `---` 分隔：

```yaml
id: rule-name
language: <language-alias>
rule:
  kind: <ast-node-type>
  regex: <pattern>
  not:
    inside:
      kind: <parent-node-type>
---
id: another-rule
language: <language-alias>
rule:
  # ... 规则定义
```

## Language 字段配置指南

### 为什么要配置多个 language 别名？

`language` 字段用于建立 Pygments 检测结果到标准化语言名的映射。Pygments 在检测文件语言时会返回各种别名（如 `python`, `py`, `py3`, `sage` 等），为了提高匹配成功率，应该在 YAML 文件中**声明所有常见的别名**。

### 推荐的 language 别名配置

#### Python (`py.yaml`)
```yaml
language: python   # Pygments 主要别名
language: py       # 短名称
language: py3      # Python 3 别名
language: sage     # SageMath (基于 Python)
language: starlark # Bazel/Starlark (类似 Python)
```

#### TypeScript (`ts.yaml`)
```yaml
language: typescript  # Pygments 主要别名
language: ts          # 短名称
```

#### JavaScript (`js.yaml`)
```yaml
language: javascript  # Pygments 主要别名
language: js          # 短名称
language: jsx         # JSX 语法
```

#### Go (`go.yaml`)
```yaml
language: go       # 主要别名
language: golang   # 全称
```

#### Rust (`rust.yaml`)
```yaml
language: rust  # 主要别名
language: rs    # 短名称
```

#### Java (`java.yaml`)
```yaml
language: java
```

#### C (`c.yaml`)
```yaml
language: c
```

#### C++ (`cpp.yaml`)
```yaml
language: cpp      # 主要别名
language: c++      # 标准名称
language: cc       # 常见扩展名
language: cxx      # 另一种命名
```

#### Ruby (`ruby.yaml`)
```yaml
language: ruby
language: rb
```

#### PHP (`php.yaml`)
```yaml
language: php
language: php3
language: php4
language: php5
```

#### Shell (`bash.yaml`)
```yaml
language: bash
language: sh
language: shell
```

## 如何查找语言的 Pygments 别名

如果你不确定某个语言应该配置哪些别名，可以使用以下 Python 代码查询：

```python
from pygments.lexers import get_lexer_for_filename

# 测试文件检测
lexer = get_lexer_for_filename('test.py', '')
print(f"Name: {lexer.name}")
print(f"Aliases: {lexer.aliases}")

# 输出示例：
# Name: Python
# Aliases: ['python', 'py', 'sage', 'python3', 'py3', 'bazel', 'starlark', 'pyi']
```

**建议：将 `lexer.aliases` 中的前 3-5 个常见别名都添加到 YAML 文件中。**

## 规则定义说明

### 基本字段

- **id**: 规则的唯一标识符（如 `def`, `class`, `function`）
- **language**: 语言别名（必须匹配 Pygments 的别名或 AST-Grep 支持的语言名）
- **rule**: AST-Grep 搜索规则

### Rule 字段

- **kind**: AST 节点类型（需要了解该语言的 Tree-sitter 语法树结构）
- **regex**: 正则表达式模式，`{WORDS}` 是关键字占位符，会被实际搜索关键字替换
- **not**: 排除条件
  - **inside**: 排除在特定父节点内的匹配

### 特殊占位符

- `{WORDS}`: 会被替换为用户搜索的关键字（已转义）
- 使用 `\s+` 匹配空白字符
- 使用 `[\s\S]*?` 匹配任意字符（非贪婪）

## 完整示例：Python 规则

```yaml
# 普通函数定义
id: def
language: python
language: py
language: py3
rule:
  kind: function_definition
  regex: def\s+({WORDS})\s*\(\s*([\s\S]*?)\)\s*
  not:
    inside:
      kind: decorated_definition
---
# 类定义
id: class
language: python
language: py
rule:
  kind: class_definition
  regex: {WORDS}
  not:
    inside:
      kind: decorated_definition
---
# 装饰器函数
id: decorated def
language: python
rule:
  kind: decorated_definition
  regex: def\s+({WORDS})\s*\(\s*([\s\S]*?)\)\s*
```

## 添加新语言支持

### 步骤 1: 创建 YAML 文件

```bash
touch grep_config/<language>.yaml
```

### 步骤 2: 查找 Pygments 别名

```python
from pygments.lexers import get_lexer_by_name, get_all_lexers

# 方式1：通过文件名查找
lexer = get_lexer_for_filename('test.<ext>', '')
print(lexer.aliases)

# 方式2：列出所有 lexer
for name, aliases, patterns, mimetypes in get_all_lexers():
    if '<your-language>' in name.lower():
        print(f"{name}: {aliases}")
```

### 步骤 3: 编写规则

参考该语言的 Tree-sitter 语法：
- [Tree-sitter 语法文档](https://tree-sitter.github.io/tree-sitter/)
- 使用 `ast-grep scan --help` 查看支持的语言
- 参考现有的 YAML 文件作为模板

### 步骤 4: 测试规则

```python
from ast_grep_search import ast_grep_search_file

# 测试搜索
result = ast_grep_search_file('test.<ext>', 'your_keyword')
print(result)
```

## 常见问题

### Q: 我的语言没有被识别？

A: 检查以下几点：
1. 文件命名是否正确（`<lang>.yaml`）
2. YAML 文件中的 `language` 字段是否包含了 Pygments 的别名
3. 使用上面的 Python 代码查询该文件扩展名对应的 Pygments 别名
4. 确保至少添加了最常见的 2-3 个别名

### Q: 如何知道 kind 应该填什么？

A: 使用 Tree-sitter 的 playground 或 AST-Grep 的调试功能：

```bash
# 安装 ast-grep
npm install -g @ast-grep/cli

# 查看 AST 结构
ast-grep scan <file> --debug-query='$A'
```

### Q: 我的规则不生效？

A: 可能的原因：
1. `regex` 模式不正确 - 使用在线正则测试工具验证
2. `kind` 不匹配 - 使用 `--debug-query` 查看实际的 AST 节点类型
3. `{WORDS}` 占位符前后缺少必要的模式（如 `\s+`）
4. YAML 语法错误 - 使用 YAML linter 检查

### Q: 为什么有些语言回退到 generic fallback？

A: 这表示该语言没有对应的 YAML 规则文件，代码会使用 AST-Grep 的通用搜索模式。虽然仍然有效，但可能不如自定义规则精确。建议为常用语言创建专门的 YAML 规则。

## 贡献指南

添加新语言规则时，请确保：

1. ✅ 文件命名遵循 `<language>.yaml` 格式
2. ✅ 至少包含 2-3 个 Pygments 常见别名
3. ✅ 规则能匹配该语言的主要代码结构（函数、类、接口等）
4. ✅ 使用 `{WORDS}` 占位符作为关键字匹配位置
5. ✅ 添加必要的排除条件（`not`）避免重复匹配
6. ✅ 测试规则在实际文件上能正确工作

## 参考资源

- [AST-Grep 文档](https://ast-grep.github.io/)
- [Tree-sitter 文档](https://tree-sitter.github.io/tree-sitter/)
- [Pygments 支持的语言列表](https://pygments.org/languages/)
- [Tree-sitter Playground](https://tree-sitter.github.io/tree-sitter/playground)
